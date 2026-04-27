# Frappe Site Lifecycle — Pre-Merge Hardening + Design Cleanup

**Date:** 2026-04-27
**Branch:** `feat/site-lifecycle`
**Status:** Design — pending implementation plan

## Context

The `feat/site-lifecycle` branch added `Deleting` and `Migrating` to the `Frappe Site` lifecycle, generalised the Job manifest builder into `_build_op_job_manifest`, and dispatched reconciliation per status (`_reconcile_site_create` / `_reconcile_site_delete` / `_reconcile_site_migrate`). It also did the `creation_job_*` → `operation_job_*` rename.

A pre-merge audit surfaced five items worth landing before the branch ships:

- **H1.** Cancel-mid-migrate is unsafe — MariaDB has no atomic DDL, so a SIGKILL'd `bench migrate` can leave the schema half-applied.
- **H2.** `_sweep_orphan_site_jobs` has no grace period, so it could race a worker that has just applied a Job but not yet recorded `operation_job_name`.
- **H3.** `on_trash` ignores `Failed` rows that have an `operation_job_name`, leaving an orphan Job alive until TTL.
- **H4.** No reconciliation behavioural tests for the new `Deleting` and `Migrating` branches.
- **D1.** The three task functions (`create_site_task`, `delete_site_task`, `migrate_site_task`) duplicate ~80% of their scaffolding. Any fix that touches the apply / cleanup paths (H2, H3, future ops like backup/restore) needs three copies.

## Decisions

### D1 — Single orchestrator with per-op config

Collapse the three task functions into one `_run_site_op` orchestrator that owns all shared scaffolding. Per-op behaviour is supplied by a small frozen config dataclass plus three callables.

**Config dataclass:**

```python
@dataclass(frozen=True)
class SiteOpConfig:
    op_kind: str                  # "create" | "delete" | "migrate"
    expected_status: str          # "In Progress" | "Deleting" | "Migrating"
    container_label: str          # "create-site" | "delete-site" | "migrate-site"
    failure_log_title: str        # e.g. "Frappe Site Job Submission Failed"
    failure_detail_prefix: str    # e.g. "Job submission failed"
```

**Per-op callables (plain functions, not methods on the config):**

- `build_command(doc) -> str` — bench command string for this op.
- `build_env(doc, creds_secret_name) -> list[dict]` — container env list. `creds_secret_name` is `None` when the op did not build a Secret.
- `build_creds_secret(doc, job_name) -> dict | None` — optional. Returns the Secret manifest, or `None` for ops that need no Secret (migrate; delete-with-external-db-secret).

**Orchestrator responsibilities (single source of truth):**

1. Pre-apply token gate via `_site_operation_matches`.
2. Resolve the bench: load `Helm Release`, build api client, select reference pod, clone reference pod spec.
3. Compute `job_name` via `_job_name(site_name, operation_token)`.
4. Build & apply optional creds Secret (when `build_creds_secret` is supplied and returns non-None).
5. Build bench command (delegated) and container env (delegated).
6. Build full Job manifest via `_build_op_job_manifest` and apply it.
7. Attach owner-reference for the Secret (extracted into helper `_attach_creds_secret_owner_ref`).
8. Post-apply token re-check; on mismatch, tear down the Job and Secret and return.
9. On success: `db_set("operation_job_name" / "operation_job_token")` and publish `frappe_site_status_update` realtime event with the `expected_status`.
10. Exception branch: best-effort delete of any applied Job + Secret; if the token still matches, write `Failed` + truncated detail and log via `frappe.log_error`.

**Public task functions become thin wrappers:**

```python
def create_site_task(site_docname: str, operation_token: str):
    _run_site_op(
        site_docname,
        operation_token,
        config=_CREATE_OP_CONFIG,
        build_command=_create_command,
        build_env=_create_env,
        build_creds_secret=_create_creds_secret,
    )
```

(Equivalent for `delete_site_task` and `migrate_site_task`. Migrate passes `build_creds_secret=None`. Delete supplies it conditionally — see below.)

**Conditional creds Secret for delete.** `delete_site_task` today only builds a creds Secret when the user did not supply an external `db_root_secret`. The orchestrator handles this naturally: `build_creds_secret` returns `None` when no Secret is needed, so the orchestrator skips the apply + ownerRef steps.

**What stays out of the orchestrator:**

- `cancel_site_task` — its shape is "delete a Job and its Secret," not the apply pipeline. Different beast; leaving it alone keeps each function single-purpose.
- The reconciliation per-op handlers (`_reconcile_site_create` / `_delete` / `_migrate`) — they share less common scaffolding and live in a different module; collapsing them is a separate question (out of scope here).

**Trade-offs.**

