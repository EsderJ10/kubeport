# Kubeport Changelog

Architecture decision log for contributors and agents. Each entry records what changed, why the approach was chosen, and what alternatives were rejected.

**Format**: Entries are ordered newest-first. Each entry includes Context (the problem or need), the Decision (what was chosen and why), Rejected Alternatives (what was not chosen and why), and Implementation Details (how it was built).

---

## 2026-05-08 — Helm Release observability drilldown

### Context

Helm Release readiness already identified which rendered resource was failing, but operators still
had to leave Kubeport for the next diagnostic step: pod logs, scoped Kubernetes events, or workload
rollout context.

### Decision

- Added read-only Helm Release observability endpoints that resolve cluster identity from the
  release row, enforce System Manager plus document read access, and validate the requested
  resource against the live Helm manifest before returning diagnostic data.
- Added Kubernetes observability helpers for resource-scoped pod logs, events, and
  Deployment / StatefulSet / DaemonSet rollout context. Log reads are bounded by tail line count
  and response size; the API resolves ownership-proven pods for the resource and returns logs only
  for the selected pod, plus the pod list and default `selected_pod` for the UI.
- Extended the Helm Release readiness panel into an in-form persistent observability panel. Each
  unready row exposes Logs, Events, and Rollout actions; the Logs view ships a pod picker that
  fetches the selected pod on each switch, while Events and Rollout return uniform `{rows, error}`
  payloads so the panel can degrade per-section.
- Added a `pod_count` field to the workload readiness rows so the form can hide the pod picker
  when only one pod backs a resource.
- Kept all observability data ephemeral. No new DocTypes or persisted observed-state fields were
  added.

### Rejected alternatives

- **Persisting logs/events/history.** This violates Kubeport's desired-state versus observed-state
  boundary and would make stale diagnostics look authoritative.
- **Modal dialogs per action.** The first iteration used `frappe.ui.Dialog` per Logs/Events/Rollout
  click; switching pods or actions repeatedly closed and reopened modals. A persistent in-form
  panel keeps the readiness table and diagnostics visible together and survives panel-internal
  refreshes.
- **Fetching every pod log in one request.** This made a single form request scale with workload
  pod count and could stall the web thread on large or unhealthy releases. The API now fetches one
  selected pod per request while still returning the pod list for picker navigation.
- **Adding streaming logs.** Useful later, but larger than the current diagnostic drilldown scope.

### Implementation details

- `kubeport/api/observability.py`: whitelisted read-only endpoints with release-scope
  authorization and manifest membership validation. Events and rollout return `{rows, error}`;
  logs return `{pods, selected_pod, logs_by_pod, errors_by_pod, error}`.
- `kubeport/utils/observability.py`: Kubernetes helpers with request timeouts and per-pod payload
  caps; `list_pods_for_resource` walks owner references for `Deployment` / `StatefulSet` /
  `DaemonSet` / standalone `Pod`.
- `kubeport/utils/release_health.py`: per-workload `pod_count` so the form knows whether to render
  the pod picker.
- `kubeport/kubeport/doctype/helm_release/helm_release.js`: readiness-row Logs / Events / Rollout
  actions render into a persistent in-form observability panel that shares the existing realtime
  refresh channel and ignores stale async responses after operators switch resources, views, or pods.
- `kubeport/tests/test_observability.py` and `kubeport/tests/test_api_observability.py`: utility
  and API coverage for caps, authorization, selected-pod log fetching, `{rows, error}` wrapping,
  and malformed requests.

---

## 2026-05-04 — Backup ground truth, cancel cascade, and Kubernetes Command hardening

### Context

The `feat/frappe-site-backup` branch shipped backup/restore but a primary-source audit surfaced four
themes worth fixing before merge: (1) backup rows whose Job aged out of the cluster were silently
marked `Failed` even when the archive existed on the PVC, (2) cancelling a site mid-backup left the
backup row stuck in `Pending` forever because only the site's `operation_token` was rotated, (3) the
cluster-mutation `cancel_site_task` was queued on `short` instead of `long`, violating the design
rule, and (4) the new `Kubernetes Command` doctype exposed Delete on Secret / PVC / Deployment /
StatefulSet — a privilege surface broader than the diagnostic use cases warrant, with no audit log
distinct from the row itself.

### Decision

- **PVC-side ground-truth probe for backup completion.** Added
  `_probe_backup_archive_on_pvc(backup, api_client, core_v1)` in `kubeport/tasks/reconciliation.py`.
  It submits a short-lived `busybox` Pod with the `kubeport-backups` PVC mounted, reads the
  `<archive>.size` sidecar that the bench backup script already writes only on success, and returns
  `(exists, size_bytes)` / `(missing, None)` / `(unknown, None)` mirroring `_probe_site_state`.
  `_reconcile_site_backup` consults the probe in both branches: gone-Job (replaces the broken
  `size_bytes` heuristic) and post-success (defends against the narrow window where `tar` exits zero
  but the inode is lost before reconciliation reads it).
