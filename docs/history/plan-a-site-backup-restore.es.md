# Plan A — Backup / Restore de Frappe Site

> Version parcialmente traducida al espanol. Se conservan comandos y terminos tecnicos.

## Proposito

`Frappe Site` cubre create, delete y migrate. `delete_site_task` ejecuta `bench drop-site
--no-backup --force`, que es realmente irreversible: un misclick en la UI destruye de forma
permanente la base de datos y los archivos del site. Plan A cierra ese hueco dando a cada
`Frappe Site` un lifecycle de backup/restore que replica los patrones ya probados para
create/delete/migrate.

El objetivo del ciclo es una **operacion sin perdida de datos**: un operador debe poder
recuperarse de un `delete_site` accidental (o una migracion corrupta) en minutos, restaurando el
backup mas reciente sin salir de la UI de Kubeport.

Fuera de alcance para este ciclo (deferred): backups programados/automaticos, backends de
object-store (S3/GCS/Azure Blob), restore cross-cluster y politicas de retencion mas alla de un
boton manual de delete-old-backup.

## Encaje arquitectonico

Backup/restore son operaciones que mutan cluster sobre `Frappe Site`, por lo que siguen las mismas
reglas que el lifecycle existente del site (invariantes en `AGENTS.md`, resumen en `CLAUDE.md`):

- All cluster mutation goes through background jobs (`kubeport/tasks/site_tasks.py`).
- Each operation rotates the site's `operation_token`; workers re-check before any writeback.
- Backups are **observed state of a past point in time** persisted to MariaDB only as metadata
  (timestamp, size, location, status). The actual archive is a file on the chosen storage
  backend, not a database blob.
- Reconciliation polls the Job, runs the existing three-state bench probe, and finalizes the row.
- Reuse `_run_site_op` (`kubeport/tasks/site_tasks.py:SiteOpConfig`) for both backup and restore
  operations — they fit the existing orchestrator.

## Alcance

### New DocType

**`Frappe Site Backup`** — child-style record (one site → many backups). Fields:

| Field | Type | Notes |
|---|---|---|
| `frappe_site` | Link → Frappe Site | Required, immutable. |
| `backup_name` | Data | Auto-generated: `<site>-<utc-timestamp>`. |
| `status` | Select | `Pending`, `In Progress`, `Available`, `Restoring`, `Failed`. |
| `started_at` | Datetime | Set when worker enqueues. |
| `completed_at` | Datetime | Set on terminal state. |
| `size_bytes` | Long Int | Filled from worker stat after archive lands. |
| `storage_backend` | Select | `pvc` only this cycle; field exists for forward compat. |
| `storage_path` | Data | Absolute path inside the backup PVC, or object key in a future cycle. |
| `bench_archive_name` | Data | Filename `bench backup` produced (for restore). |
| `operation_token` | Data, hidden, read-only | Same pattern as `Frappe Site`. |
| `operation_job_name` | Data | Kubernetes Job name, cleared on terminal state. |
| `operation_started_at` | Datetime | Used by reconciliation for staleness check. |
| `status_detail` | Small Text | Job logs / failure reason, capped at 500 chars. |
| `triggered_by` | Link → User | For audit trail. |

`autoname`: `<site-docname>::<utc-timestamp-iso8601>`.

`on_trash` rules: refuses `In Progress` / `Restoring` rows; `Available` rows allow delete which
also fires a worker to `rm` the underlying archive (best-effort; failure logs but does not block
row delete). `Failed` and `Pending` rows allow direct delete.

### Cluster artifacts

- One **backup PVC per cluster**, named `kubeport-backups`, RWX. Provisioned on first backup if
  missing (the worker apply path; reuse `kubeport/utils/k8s.py` apply helpers). Storage class
  defaults to the cluster's default; admin can override per cluster via a new
  `Kubernetes Cluster.backup_storage_class` field (Data, optional).
- Backup Jobs mount this PVC at `/mnt/kubeport-backups` and are scoped to the site's namespace
  via the existing reference-pod-clone pattern (`_clone_reference_bench_pod` in
  `site_tasks.py`).

### Whitelisted methods (`Frappe Site` controller)

- `backup_site() -> dict` — rotates `operation_token`, sets `status="In Progress"`, creates the
  child `Frappe Site Backup` row in `Pending`, enqueues `backup_site_task`. Returns the new
  backup's docname.
- `restore_site(backup_docname: str, confirm_destructive: bool = False) -> dict` — destructive
  on the running site (drops + restores). Requires `confirm_destructive=True`; client opens a
  typed `RESTORE <site>` dialog like the migrate cancel path. Rotates the token, sets
  `status="Migrating"` (re-using the in-flight state), enqueues `restore_site_task`.

### Tasks

- **`backup_site_task(site_docname, backup_docname, operation_token, ...)`**:
  - Runs `bench --site $SITE backup --with-files`. Output lands in
    `sites/$SITE/private/backups/`. Worker `cp` it onto the mounted PVC under
    `/mnt/kubeport-backups/<cluster>/<namespace>/<site>/<backup_name>.tar.gz`.
  - Reconciliation polls the Job; on success the worker `stat`s the archive and writes
    `size_bytes`, `bench_archive_name`, `storage_path` onto the backup row, sets the row's
    status to `Available`, and resets the parent site to `Active` (token-guarded as elsewhere).
  - Failure path: parent site to `Active` (the failure was on the backup, not the site itself);
    backup row to `Failed` with logs in `status_detail`.