- **Pro:** ~600 lines of three task functions collapse to ~250 (orchestrator + three thin wrappers + per-op helpers). Future ops (backup, restore) cost a wrapper plus three callables, not a new ~200-line task function.
- **Pro:** H2's grace-period filter and H3's cleanup widening are unrelated to the orchestrator, but any future scaffolding fix lands in one place.
- **Con:** Extra indirection — reading `delete_site_task` no longer shows you the full apply path top-to-bottom. Mitigated by the per-op callables being short, named, and co-located with the wrapper.
- **Con:** The orchestrator's signature is wider than any individual task's was. Mitigated by keeping the config dataclass small (5 fields) and the callables to a fixed set of three.

**Rejected alternatives.**

- **Object-oriented:** `class SiteOperation(ABC)` with `CreateOp` / `DeleteOp` / `MigrateOp` subclasses defining `build_command()` / `build_env()` / `build_creds_secret()`. Heavier than the codebase's existing style, which leans on plain functions. The dataclass + callables approach is equally explicit with less ceremony.
- **Helpers only, keep three task functions:** Extract just the post-apply token-recheck branch and the exception cleanup branch into helpers; leave each task function structurally separate. Smaller diff, but loses the single-source-of-truth benefit. Each future fix still touches three files.
- **Status quo:** Don't refactor; touch all three when fixing H2/H3. Defended in the original branch CHANGELOG, but with three concrete instances on the table the duplication tax is now real.

### H1 — Confirmation-gated cancel for `Migrating`

A `bench migrate` Job killed mid-DDL on MariaDB can leave the schema half-applied. The current code kills the Job cheerfully on both operator Cancel and post-apply token mismatch.

**Server change.** `FrappeSite.cancel_site` gains a `confirm_destructive: bool = False` kwarg:

```python
@frappe.whitelist()
def cancel_site(self, confirm_destructive: bool = False):
    if self.status not in ("In Progress", "Deleting", "Migrating"):
        frappe.throw("Cancel is only available while an operation is in progress.")
    if self.status == "Migrating" and not confirm_destructive:
        frappe.throw(
            "Cancelling a running migration can leave the site's database "
            "schema in a half-applied state with no automatic rollback. "
            "Confirm explicitly to proceed.",
            title="Destructive cancel required",
        )
    # ... existing token rotate / status / status_detail / cancel_site_task enqueue ...
```

The kwarg is ignored when status is `In Progress` or `Deleting` (those have no comparable risk).

**Status detail wording.** When the gated cancel fires, `status_detail` becomes `"Cancelled migration by {user} (destructive cancel acknowledged)."` so the audit trail records that the warning was shown and accepted.

**Client change.** `frappe_site.js` cancel handler branches on status:

- `In Progress` / `Deleting` → existing simple `frappe.confirm(...)` then `frm.call("cancel_site")`.
- `Migrating` → custom dialog (`new frappe.ui.Dialog({...})`) with the destructive warning text and a typed-confirmation field: operator must type `CANCEL` to enable the primary "Cancel migration" button. On submit: `frm.call("cancel_site", { confirm_destructive: 1 })`. Implementation can fall back to a styled `frappe.confirm` with a clear destructive-action warning if the typed confirm proves heavy in code review.

**`on_trash` for `Migrating`.** Refuse direct row deletion, mirroring the existing `Active` refusal:

```
This site has a migration in progress. Cancel the migration first
(it requires explicit confirmation), then delete the row.
```

This forces the operator through the gated-cancel path instead of letting them backdoor a kill via row trash.

**`In Progress` / `Deleting` `on_trash`** stay as-is for the in-flight branch (token rotate + `cancel_site_task` enqueue) — the H3 widening below extends them to `Failed`-with-Job too.

**No `Cancelling` lifecycle state.** Adding one for symmetry would require new reconciler branches (one per op kind) for a state that lasts 0–30 seconds. The existing direct-to-`Failed` transition is cosmetically muddier but operationally fine; defer to a future cycle if it ever bites in practice.

**No retry logic for `cancel_site_task`.** Fire-and-forget matches the existing pattern. The orphan-sweep eventually catches a Job whose deletion failed.

**Rejected alternatives.**

- **Block cancel for `Migrating` entirely.** Brutal if a migrate genuinely hangs; operator's only recourse is waiting up to `activeDeadlineSeconds` (30 min).
- **Best-effort SIGTERM with long `terminationGracePeriodSeconds`.** Doesn't actually help — `bench migrate` has no signal handler that stops at a safe DDL boundary; SIGTERM just delays the same corruption.
- **Status-quo cancel + a documentation note.** Cosmetic; no real safety improvement.

### H2 — Orphan-sweep grace period