- **Cancel-cascade to in-flight backups.** `FrappeSite.cancel_site` and `FrappeSite.on_trash` now
  call `_cancel_inflight_backups_for_site(self.name, ...)`, which rotates `operation_token`, sets
  `status = "Failed"` on every backup row linked to the site whose status is `Pending` /
  `In Progress` / `Restoring`, publishes a realtime event, and enqueues `cancel_site_task` for any
  recorded Job. This is what keeps the backup row from being orphaned by site-level cancellation.
- **Long queue for cluster mutations.** `cancel_site_task` is enqueued on `queue="long"` from both
  call sites. Short queue had aggressive timeouts and minimal retries; long queue matches every
  other cluster-mutating background task.
- **Failed-backup archive cleanup.** `FrappeSiteBackup.on_trash` now enqueues archive cleanup
  whenever `storage_path` is set, regardless of status. A backup that partial-wrote an archive then
  failed (e.g., `tar` corruption mid-flush) used to leak the file on the PVC; now it is deleted on
  trash like an `Available` row would be.
- **Explicit operation label for self-managed Jobs.** Added
  `OPERATION_LABEL = "kubeport.io/operation"` and `SELF_MANAGED_OPERATION_VALUES = {"archive-delete"}`
  in `site_tasks`. The orphan sweep skips Jobs carrying these labels so the cleanup path is no
  longer accidentally handled by the grace-window race in the sweep.
- **Tightened `Kubernetes Command` Delete allowlist.** Delete is now restricted to `Pod`, `Job`,
  `ConfigMap` — restartable / recoverable kinds. Secret, PVC, Deployment, StatefulSet, Service are
  excluded from the destructive path; operators go through the proper controllers (Helm Release,
  Service Bundle, Frappe Site) for those. `validate()` now throws on Delete without
  `confirm_destructive` (was a `pass` masquerading as a check).
- **`Kubernetes Command Audit Log` doctype.** Append-only, System Manager read-only. Every execute
  appends a row capturing user, cluster, namespace, action, kind, name, outcome, and an output
  excerpt — decoupled from the source row so the audit trail survives row deletion.
- **Visible output truncation.** `_finalize` now appends a `[... truncated, original N chars ...]`
  marker so an operator debugging a long error message knows the body is incomplete.
- **Backup form realtime listener.** `frappe_site_backup.js` now subscribes to
  `frappe_site_backup_status_update` and reloads on docname match, mirroring the existing listener
  on the `Frappe Site` form.

### Rejected alternatives

- **No probe — defer everything via `unknown`.** Without a probe, the gone-Job branch has no signal
  to recover from; deferring forever is the same as silent loss.
- **Asynchronous probe Job tracked across reconciliation ticks.** Adds a state field on the backup
  row plus two-tick latency. The synchronous probe Pod is bounded (15-second `activeDeadlineSeconds`)
  and gone-Job recovery is rare in practice.
- **Reverting `Kubernetes Command` entirely.** The diagnostic use cases (delete a stuck PVC, list
  pods) are real. Tightening the allowlist plus an audit log is sufficient.

### Implementation details

- `kubeport/tasks/reconciliation.py`: new `_probe_backup_archive_on_pvc`, updated
  `_reconcile_site_backup` signature (now takes `api_client`), orphan sweep recognizes
  `OPERATION_LABEL` + `SELF_MANAGED_OPERATION_VALUES`.
