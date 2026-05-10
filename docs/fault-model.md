# Kubeport — Fault Model

This document enumerates the cluster- and infrastructure-level faults Kubeport tolerates, the mechanism that defends against each fault, the file-level witness for the mechanism, and an upper bound on recovery time. It is the operational companion to the eight numbered properties in [`docs/architecture.md`](architecture.md) §3 and the prose robustness inventory in [`docs/control-plane-state.md`](control-plane-state.md) §Robustness Properties.

A fault listed here is tolerated by design: the system is expected to converge to a correct terminal state without operator intervention within the stated bound. Faults outside this list (e.g. MariaDB data loss, kubeconfig forgery) are out of scope and require manual recovery.

---

## Tolerated faults

| # | Fault | Defended by | Witness | Recovery upper bound |
|---|---|---|---|---|
| F1 | RQ worker crashes mid-Helm-operation, leaving the row stuck in `In Progress` or `Uninstalling`. | Periodic stale-operation reconciler re-runs the live Helm classifier and forces a terminal write once `STALE_OPERATION_THRESHOLD_MINUTES` have elapsed since `operation_started_at`. | `kubeport/utils/constants.py:17`, `kubeport/tasks/reconciliation.py:155`, `kubeport/tasks/reconciliation.py:1862` | `STALE_OPERATION_THRESHOLD_MINUTES + tick_interval` ≤ **35 min**. |
| F2 | Operation Job's `ttlSecondsAfterFinished` expires before the next reconciliation tick reads its terminal status. | Reconciler does not depend on the Job object alone — it falls back to the bench ground-truth probe (`_probe_site_state`) and the PVC-side `<archive>.size` sidecar probe (`_probe_backup_archive_on_pvc`) for backups. | `kubeport/tasks/reconciliation.py:1151`, `kubeport/tasks/reconciliation.py:1245` | One tick after the next probe succeeds; bounded by `2 × tick_interval` = **10 min** in the success path. |
| F3 | Pod-exec call against the bench fails transiently (rolling restart, transient network drop) during a site probe. | Three-state probe returns `SITE_PROBE_UNKNOWN`; the caller defers the status transition rather than committing `Failed`. The next tick retries the probe. | `kubeport/tasks/reconciliation.py:31`, `kubeport/tasks/reconciliation.py:1192` (the `except Exception` branch returning `unknown`), defer call sites at `:568`, `:752`, `:786`, `:865`. | Indefinite while transient persists; **5 min** after the bench is reachable again. |
| F4 | Job-name hash collision or stale `operation_job_name` points reconciliation at an unrelated Job. | `_job_belongs_to_site` / `_job_belongs_to_backup` reject any Job whose `kubeport.io/frappe-site` (or `kubeport.io/frappe-site-backup`) label does not match the expected docname. | `kubeport/tasks/reconciliation.py:1414`, label set on apply at `kubeport/tasks/site_tasks.py:53`. | Synchronous reject inside the tick — wrong-row writes never happen; the correct row recovers via F1 / F2 bounds. |
| F5 | Stale operation lock — the worker is still alive but the operator has clicked Deploy / Cancel again, rotating the desired state. | Controller rotates `operation_token` (128 bits) before each enqueue; the worker (and the reconciler) re-read the token from MariaDB before any state write. Mismatch makes the write a no-op and is logged. | Rotate: `kubeport/kubeport/doctype/helm_release/helm_release.py:141`. Worker re-check: `kubeport/tasks/site_tasks.py:1357`. Reconciler re-check: `kubeport/tasks/reconciliation.py:1820`. | **0 s** — guarded synchronously at every write site. |
| F6 | Worker is hard-killed between applying a `Frappe Site` Job to the cluster and persisting `operation_job_name` on the row. | Orphan-Job sweep runs on every reconciliation tick: lists Jobs labelled `app.kubernetes.io/managed-by=kubeport` in every `(cluster, namespace)` pair that has any site/backup row, and deletes those not referenced by any row's `operation_job_name`. A grace window protects healthy workers' apply→`db_set` window. Self-managed lifecycle Jobs (e.g. `archive-delete`) are skipped via the `kubeport.io/operation` label. | Sweep: `kubeport/tasks/reconciliation.py:1499`. Grace: `kubeport/tasks/reconciliation.py:55` (`_ORPHAN_SWEEP_GRACE_SECONDS = 300`). Self-managed allow-list: `kubeport/tasks/reconciliation.py:1591`. | `_ORPHAN_SWEEP_GRACE_SECONDS + 2 × tick_interval` ≤ **15 min**. |
| F7 | External blocking call hangs — Helm CLI subprocess or `kubernetes`-client request — when the cluster API is unreachable, the kubeconfig points at a black-holed endpoint, or the API server is overloaded. | Every `_run_helm` call is bounded: `_HELM_WORKER_TIMEOUT_SECONDS = 600` for mutations, `_HELM_READ_TIMEOUT_SECONDS = 30` for reads. Every reconciliation-time `kubernetes`-client call passes `_request_timeout` (15 s for Job list/log reads, 10 s for storage-class discovery, configurable seconds for pod listing in observability). On timeout the call raises and the row is left in-flight to be picked up by the F1 reconciler. | Helm constants: `kubeport/utils/helm.py:31`. Helm wrapper: `kubeport/utils/helm.py:512`. K8s-client timeouts: `kubeport/tasks/reconciliation.py:1577` (orphan-sweep Job list), `kubeport/tasks/site_tasks.py:382` (backup-PVC read), `kubeport/utils/observability.py:63` (per-form reads). | Subprocess / API timeout: ≤ **10 min**. Row recovery: F1 bound (≤ **35 min**). |
| F8 | Pod stuck in `ImagePullBackOff` or unable to reach MariaDB — Job will never naturally complete. | Every operation Job carries `activeDeadlineSeconds = _JOB_ACTIVE_DEADLINE_SECONDS = 1800`. The Job flips to `failed` after 30 min, after which the next tick reads the terminal status and writes the row's terminal state via the F2 ground-truth probe. | `kubeport/tasks/site_tasks.py:49`, `kubeport/tasks/site_tasks.py:861`. | `_JOB_ACTIVE_DEADLINE_SECONDS + tick_interval` ≤ **35 min**. |
| F9 | Backup `tar` exits zero but the inode is lost before reconciliation reads it — Job logs claim success while the archive is missing. | Reconciliation does not finalise `Available` on Job exit code or stdout metadata. A short-lived `busybox` Job mounts the `kubeport-backups` PVC and reads the `<archive>.size` sidecar that the bench backup script writes only on a fully-flushed success. The probe is consulted both on the gone-Job recovery path and as post-success verification. Returns `unknown` on probe failure so the caller defers. | `kubeport/tasks/reconciliation.py:1245`. | Per-tick budget bounded by `_BACKUP_PROBE_PER_TICK_LIMIT = 5`; remaining rows defer one tick — **≤ 10 min**. |
| F10 | Direct deletion of a `Frappe Site` row would orphan the real site's database and files on the bench PVC. | `Frappe Site.on_trash` refuses direct deletion of `Active` rows and refuses `Migrating` rows outright; for any other status with a recorded Job it rotates the operation token and enqueues `cancel_site_task` to delete the Job and its credentials Secret. The cascade extends to in-flight backup rows via `_cancel_inflight_backups_for_site`. | Refusal: `kubeport/kubeport/doctype/frappe_site/frappe_site.py:445`. Cascade: `kubeport/kubeport/doctype/frappe_site/frappe_site.py:495`. | **0 s** — synchronous at the trash hook. |