`_sweep_orphan_site_jobs` lists Jobs by label and deletes any whose name isn't in any row's `operation_job_name`. The worker writes `operation_job_name` *after* `apply_resource(Job)`. A sweep that runs in the few-millisecond window between Job apply and the `db_set` would kill a Job that's about to be recorded.

**Change.** Filter by Job `creationTimestamp`. Only sweep Jobs older than a grace bound. Constant:

```python
_ORPHAN_SWEEP_GRACE_SECONDS = 300  # 5 minutes
```

**Rationale for 5 minutes.** The race window in the worker is bounded by: K8s ack of Job apply (< 1s typical, < 30s under heavy load), re-read Job for UID (< 1s), re-apply Secret with ownerRef (< 1s), token re-check DB query (< 100ms), `db_set` (< 100ms). Realistic upper bound is well under a minute. Five minutes is hugely conservative but matches the reconciliation tick cadence (orphans get caught on the next-or-next tick rather than the same one), making the behaviour easy to reason about: "a Job younger than one full reconciliation cycle is never swept."

**Implementation.** In the loop over `jobs.items`:

```python
metadata = getattr(job, "metadata", None)
created = getattr(metadata, "creation_timestamp", None) if metadata else None
if created and (now() - created).total_seconds() < _ORPHAN_SWEEP_GRACE_SECONDS:
    continue
```

`now()` reads UTC to match the K8s timestamp's timezone. Missing `creation_timestamp` (shouldn't happen on a real K8s Job, but defensively) → fall through to existing logic, since the worst case there is the existing race that we already tolerate without the filter.

**Rejected alternative — shorter grace (e.g. 60s).** Equally safe in normal operation; faster cleanup of true orphans. Not chosen because the 5-minute reconciliation cadence already bounds how quickly orphans get cleaned anyway, and aligning the grace with the tick cadence makes the contract crisp ("never sweep a Job younger than one tick").

### H3 — `on_trash` for `Failed`-with-Job

Today's `on_trash` flow:

```python
if self.status == "Active":
    frappe.throw(...)
if self.status not in ("In Progress", "Deleting", "Migrating"):
    return
# ... cleanup branch ...
```

A `Failed` row with a real `operation_job_name` (cancelled mid-flight, or a drop-site that left the site present) is trashed with no Job cleanup. TTL reaps after 2 h, but the window isn't justified.

**Change.** Trigger the cleanup whenever `operation_job_name` is set, regardless of status (after the `Active` and `Migrating` refusals):

```python
def on_trash(self):
    if self.status == "Active":
        frappe.throw("...")  # existing message
    if self.status == "Migrating":
        frappe.throw("...")  # new message from H1

    if not self.operation_job_name:
        return

    self.db_set("operation_token", secrets.token_hex(16))
    frappe.enqueue(
        "kubeport.tasks.site_tasks.cancel_site_task",
        cluster=self.cluster,
        namespace=self.namespace or "default",
        job_name=self.operation_job_name,
        queue="short",
        enqueue_after_commit=True,
    )
```

**Why simplify the status check.** The original `status not in ("In Progress", "Deleting", "Migrating")` early-return existed because those were the only states where a Job *could* exist. With H3, the predicate becomes "is there a Job pointer at all?" which is the more direct expression of the intent and naturally covers `Failed` rows.

**Token rotation for non-in-flight rows.** `cancel_site_task` itself takes `(cluster, namespace, job_name)` and ignores the token, so rotation isn't load-bearing for the cancel call. It does matter for the rare race where a reconciliation tick is mid-flight against this same row when `on_trash` runs: rotating the token ensures any subsequent `_finalize_site_status` / `_finalize_site_deletion` write is rejected by the token check rather than silently overwriting a row mid-deletion.

### H4 — Reconciliation behavioural tests for `Deleting` / `Migrating`

The new dispatch paths in `_reconcile_frappe_sites` are exercised end-to-end in `test_site_lifecycle.py`, but the per-branch token-guard / probe-result mapping is not directly tested in `test_reconciliation.py`.

**New test cases (`tests/test_reconciliation.py`):**

For `_reconcile_site_delete`:
- Job succeeded + probe `MISSING` → `_finalize_site_deletion` called (row deleted via `frappe.delete_doc`).
- Job succeeded + probe `EXISTS` → row finalized to `Failed` with descriptive detail.
- Job succeeded + probe `UNKNOWN` → no state write (deferred to next tick).
- Job failed + probe `MISSING` → row deleted (drop-site exit code is not authoritative; site really is gone).
- Job failed + probe `EXISTS` → row finalized to `Failed` with extracted Job log detail.
- Job 404 (TTL elapsed) + probe `MISSING` → row deleted.
- Token mismatch (concurrent supersession) → no state write even on probe `MISSING`.
- Job label mismatch → skip without state write.