- `kubeport/tasks/site_tasks.py`: added `OPERATION_LABEL`, `SELF_MANAGED_OPERATION_VALUES`;
  `delete_backup_archive_task` tags its Job with `kubeport.io/operation=archive-delete`.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`: `cancel_site` / `on_trash` use
  `queue="long"` and call `_cancel_inflight_backups_for_site`.
- `kubeport/kubeport/doctype/frappe_site_backup/frappe_site_backup.py`: `on_trash` covers Failed
  rows with a stamped `storage_path`.
- `kubeport/kubeport/doctype/frappe_site_backup/frappe_site_backup.js`: realtime listener.
- `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.py`: `_DELETABLE_KINDS`
  allowlist, validate() throws on missing `confirm_destructive`.
- `kubeport/kubeport/doctype/kubernetes_command_audit_log/`: new audit log doctype.
- `kubeport/tasks/kubernetes_command_tasks.py`: `_finalize` appends truncation marker, calls
  `_append_audit_log`.
- Tests added in `kubeport/tests/test_reconciliation.py`,
  `kubeport/tests/test_site_tasks.py`,
  `kubeport/kubeport/doctype/frappe_site_backup/test_frappe_site_backup.py`,
  `kubeport/kubeport/doctype/kubernetes_command/test_kubernetes_command.py`.

---

## 2026-04-30 — Frappe Site backup and restore lifecycle

### Context

`Frappe Site` supported create, drop, and migrate, but `drop-site --no-backup --force` made a mistaken
delete irreversible from inside Kubeport. Operators needed a first-class backup/restore path that preserved
the desired-vs-observed split and kept cluster mutations out of the web thread.

### Decision

- **Standalone backup metadata.** Added `Frappe Site Backup` as a normal DocType, not a child table, so
  `Available` backup metadata can outlive the source `Frappe Site` row.
- **PVC-backed storage for this cycle.** Backup Jobs create archives on a namespace-local RWX
  `kubeport-backups` PVC. `Kubernetes Cluster.backup_storage_class` can override the storage class; blank
  uses the namespace default.
- **Async backup/restore operations.** `backup_site` and `restore_site` rotate the parent site's
  `operation_token`, write backup-row operation metadata, and enqueue long-queue Jobs through the shared
  site operation scaffolding. Restore requires destructive confirmation and reuses the `Migrating` parent
  state.
- **Reconciliation owns final state.** Backup rows become `Available` or `Failed` from Job status and
  archive metadata. Restore completion uses the same functional bench probe as migrate so a false-negative
  Job exit can still recover to `Active`.
- **Orphan sweep recognizes backup Jobs.** The sweep now accounts for both `Frappe Site` and
  `Frappe Site Backup` operation Job names and labels.

### Rejected alternatives

- **Store archives in MariaDB.** Large binary blobs in the desired-state database would blur metadata and
  storage responsibilities.
- **Object storage first.** S3/GCS/Azure support needs credentials, retention, and cross-cluster transfer
  policy; PVC storage is enough to close the immediate no-data-loss gap.
- **Attach backups as a child table.** Child rows would disappear with the parent site row, undermining
  recovery from mistaken deletion.

### Implementation details

- `kubeport/kubeport/doctype/frappe_site_backup/`: new DocType, controller guards, form script, and tests.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`: added `backup_site()` and `restore_site(...)`.
- `kubeport/tasks/site_tasks.py`: added backup/restore commands, backup PVC ensure/mount logic, archive
  delete best effort task, and backup-specific Job labels.
- `kubeport/tasks/reconciliation.py`: added backup/restore reconciliation and extended orphan sweep.
- `kubeport/api/site.py` and `frappe_site.js`: added backup listing, restore action, and backup/restore logs.

### Known follow-ups

Scheduled backups, retention policy, object-store backends, encryption, cross-cluster restore, and
restore-to-different-site-name remain deferred.

---

## 2026-04-30 — Helm Release lifecycle: rollback, uninstall, and manifest-based health

### Context