`tick_interval = 5 min` for every cron-driven reconciler — see `kubeport/hooks.py:148`.

---

## Faults explicitly out of scope

The following failure modes are **not** defended by Kubeport. Operator-side disaster recovery applies.

- Loss of the Frappe MariaDB hosting Kubeport itself. Desired-state rows live there; their loss is unrecoverable from inside the application. See [`docs/deploy.md`](deploy.md) (when published) for the control-plane backup procedure.
- Forged or stolen kubeconfigs. Trust is established at the boundary; see [`SECURITY.md`](../SECURITY.md).
- Compromise of the bench pod. The site is treated as ground truth; an attacker with code execution there can mislead the probe.
- CRDs and arbitrary custom resources in `Service Bundle`. Out of scope by design (allowlist of 17 built-in kinds — see `kubeport/utils/k8s_resources.py`).

---

## Cross-references

- [`docs/architecture.md`](architecture.md) §3 — the eight numbered safety / liveness / eventual-consistency properties this document operationalises.
- [`docs/control-plane-state.md`](control-plane-state.md) §Robustness Properties — prose inventory of the same defences in product-feature framing.
- [`AGENTS.md`](../AGENTS.md) — non-negotiable rules for contributors that keep these properties true.
- [`CHANGELOG.md`](../CHANGELOG.md) — architectural decisions that introduced or strengthened the defences listed above.
