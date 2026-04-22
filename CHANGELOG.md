# Kubeport Changelog

Architecture decision log for contributors and agents. Each entry records what changed, why the approach was chosen, and what alternatives were rejected.

**Format**: Entries are ordered newest-first. Each entry includes Context (the problem or need), the Decision (what was chosen and why), Rejected Alternatives (what was not chosen and why), and Implementation Details (how it was built).

---

## 2026-04-22 — Frappe Site: fix Recreate (Force) path and orphan Job on mid-flight delete

### Context

Pre-merge review of `feat/frappe-site-provisioning` surfaced two defects that survived the earlier rounds:

1. **`Recreate Site (Force)` was dead code.** The UI relabels the primary button and allows the click when `force_create=1` and `status="Active"`, but the server `create_site()` threw on `status="Active"` unconditionally, so the button always errored. The `--force` flag in `_bench_new_site_command` was reachable only from the Failed → retry path, not from Active → recreate, which is the advertised use case.
2. **Orphan Job on mid-flight delete / force-recreate.** `create_site_task` re-checks the operation token after applying the Job (to handle the user cancelling or re-triggering while we were in flight) and returns on mismatch. Nothing tore down the Job it had just applied. `creation_job_name` stays empty on the row, so `on_trash` and reconciliation can't see the Job either. It ran to completion untracked, creating a site on the bench PVC with no MariaDB row. The Job's own TTL reaped the K8s resources but not the PVC data.

### Decision

- **Controller gate respects `force_create`.** `create_site()` now throws on Active only when `force_create` is unchecked. The Python guard matches the JS button's contract.
- **Worker self-cleans on supersession.** When the post-apply token check fails, `create_site_task` calls `_best_effort_delete_job` and `_best_effort_delete_secret` on the Job and Secret it just created before returning. The same cleanup runs in the exception handler when `job_applied` is true, so a failure partway through the ownerRef step does not leak a Job. `_best_effort_delete_job` uses `propagation_policy="Background"` so K8s GC also reaps the Secret via the ownerRef (when it was attached) and the Job's pods.

### Rejected alternatives

- **Reserve `creation_job_name` before applying the Job.** Would make the Job visible to `on_trash` earlier, but introduces a new inconsistency window (a name recorded for a Job that does not yet exist) and forces reconciliation to tolerate phantom names. Deleting from the worker itself, using state it already has in scope, is simpler.
- **Have `on_trash` list and delete Jobs by label selector when `creation_job_name` is empty.** Works, but the discovery call pays a round-trip on every trash of an In Progress doc just to cover a short-window race. Worker-side cleanup is cheaper and catches the same race.

### Implementation details

- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`: `create_site` gate changed to `if self.status == "Active" and not self.force_create`.
- `kubeport/tasks/site_tasks.py`:
  - New `_best_effort_delete_job` mirroring `_best_effort_delete_secret` (404-tolerant, warn-and-continue on everything else, Background propagation).
  - `create_site_task` tracks `job_applied: bool` and `job_name_for_cleanup`; the post-apply token re-check and the exception branch both delete the orphan Job before returning.
- `kubeport/tests/test_site_tasks.py`:
  - New `UnitTestBestEffortDeleteJob` for the helper.
  - New `test_create_site_task_deletes_orphan_job_when_token_superseded_after_apply` driving `_site_operation_matches` to return `[True, False]`.
  - New `test_manifest_passes_force_flag_through_to_bench_command` as a regression guard for the Recreate path.

---

## 2026-04-22 — Frappe Site credentials move to per-Job Secret; orphan-Job cleanup on trash

### Context

The initial Frappe Site provisioning landed with two security/lifecycle gaps surfaced during pre-merge review:

1. `ADMIN_PASSWORD` was injected as a plaintext env `value` in the Job pod spec, visible to anyone with pod-read RBAC and persisted in etcd until the Job's TTL. `DB_ROOT_PASSWORD` had a Kubernetes Secret path but kept a plaintext fallback for the user-provides-password case.
2. Deleting a `Frappe Site` document while a creation Job was in flight orphaned the Job: reconciliation filters rows by `status="In Progress"`, so the deleted row was invisible and the Job ran to completion creating an untracked site.
3. When the Job's `ttlSecondsAfterFinished` elapsed before reconciliation read its final status, the site was left in "In Progress" forever.

### Decision

- **Route every credential through a per-Job Secret.** `create_site_task` creates `{job_name}-creds` (labelled `app.kubernetes.io/managed-by=kubeport`, `kubeport.io/frappe-site=<docname>`) before submitting the Job. The Job consumes `ADMIN_PASSWORD` (always) and `DB_ROOT_PASSWORD` (when no user-supplied Secret) via `secretKeyRef`. After the Job exists we patch the Secret with `ownerReferences` → Job + `blockOwnerDeletion: true`, so K8s GC takes the Secret down with the Job's TTL cleanup. On exception before or during Job apply, we best-effort `delete_namespaced_secret` to avoid orphaning admin creds.
- **Add `on_trash` to `FrappeSite`.** When the doc is deleted while a Job is in flight it rotates `operation_token` (invalidates the worker) and enqueues `cancel_site_task`, which deletes the Job with `propagation_policy="Background"`; ownerRef GC then reaps the creds Secret as a side effect. `cancel_site_task` also calls `_best_effort_delete_secret` as a backstop for the narrow window where the ownerRef patch never attached.
- **Recover zombie "In Progress" on 404.** Reconciliation's `read_namespaced_job` 404 branch now calls `_site_exists_in_bench` — the same two-stage bench probe already used on Job-failed — and transitions the site to Active or Failed instead of logging and leaving it stuck.
- **Validate `site_name`.** Reject anything outside a hostname-style label (lowercase alphanumerics, `.`, `-`, `_`, starting/ending alphanumeric) so the `{bench_release}/{site_name}` autoname, the K8s Job slug, and the bench env stay well-formed. Shell safety was already intact — `"$SITE_NAME"` in `_bench_new_site_command` does not expand command substitutions in the variable's value — but the naming correctness gap needed closing.

### Rejected alternatives

- **Mount passwords via `envFrom: secretRef`**: works, but loses the ability to cleanly mix our creds Secret with the bench reference pod's existing `envFrom` entries without risking accidental env-var leaks. Per-key `secretKeyRef` is more precise.
- **Put `ownerReferences` on the Secret up front**: rejected because the Job's UID isn't known until after `apply_resource(Job)`. The two-step apply (create Secret, create Job, re-apply Secret with UID) is the canonical pattern.
- **Delete the creds Secret from `cancel_site_task` only**: rejected because the rare "Secret applied, Job apply failed" path would leak credentials outside the normal cancel flow. Best-effort cleanup in the exception branch of `create_site_task` closes that window.

### Implementation details

- `kubeport/tasks/site_tasks.py`:
  - New `_build_creds_secret_manifest(secret_name, namespace, site_docname, admin_password, db_root_password)`.
  - `_build_env` rewritten: no more plaintext password parameters; takes `creds_secret_name` and `db_root_in_creds` and emits `secretKeyRef` for both ADMIN_PASSWORD and DB_ROOT_PASSWORD.
  - `_build_job_manifest` passes these through.
  - `create_site_task`: Secret apply → Job apply → read Job → re-apply Secret with `ownerReferences`. Any pre-Job exception triggers `_best_effort_delete_secret`.
  - `cancel_site_task`: unchanged Job-delete path, plus a trailing `_best_effort_delete_secret` backstop.
  - New `_best_effort_delete_secret` helper shared by both paths.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`:
  - New `_SITE_NAME_RE` and `_validate_site_name()` called from `validate()`.
  - New `on_trash(self)` that rotates the operation token and enqueues `cancel_site_task` when status is "In Progress" and a `creation_job_name` is set.
- `kubeport/tasks/reconciliation.py`:
  - 404 branch on `read_namespaced_job` now calls `_site_exists_in_bench` and finalizes to Active or Failed.
- `kubeport/tests/test_site_tasks.py`:
  - Expanded `_build_env` tests for the three new cases (ADMIN_PASSWORD secretKeyRef, user DB secret, creds-Secret fallback).
  - New `_build_creds_secret_manifest` tests.
  - New `UnitTestCreateSiteTask` (stale-token exit, happy-path Secret→Job→Secret apply with no plaintext passwords, exception rollback with orphan-Secret cleanup).
  - New `UnitTestCancelSiteTask` (404 tolerance, non-404 logging, Secret cleanup in both).

---

## 2026-04-17 — Frappe Site creation via Kubernetes Jobs

### Context

Kubeport already discovers ERPNext benches (Helm releases of `chart_name == "erpnext"`) and the sites
living on each bench (via exec into running pods). The next capability is creating new sites on those benches.

The ERPNext Helm chart (`frappe/helm`) provides `jobs.createSite` — a standard Kubernetes Job template
(confirmed: no Helm hook annotations) that runs `bench new-site` with templated env vars. The official
mechanism for triggering this job is a `helm upgrade` with `jobs.createSite.enabled: true`.

**That approach was rejected** for this implementation (see Architecture Decision below).
Instead, we submit a Kubernetes Job directly from a new `Frappe Site` DocType — the same way
`Service Bundle` submits raw manifests.

### Architecture decision: Direct Job submission (not Helm upgrade)

Evidence from `frappe/helm` research:

- `job-create-site.yaml` is a *standard* batch/v1 Job, not a Helm hook.
- It is conditionally rendered only when `jobs.createSite.enabled: true`.
- After the Job runs, `enabled` must be toggled back to `false` or it re-fires on the next
  `helm upgrade` — creating a Job name collision (or a duplicate site creation attempt).