- **`restore_site_task(site_docname, backup_docname, operation_token, ...)`**:
  - Runs `bench --site $SITE restore /mnt/kubeport-backups/<path>` (with appropriate flags to
    overwrite). Mounts the same backup PVC.
  - On success: parent site → `Active`, backup row → `Available`. On failure: parent site →
    `Failed`, backup row → `Available` (the backup itself is fine, only the restore failed).
  - Treats partial-restore correctly: reconciliation runs the existing functional bench probe
    after the Job finishes; if the bench reports the site present and functional, success
    regardless of Job exit (same pattern as `migrate_site` false-negative tolerance).

### Reconciliation

- Add a reconciliation branch for `Frappe Site Backup` rows in `In Progress` / `Restoring` older
  than `_FRAPPE_SITE_OPERATION_STALE_SECONDS` (existing constant). On staleness, check the Job;
  if missing or completed, re-finalize via the bench probe; if still running, leave alone.
- Extend the orphan-Job sweep
  (`reconciliation.py:_sweep_orphan_site_jobs`) to recognize the new
  `kubeport.io/frappe-site-backup` label and clean up unreferenced backup/restore Jobs older
  than the existing 5-minute grace window.

### UI (`frappe_site.js`)

- "Backup now" button on the site form (always enabled when status is `Active`).
- "Backups" panel listing the child `Frappe Site Backup` rows, newest first, with per-row status
  indicator and a `Restore` action that opens the typed-confirmation dialog.
- Live status updates via the existing `frappe.realtime` channel; new event name
  `frappe_site_backup_status_update`.

### Tests

- `test_frappe_site.py`: `backup_site` rotates token; refuses to enqueue if a backup is already
  in flight; `restore_site` requires `confirm_destructive=True`.
- `test_frappe_site_backup.py` (new): backup row autoname, `on_trash` rules, child-side status
  transitions.
- `test_site_tasks.py`: `backup_site_task` and `restore_site_task` token-staleness,
  reference-pod-clone success and failure paths, archive-not-found fallback.
- `test_reconciliation.py`: stale-backup recovery, orphan-Job sweep recognizing the new label,
  backup-Job-missing-after-success path (Job TTL'd before reconciliation read).
- `test_helm_release.py` regression: uninstall blocking now also has to consider linked sites
  with in-flight backups (a backup in progress holds the site in a non-`Active` state, which the
  existing blocking-site detection already covers, but worth a direct test).

## When implementation is fulfilled

The cycle is done when **all** of the following hold:

1. **Lifecycle works end-to-end on a real cluster.** From a fresh `Active` `Frappe Site` row,
   the operator can: click `Backup now` → see a new backup row reach `Available` with non-zero
   `size_bytes` → click `Restore` on it → confirm → see the parent site return to `Active` with
   the data the backup captured. Verified inside the dev container with at least one full
   round-trip.
2. **Concurrency invariants honored.** A second `backup_site` while one is in flight is rejected
   client-side and server-side. Token rotation guards a stale worker from writing back if a
   newer operation supersedes it. Direct unit tests for both paths.
3. **Data-loss-safety property.** A delete of a `Frappe Site` whose only `Available` backup row
   is on a PVC that survives the site delete: a fresh site can be created and the backup
   restored onto it. (Backup PVC lifecycle is decoupled from the site PVC by design.)
4. **Reconciliation catches stuck operations.** Direct test that a `Frappe Site Backup` row
   marked `In Progress` past the staleness window, with its Job already terminal, transitions
   to `Available` or `Failed` based on the live archive presence and bench probe.
5. **Orphan sweep recognizes the new label.** Direct test that a `kubeport.io/frappe-site-backup`
   labeled Job not referenced by any backup row is deleted after the grace window, and that one
   younger than the grace window is skipped.
6. **Five test suites green.** `Frappe Site`, `Frappe Site Backup`, `test_site_tasks`,
   `test_reconciliation`, `test_helm_release`. No new skipped tests. No regressions in the
   existing `test_release_health` / `test_discovery` / `test_helm_tasks` suites.
7. **Docs updated in the same cycle.** `README.md` (feature list), `docs/codebase-summary.md`
   (new module pointers), `docs/control-plane-state.md` (move site backup/restore from "Open
   Gaps" to "Capabilities"), `CHANGELOG.md` (new ADR-style entry following the project format),
   and `AGENTS.md` if any new invariant lands (e.g., "backup PVC lifecycle is independent of
   site PVC").
8. **No new TODO/FIXME/skip markers** in any file changed by the cycle.

## Critical files

- New: `kubeport/kubeport/doctype/frappe_site_backup/` (DocType + controller + JS + tests).
- Modify: `kubeport/kubeport/doctype/frappe_site/frappe_site.py` (whitelisted methods),
  `frappe_site.js` (UI), `kubeport/tasks/site_tasks.py` (new task functions, extend
  `_run_site_op` if needed), `kubeport/tasks/reconciliation.py` (backup-row branch + extended
  orphan sweep), `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.json` (add
  `backup_storage_class`).
- Tests: extend `kubeport/tests/test_site_tasks.py`, `test_reconciliation.py`, add
  `test_frappe_site_backup.py`.

## Known follow-ups (explicitly deferred)

- Object-store backends (`s3://`, `gs://`, `az://`) via a pluggable
  `BackupStorage` interface.
- Scheduled backups (cron-style) per site, with retention policy and automatic prune.
- Cross-cluster restore (download + re-upload + restore in another cluster's PVC).
- Restore to a *new* site (new release) rather than overwriting the source site.
- Encryption-at-rest for backup archives.