`Helm Release` rows could be deployed and re-deployed, but the post-deploy lifecycle was thin:
runtime classification was based on `helm status` alone (so a `deployed` release with crash-looping
pods looked healthy), there was no first-class uninstall path (rows could be deleted in any state,
silently orphaning cluster resources), no rollback (operators had to `helm rollback` out-of-band and
then live with the row's desired spec drifting from reality), and no recovery for releases stuck
mid-operation. The deploy worker also had no concurrency guard against a newer operation
superseding it mid-call, so a slow `helm upgrade` could write back stale status over a fresh one.

### Decision

- **Manifest-based readiness walk.** `kubeport/utils/release_health.py:walk` parses the rendered
  output of `helm get manifest`, filters to eight built-in workload kinds (`Deployment`,
  `StatefulSet`, `DaemonSet`, `Pod`, `Job`, `PersistentVolumeClaim`, `Service`, `Ingress`), and
  queries each resource's live status. `classify_release_state` combines Helm runtime state with
  the walker's per-resource readiness: `deployed` + all-ready ⇒ `Deployed`; `deployed` + any
  unready ⇒ `Degraded`; pending/failed Helm states ⇒ `Failed`. Deploy workers and reconciliation
  both call the same classifier so health policy lives in one place.
- **Per-operation tokens for Helm.** `tasks/helm_tasks.py:_release_operation_matches` mirrors the
  site-task pattern: every whitelisted method rotates `operation_token` before enqueue, and the
  worker re-checks both token and status before each writeback (entry, post-Helm-call, finalize).
  If a newer operation has taken over, the worker drops its writeback silently. Same guard in
  `_set_helm_reconciliation_state` so reconciliation cannot overwrite an in-flight operator action.
- **Stale-operation recovery at 30 minutes.** `tasks/reconciliation.py:_reconcile_stale_helm_operations`
  picks up `In Progress` / `Uninstalling` rows older than `_HELM_OPERATION_STALE_SECONDS = 1800`,
  re-checks live Helm state, and either recovers them (re-classifying via the shared classifier) or
  routes uninstall through `_reconcile_stale_uninstall` (which treats "release not found" as success
  and resets the row to `Draft`). Fixed window rather than a worker heartbeat: simpler, no extra
  state, and matches the 5-minute scheduler cadence well.
- **Rollback as a background operation.** `helm_release.py:rollback_release` accepts a target
  revision, validates it, rotates the operation token, and enqueues `helm_tasks.rollback_release`.
  On success the worker writes back the rolled-back chart version and live values into the row's
  desired spec — so the row's desired state matches what's actually running, and the next
  reconciliation tick does not flag drift.
- **Dependency-aware uninstall with a force path.** Normal `uninstall_release` blocks while any
  linked `Frappe Site` is in `Active` / `In Progress` / `Deleting` / `Migrating`, or in `Failed`
  with a Job pointer (the bench may still own state inside the release). The force path requires a
  typed `UNINSTALL <release_name>` confirmation in the UI and records the override in
  `helm_status_detail`. Helm `release: not found` errors are treated as success (idempotency).
- **Direct delete blocked outside `Draft`.** `on_trash` refuses any non-`Draft` row; uninstall is
  the only cleanup path. Same rationale as the site-lifecycle `on_trash` widening: a `Failed` row
  may still own cluster resources and silently dropping it from MariaDB orphans them.
- **Spec-hash drift signal.** `calculate_release_spec_hash` produces a stable hash over chart,
  chart version, namespace, and values; `validate()` recomputes `desired_spec_hash` on every save
  and flags `pending_changes` when it diverges from `last_applied_spec_hash`. The applied hash is
  updated only on successful deploy or rollback, so operators see unsaved intent before the next
  operation.

### Rejected alternatives

- **Trust `helm status` alone for health.** Cheap, but a `deployed` release with crash-looping pods
  or unbound PVCs would report green. The whole point of a control plane is to observe ground
  truth, so the manifest walk is non-negotiable.
- **Maintain a worker heartbeat instead of a fixed staleness window.** More moving parts (heartbeat
  table, expiry sweep) for a problem that a 30-minute timestamp comparison already solves. The
  existing token re-check already handles the "newer op wins" race; staleness is only for genuinely
  stuck workers.
- **Walk arbitrary resource kinds via CRD discovery.** Out of scope; per-CRD health has no general
  semantics. Restricting to the eight built-in kinds keeps the walker predictable and matches what
  the docs already promise.
- **Allow uninstall regardless of linked sites.** Risks orphaning a bench's database/files inside
  the release's PVC. Blocking-by-default + typed force gate gives the operator a deliberate path
  without making the unsafe path easy.
- **Persist per-resource readiness rows.** Violates the "observed state is never persisted" rule
  in `AGENTS.md`. The form drilldown re-queries on demand via `frappe.xcall`.
- **Auto-uninstall when the row is deleted.** Implicit destructive cluster mutation triggered by a
  MariaDB delete is exactly what the desired-state-vs-observed-state separation is meant to avoid;
  uninstall remains an explicit operator action.

### Implementation details

- `kubeport/kubeport/doctype/helm_release/helm_release.py`: added `deploy_release`,
  `uninstall_release` (with `force: bool = False` and blocking-site detection),
  `rollback_release`, `get_release_health`, `load_defaults`, `get_release_history`;
  `validate()` computes `desired_spec_hash` and `pending_changes`; `on_trash` blocks non-`Draft`
  rows; `build_release_docname` scopes identity to cluster/namespace/release; storage validation
  rejects `local-path` + `ReadWriteMany` combinations.
- `kubeport/kubeport/doctype/helm_release/helm_release.js`: status indicators, realtime
  `helm_release_status_update` listener, button-state machine (Install / Upgrade / Retry /
  Redeploy), force-uninstall typed-confirmation dialog, history/rollback dialog with revision
  picker, post-deploy health drilldown, namespace + chart-version autocomplete.
- `kubeport/tasks/helm_tasks.py`: `install_or_upgrade_release`, `rollback_release`,
  `uninstall_release` all routed through `_release_operation_matches` with token+status re-checks
  before each writeback; `_safe_walk` isolates walker errors; `_finalize_uninstall_success` resets
  the row to `Draft`; `_is_release_not_found_error` classifies idempotent uninstall.
- `kubeport/tasks/reconciliation.py`: `_reconcile_helm_releases` (Deployed/Degraded healing),
  `_reconcile_stale_helm_operations` + `_reconcile_stale_uninstall` (30-min staleness window),
  `_set_helm_reconciliation_state` (token-guarded writeback), `_helm_operation_is_stale` window
  check.
- `kubeport/utils/release_health.py`: `walk`, `summarize`, `classify_release_state`,
  `classify_release_from_cluster`, plus per-kind readiness for the eight built-in kinds and
  `_attach_warning_events` for last-N event annotation.
- `kubeport/utils/helm.py`: `install_or_upgrade`, `rollback`, `uninstall`, `status`,
  `get_manifest`, `get_values`, `history`, `show_chart`, `show_values` — all subprocess wrappers
  over a per-call temporary kubeconfig.
- `kubeport/api/discovery.py`: `get_cluster_discovery` annotates each live release with whether a
  tracking row exists; `adopt_helm_release` creates a desired-state row from a discovered release
  with chart version + values baselined.
- Tests added: `test_helm_release.py` (validation, immutability, deploy gate),
  `test_helm_tasks.py` (token staleness for install/rollback/uninstall, repo sync supersession,
  chart inventory), `test_release_health.py` (all eight kinds + warning-event attachment),
  `test_reconciliation.py` (Helm healing + stale-op recovery + uninstall-not-found path),
  `test_discovery.py` (release annotation + adoption).
- Documentation: `README.md`, `docs/codebase-summary.md`, and `docs/control-plane-state.md`
  updated in the same cycle to describe rollback/uninstall/health/reconciliation as shipped.

### Known follow-ups

Helm diff/preview, pod-log and event-history drilldown in the form, application-level HTTP health,
and CRD-aware health remain out of scope (recorded in `docs/control-plane-state.md` Open Gaps).

---

## 2026-04-27 — Frappe Site lifecycle: pre-merge hardening

### Context

Pre-merge audit of `feat/site-lifecycle` surfaced five items: cancelling a running migration is unsafe
because MariaDB DDL is not atomic, the orphan sweep could race the worker's Job apply to `db_set` window,
`on_trash` ignored `Failed` rows that still held a Job pointer, the new `Deleting`/`Migrating`
reconciliation branches lacked direct behavioural tests, and the three site-task functions duplicated most
of their apply scaffolding.

### Decision

- **Confirmation-gated cancel for `Migrating`.** `cancel_site` accepts
  `confirm_destructive: bool = False`; `Migrating` requires it. Status detail records
  "destructive cancel acknowledged". The client uses a typed `CANCEL` dialog, and `on_trash` refuses
  `Migrating` rows so operators must use the gated path.
- **Orphan-sweep grace period.** `_sweep_orphan_site_jobs` skips Jobs younger than
  `_ORPHAN_SWEEP_GRACE_SECONDS` (5 minutes, matching the reconciliation tick), closing the worker
  apply-to-DB race.
- **`on_trash` cleanup widened.** After the `Active` and `Migrating` refusals, cleanup runs whenever
  `operation_job_name` is set, regardless of row status.
- **D1 orchestrator refactor.** `create_site_task`, `delete_site_task`, and `migrate_site_task` are thin
  wrappers over `_run_site_op(...)`. Per-op behavior lives in `SiteOpConfig` plus plain callables for
  command, env, and optional creds Secret construction.
- **H4 behavioural tests.** Direct tests now cover `Deleting` and `Migrating` reconciliation branch
  transitions, orphan-sweep grace behavior, and the shared `_run_site_op` success/supersession paths.

### Rejected alternatives

- **Block cancel for `Migrating` entirely.** Too harsh for genuinely hung migrations; the operator would
  have to wait for `activeDeadlineSeconds`.
- **SIGTERM with a long grace period.** `bench migrate` has no safe DDL-boundary signal handler, so this
  only delays the same risk.
- **Shorter orphan-sweep grace.** A shorter grace would work, but five minutes matches the scheduler cadence:
  a Job younger than one full reconciliation tick is never swept.
- **Object-oriented operation classes.** Heavier than the codebase's plain-function style; dataclass config
  plus callables keeps the operation-specific code explicit and local.
- **Add a `Cancelling` lifecycle state.** Mostly cosmetic and would require new reconciliation branches for
  a state that usually lasts seconds.

### Implementation details

- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`: `cancel_site` now accepts
  `confirm_destructive`, gates `Migrating`, and records destructive acknowledgement in `status_detail`.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.js`: cancelling `Migrating` opens a custom dialog whose
  primary action is disabled until the operator types `CANCEL`; it submits `confirm_destructive: 1`.
- `kubeport/tasks/site_tasks.py`: added `SiteOpConfig`, `_run_site_op`, and
  `_attach_creds_secret_owner_ref`; public create/delete/migrate tasks delegate to the orchestrator.
- `kubeport/tasks/reconciliation.py`: added `_ORPHAN_SWEEP_GRACE_SECONDS = 300` and skips recent orphan
  candidates by Kubernetes `creation_timestamp`.
- Tests: added controller confirmation tests, orchestrator tests, orphan-sweep age tests, and direct
  behavioural tests for delete/migrate reconciliation.

### Known follow-ups

D2 (terminal `Deleted` state for audit trail), D3 (force-drop for `Failed` without a Job), D4 (`Cancelling`
state), H5 (migrate pre-flight bench probe), and H6 (maintenance-mode wrapping) remain out of scope for this
cycle.

---

## 2026-04-26 — Frappe Site: post-creation lifecycle (delete + migrate)

### Context

Once a `Frappe Site` row reached `Active`, the control plane had no way to act on the bench-side site. `on_trash` only cleaned up *in-flight* creation Jobs (it early-returned unless `status == "In Progress"`), so deleting an `Active` row silently orphaned the real site (database + files) on the bench PVC. The only remediation was to `kubectl exec` into a bench pod and run `bench drop-site` by hand — defeating the point of a control plane that creates resources but cannot destroy or maintain them. Schema migrations were similarly out-of-band.

### Decision

- **Two new in-flight states**: `Deleting` and `Migrating`. Lifecycle is now `Draft → In Progress → Active | Failed`, plus `Active → Migrating → Active | Failed` and `Active|Failed → Deleting → [doc deleted] | Failed`.
- **`delete_site()`** whitelisted method: rotates the operation token, sets `status="Deleting"`, enqueues `delete_site_task`. The task submits a Kubernetes Job running `bench drop-site --no-backup --force` using the same reference-pod-clone pattern as `create_site_task`. Reconciliation polls the Job, runs the existing three-state bench probe, and on a confirmed-missing site calls `frappe.delete_doc` to remove the row itself. On still-present, the row lands `Failed` with the Job logs in `status_detail`.
- **`migrate_site()`** whitelisted method: same pattern, runs `bench --site $SITE_NAME migrate`. No creds Secret needed (bench reads from `site_config.json`). Reconciliation runs the functional probe; non-zero Job exit with a still-functional site recovers to `Active` (false-negative tolerance).
- **`on_trash` refuses `Active` rows** with a message directing the operator to "Delete Site" first. For in-flight rows it still rotates the token and enqueues the existing `cancel_site_task` to delete the K8s Job.
- **`cancel_site()` extends to all three in-flight states** with operation-aware status detail messages.
- **`_build_job_manifest` becomes `_build_op_job_manifest`** — a generic Job-shape builder taking `operation_label`, `container_command`, and `container_env`. Each operation task assembles its own command and env, keeping per-op concerns local while sharing the pod-spec-clone path.
- **Reconciliation dispatches by status** via `_reconcile_site_create` / `_reconcile_site_delete` / `_reconcile_site_migrate`. `_finalize_site_status` gains an `expected_status` parameter so it guards `Deleting` and `Migrating` writes the same way it guarded `In Progress`. New `_finalize_site_deletion` deletes the row under the same token guard.

### Rejected alternatives

- **Auto-cascade `on_trash` to `delete_site` for Active rows.** Mixes async lifecycle into row deletion: the row would sit there until the Job confirms, and operators clicking "Delete" expect immediate disappearance. The explicit "Delete Site" button is unambiguous and keeps the cluster effect visible.
- **Transition the row to `Draft` after a successful drop (Helm Release "Uninstalling → Draft" pattern).** A Helm Release row is a reusable template — the deployment is meant to be redeployable. A `Frappe Site` row identifies a specific site; once dropped, the row is operational debris. Auto-deletion matches user intent.
- **Default `bench drop-site` (with backup).** Leaves an unindexed SQL dump on the bench PVC with no way to expose it (we have no backup-restore feature yet). `--no-backup` keeps the PVC clean. Backup will come as a first-class operation with proper storage handling.
- **Backup/restore in this PR.** Substantially larger surface — needs a backup storage strategy (PVC vs object store), a child DocType for backup runs, and file-upload plumbing. Deferred to a dedicated cycle.
- **Rename `creation_job_name`/`creation_job_token` to drop "creation_".** The fields now hold the *current operation*'s Job, not specifically a creation Job. Renaming requires a schema patch and is orthogonal to the lifecycle work; deferred.

### Implementation details

- `kubeport/kubeport/doctype/frappe_site/frappe_site.json`:
  - `status` options gain `Deleting` and `Migrating`.
  - New `migrate_site_btn` and `delete_site_btn` (danger color) buttons. `cancel_site_btn` relabelled to a generic "Cancel".
  - `creation_job_name` / `creation_job_token` descriptions broadened to "Operation Job…" semantics.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`:
  - `status` `DF.Literal` extended.
  - New `delete_site()` and `migrate_site()` whitelisted methods, each rotating the operation token and clearing the prior `creation_job_*` pointers before enqueueing.
  - `cancel_site()` accepts `In Progress | Deleting | Migrating`; status_detail spells out which operation was cancelled.
  - `on_trash` throws on `Active`; in-flight cleanup branch widened to all three in-flight statuses.
- `kubeport/kubeport/tasks/site_tasks.py`:
  - `_build_job_manifest` → `_build_op_job_manifest(..., operation_label, container_command, container_env, ...)`. Per-op command and env are now built by the caller.
  - New `delete_site_task` and `migrate_site_task` mirroring `create_site_task` (token-guard pre and post-apply, exception cleanup, realtime events).
  - New helpers: `_bench_drop_site_command`, `_bench_migrate_command`, `_build_drop_env`, `_build_drop_creds_secret_manifest`. Drop-site and migrate never touch admin credentials; migrate touches no credentials at all.
- `kubeport/kubeport/tasks/reconciliation.py`:
  - `_reconcile_frappe_sites` filters on `status IN (In Progress, Deleting, Migrating)` and dispatches to per-op handlers.
  - New `_reconcile_site_delete` / `_reconcile_site_migrate` / `_apply_delete_probe` helpers; existing creation logic moved into `_reconcile_site_create`.
  - `_finalize_site_status` gains `expected_status` parameter (callers updated; existing behavioural tests updated).
  - New `_finalize_site_deletion` calls `frappe.delete_doc(..., ignore_permissions=True, force=True, delete_permanently=True)` after publishing a `frappe_site_status_update` event with `status="Deleted"`.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.js`:
  - Status indicator map covers all six states.
  - Per-status button visibility (`Migrate Site` only for `Active`; `Delete Site` for `Active` and `Failed`-with-Job; `Cancel` during any in-flight; relabels per operation).
  - Realtime listener detects `status="Deleted"` and routes to the list view instead of attempting `reload_doc` on a 404.
- `kubeport/kubeport/api/site.py`: `get_site_job_logs` docstring broadened to current-operation Job.
- Tests: `_build_job_manifest` tests rewritten to use the new generic builder via a `_create_manifest` helper; new tests for drop-site command, migrate command, drop env (no admin password), drop creds Secret shape, container `name=operation_label`, and operation-token guard for `Deleting`/`Migrating`.
- Docs: `docs/control-plane-state.md` capabilities + open gaps + robustness table updated; README capability bullet broadened to "Frappe Site Lifecycle".

### Known follow-ups

- Reconciliation behavioural tests for the new `Deleting` and `Migrating` branches (mock-based, similar to existing creation reconciliation tests). Existing creation tests still cover the dispatch+token-guard path; explicit Deleting/Migrating cases would tighten the guarantee.
- Field rename (`creation_job_*` → `operation_job_*`) once a quiet window for a schema patch presents itself.
- Backup/restore as the next dedicated cycle.

---

## 2026-04-23 — Frappe Site: orphan-Job sweep, hang timeout, label-guarded reconciliation, 3-state bench probe

### Context

Pre-merge audit of `feat/frappe-site-provisioning` surfaced five robustness gaps that the prior rounds did not close:

1. A worker hard-killed (OOM, node drain, SIGKILL) between `apply_resource(job)` and `db_set("creation_job_name", ...)` leaves a real Job running with no DocType pointer. Reconciliation filters by non-empty `creation_job_name`, so the Job is invisible; the PVC side-effect persists until a human notices.
2. A Job stuck in `ImagePullBackOff` / unable to reach DB never flips `succeeded` or `failed`, so reconciliation's polling leaves the row `In Progress` forever.
3. Reconciliation reads the Job purely by name; a stale `creation_job_name` (or the unlikely name collision) would let us finalize the wrong row's status.
4. A rolling bench restart during the Job-failed or TTL-expired branch causes the exec-based ground-truth probe to raise, which the old code translated to `False` → terminal `Failed` — even though the site might be perfectly fine.
5. The `db_type` UI option advertised `postgres`, but the bench command always used `--mariadb-root-*` flags and the superuser was hardcoded to `"postgres"` with no way to override. It had never been validated on a real postgres-backed bench.

### Decision

- **Orphan-Job sweep.** A new `_sweep_orphan_site_jobs()` runs at the end of every 5-minute reconciliation tick. For each cluster/namespace pair that has at least one `Frappe Site` row it lists Jobs labeled `app.kubernetes.io/managed-by=kubeport,kubeport.io/frappe-site` and deletes any whose names do not appear in any row's `creation_job_name`. Uses the existing `_best_effort_delete_job` with Background propagation so the creds Secret is GC'd via ownerRef in the same sweep.
- **`activeDeadlineSeconds` on every site-creation Job.** Defaults to 30 minutes (`_JOB_ACTIVE_DEADLINE_SECONDS`). Enough headroom for realistic `bench new-site --install-app=erpnext` on modest hardware, tight enough that genuine hangs surface before an operator notices.
- **Label-guarded reconciliation.** Before finalizing status on any Job, `_reconcile_frappe_sites` calls `_job_belongs_to_site` to confirm the Job's `kubeport.io/frappe-site` label matches the doc's `_safe_label_value(docname)`. Mismatch → log and skip. Never touches the row.
- **Three-state bench probe.** `_site_exists_in_bench` becomes `_probe_site_state` and returns `SITE_PROBE_EXISTS` / `SITE_PROBE_MISSING` / `SITE_PROBE_UNKNOWN`. Transport-level exec failures (pod selection failure, stream errors) surface as `unknown`; the caller defers the status transition to the next tick instead of writing `Failed`. `_exec_bench_site_functional` now re-raises instead of swallowing exec errors so the distinction is possible.
- **Hide postgres from the UI for now.** `frappe_site.json` removes `postgres` from the `db_type` options. The `_build_env` / `_bench_new_site_command` postgres branches stay in place for forward compatibility but are only reachable by direct DB write until the flow is plumbed correctly and validated on a real postgres bench.

### Rejected alternatives

- **Reserve `creation_job_name` before applying the Job.** Rejected again for the same reason as in the 2026-04-22 entry: phantom names on rows for Jobs that do not yet exist. The label-based sweep reaches the same orphan Jobs without the inconsistency window.
- **Shorter TTL (`ttlSecondsAfterFinished`) instead of `activeDeadlineSeconds`.** TTL only fires once the Job completes. It does not help a Job that is still hung — the very case we need to bound.
- **Plumb postgres correctly in this PR.** Requires a postgres-backed bench in CI or at least a known-good smoke run. Neither is available this cycle; shipping a visible but broken option is worse than shipping a narrower feature.

### Implementation details

- `kubeport/tasks/site_tasks.py`:
  - New module-level constants `_JOB_ACTIVE_DEADLINE_SECONDS`, `SITE_DOC_LABEL`, `MANAGED_BY_LABEL`, `MANAGED_BY_VALUE`.
  - `_build_job_manifest` adds `spec.activeDeadlineSeconds = _JOB_ACTIVE_DEADLINE_SECONDS` and uses the label constants. `_build_creds_secret_manifest` also uses the label constants so the sweep's selector is guaranteed consistent with what the worker writes.
- `kubeport/tasks/reconciliation.py`:
  - New `SITE_PROBE_EXISTS` / `SITE_PROBE_MISSING` / `SITE_PROBE_UNKNOWN` constants.
  - `_site_exists_in_bench` → `_probe_site_state` (3-state return). Exec transport failures return `unknown`.
  - `_exec_bench_site_functional` now raises instead of swallowing exceptions.
  - New `_job_belongs_to_site(job, site)` called in `_reconcile_frappe_sites` before any status write.
  - New `_sweep_orphan_site_jobs()` wired into `reconcile_all_releases`.
  - Both the Job-failed branch and the 404-TTL branch call `_probe_site_state`; `SITE_PROBE_UNKNOWN` defers to the next tick instead of finalizing.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.json`:
  - `db_type` options narrowed from `mariadb\npostgres` to `mariadb`.
- `kubeport/tests/test_site_tasks.py`:
  - New `test_manifest_sets_active_deadline_seconds`.
- `kubeport/tests/test_reconciliation.py`:
  - Existing Job-failed tests switched from `_site_exists_in_bench` to `_probe_site_state` with `"exists"` / `"missing"` return values, and all `_reconcile_frappe_sites` tests now patch `_job_belongs_to_site` to bypass label inspection for `SimpleNamespace` fakes.
  - New `test_reconcile_frappe_sites_skips_job_with_wrong_site_label`.
  - New `test_reconcile_skips_finalize_on_transient_probe_failure`.
  - New `UnitTestJobBelongsToSite` (4 cases).
  - New `UnitTestSweepOrphanSiteJobs` (tracked-name preserved, orphan deleted, empty-creation-job-name still swept).
  - `test_reconcile_all_releases_only_runs_active_sweeps` extended with `_sweep_orphan_site_jobs` assertion.
- `docs/frappe-site-smoke.md`: new real-cluster smoke procedure (8 scenarios) — worker-crash recovery (sweep), hung-pod (activeDeadlineSeconds), transient bench restart, etc.
- `docs/control-plane-state.md`: updated capabilities, robustness table, and "Site Lifecycle" gap for postgres.

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