- `adminPassword` and `dbRootPassword` would be embedded as plain text in the Helm Release
  `values` field (MariaDB-backed, not encrypted for this use case).

**Consequences of the Helm upgrade path:**

| Problem | Impact |
|---|---|
| Values are desired *steady state* | Toggling `jobs.createSite.enabled` on/off conflates one-off operations with deployment config |
| No clean status tracking | Must watch Job status anyway; Helm abstraction saves nothing |
| Credentials in values YAML | adminPassword + dbRootPassword exposed to any values dump |
| Re-fire risk | Any subsequent `helm upgrade` re-triggers site creation unless the operator manually cleans up |

**Direct Job submission advantages:**

- Reuses `apply_resource()` which already handles `Job` kind
- Job is ephemeral and owned by kubeport, not by the Helm release
- Status is trackable via `BatchV1Api.read_namespaced_job()` in reconciliation
- Fits the existing DocType → background task → reconciliation loop pattern
- `ttlSecondsAfterFinished` auto-cleans completed Jobs

### Design: DocType, background task, and reconciliation

**DocType fields:**
- `site_name`: The site domain (e.g., `erp.example.com`)
- `bench_release`: Link to the target Helm Release (the bench)
- `admin_password`: Site admin password
- `install_apps`: Apps to install (e.g., `erpnext`)
- `db_root_password` or `db_root_secret`: Database credentials
- `status`: Draft → In Progress → Active | Failed
- `creation_job_name`: K8s Job name for reconciliation tracking
- `operation_token`: Concurrency guard

**Background task** (`create_site_task`):
1. Finds a running bench pod via existing discovery utilities
2. Clones its container image and sites PVC mount dynamically
3. Builds and submits a Kubernetes Job manifest
4. Stores the Job name for reconciliation to track

**Reconciliation** (`_reconcile_frappe_sites`):
- Polls `BatchV1Api.read_namespaced_job()` every 5 minutes
- On `status.succeeded > 0`: marks Active
- On `status.failed > 0`: **verifies actual site existence first** (see fix below)

### Key design choice: why dynamic pod inspection for image/volumes

The Job container must use the same image and PVC mounts as the running bench to guarantee
`bench` is available and the correct sites directory is mounted. Hard-coding image tags or PVC
naming conventions from the Helm chart would break on version upgrades or custom values.

By reading the spec from a live reference pod, the task is resilient to chart evolution without code changes.

---

## 2026-04-17 — Fix Frappe Site reconciliation: ground truth over Job exit codes

### Problem

When `bench new-site` exits with non-zero status **despite successfully creating the site**,
Kubernetes sets `job.status.failed = 1`. Our reconciliation would mark the site as Failed,
contradicting the actual cluster state (the site exists and is usable).

This happens frequently in ERPNext when:
- The `--install-app` parameter triggers post-install migrations that emit non-critical warnings
- Asset builds or database setup steps log to stderr
- Some post-creation hook exits non-zero while the site config was already written

### Why this matters

The site directory and `site_config.json` exist on the bench (confirmed by discovery) but
kubeport reports "Failed" — a false negative that confuses operators and requires manual
verification in the cluster.

### Solution: Ground truth verification

When `job.status.failed > 0`:

1. Call `_site_exists_in_bench()` — exec into a live bench pod and check if `site_config.json`
   actually exists (reusing the same discovery utilities).
2. If the file exists → mark site as **Active** (Job exit code was a false negative).
3. Only if the file does not exist → mark as **Failed** and include actual pod log lines.

This reinforces the project invariant: **observed state is always queried live from the cluster**.
Reconciliation bases its decision on ground truth, not on an exit code that lacks semantic meaning.

### Better failure details

`_extract_job_failure_detail()` now fetches the last 30 lines of the failed pod's stdout log
instead of parsing `terminated.message` (which is always empty — we never configured
`terminationMessagePath` in the Job spec). This gives meaningful error output when the site
genuinely fails (e.g., bad database password, app install error).

### Implementation details

- Added `_site_exists_in_bench(site, core_v1) -> bool` helper that uses exec-based discovery
- Modified `elif failed > 0` block in `_reconcile_frappe_sites()` to check site existence first
- Updated `_extract_job_failure_detail()` to fetch pod logs via `read_namespaced_pod_log()`
- Added `bench_release` and `site_name` to the reconciliation query `get_all()` fields

### Testing approach

1. Create a Frappe Site and click Create Site
2. Monitor the Job: `kubectl get jobs -n <namespace> -l app.kubernetes.io/managed-by=kubeport`
3. If the Job pod exits non-zero, wait for reconciliation (5 min) or trigger manually
4. **Expected:** status transitions to **Active** if the site file exists, despite the non-zero exit
5. To test the failure path: create a site with wrong DB root password
6. **Expected:** status transitions to **Failed** with actual `bench new-site` error in the detail