For `_reconcile_site_migrate`:
- Job succeeded + probe `EXISTS` → row → `Active`.
- Job succeeded + probe `MISSING` → row → `Failed` with extracted log detail.
- Job succeeded + probe `UNKNOWN` → no state write.
- Job failed + probe `EXISTS` (false-negative recovery) → row → `Active`.
- Job failed + probe `MISSING` → row → `Failed`.
- Job 404 + probe `EXISTS` → row → `Active`.
- Job 404 + probe `MISSING` → row → `Failed`.
- Token mismatch → no state write.
- Job label mismatch → skip.

For `_sweep_orphan_site_jobs` (covers the H2 grace period):
- Tracked Job (in `operation_job_name`) — not deleted.
- Untracked Job older than grace — deleted.
- Untracked Job younger than grace — preserved (new test).
- Untracked Job with missing `creationTimestamp` — deleted (defensive fallthrough).

For `_run_site_op` (covers the D1 orchestrator):
- Success path for each of the three op configs (token rotated, Job + Secret applied, ownerRef attached, post-apply token re-check passes, `db_set` happens).
- Stale-token early exit (orchestrator returns before any apply).
- Post-apply token mismatch → Job + Secret deleted, no `db_set`.
- Exception during apply with `job_applied=True` → Job + Secret cleanup runs, row → `Failed` (when token still matches) or untouched (when superseded).
- Migrate config (no creds Secret): orchestrator skips the Secret apply and the ownerRef step.

For `cancel_site` confirmation gate (H1):
- `cancel_site` with status `Migrating` and `confirm_destructive=False` → throws.
- `cancel_site` with status `Migrating` and `confirm_destructive=True` → succeeds; status becomes `Failed`; status_detail mentions destructive acknowledgement.
- `cancel_site` with status `In Progress` → succeeds without `confirm_destructive`.
- `cancel_site` with status `Deleting` → succeeds without `confirm_destructive`.
- `on_trash` on `Migrating` row → throws.
- `on_trash` on `Failed` row with `operation_job_name` → enqueues `cancel_site_task` (covers H3).
- `on_trash` on `Failed` row with no `operation_job_name` → no enqueue.

Tests use the existing pytest-as-unittest patterns established in `test_site_lifecycle.py` and the mock-based reconciliation patterns in `test_reconciliation.py` (`SimpleNamespace` fakes for K8s objects, `_job_belongs_to_site` patched to bypass label inspection where not under test).

## Out of scope

These were considered and explicitly deferred:

- **D2 — Auto-deleting the row on successful drop.** Real concern (no audit trail), but reopening it touches the doc lifecycle, list-view behaviour, and the realtime client routing. Worth its own cycle; not pre-merge.
- **D3 — `Failed`-without-Job has no "drop a real site" path.** Edge case; orphan-sweep covers the typical race. A "Force Drop" surface deserves its own design.
- **D4 — `Cancelling` lifecycle state.** Cosmetic; defer until it bites.
- **D5 — `_finalize_site_deletion` deleting a doc from a cron task.** Confirmed safe in the existing tests; flag for monitoring but no design change.
- **H5 — Migrate pre-flight bench health probe.** Real value (fail-fast on a degraded bench), but adds a new probe + decision branch in `migrate_site`; better landed alongside the eventual broader health-modeling work called out in `docs/control-plane-state.md`.
- **H6 — Maintenance-mode wrapping for migrate.** Real value but operator-policy-shaped: some operators want it, some don't; surfacing it as a checkbox is fine but is a UX scope expansion. Defer.
- **Backup / restore.** Already explicitly deferred in the branch CHANGELOG.

## Risks and follow-ups

- **Orchestrator regression risk.** Collapsing three near-identical task functions into one is exactly the kind of refactor where a subtle behavioural change (e.g. ordering of the post-apply token re-check vs. the ownerRef attach) could cause silent breakage. Mitigation: the H4 orchestrator tests cover each op's success path, the post-apply mismatch branch, and the exception branch; the existing per-task tests in `test_site_tasks.py` rewire to call `_run_site_op` and continue to assert end-to-end behaviour.
- **Confirmation-gate UX rollout.** The typed-confirmation modal is the first instance of that pattern in the codebase. If it feels heavy in practice, a future iteration can downgrade to a simpler two-button confirm with a strongly-worded message; the server-side `confirm_destructive` arg accommodates either UX without changing the contract.
- **Orphan-sweep grace tuning.** 5 minutes matches the reconciliation tick cadence but means a true orphan from a worker hard-kill survives one extra tick. If real-world ops show that's painful, reducing to 60–120 s is a one-constant change.
- **CHANGELOG follow-up.** This work, when it lands, gets a new entry in `CHANGELOG.md` and updates `docs/control-plane-state.md` (Robustness Properties table picks up "orphan-sweep grace period" and "destructive cancel gated").
