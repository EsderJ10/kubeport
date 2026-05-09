# Control Plane State

## Executive Summary

Kubeport operates as a real Kubernetes control plane: desired state is stored in Frappe DocTypes, while live cluster state is queried on demand. The implementation can connect to clusters, discover live releases and Frappe sites, deploy Helm releases, apply raw manifests, provision Frappe sites via Kubernetes Jobs, and reconcile persisted state against the cluster on a 5-minute schedule.

The current milestone is **robustness** — making discovery, background execution, and reconciliation reliable under imperfect cluster conditions.

---

## Capabilities

### Cluster Connectivity

- Three authentication modes: kubeconfig, bearer token, in-cluster service account.
- Connection testing against the live Kubernetes API.
- Dev-only TLS verification bypass for kubeconfig and bearer-token clusters.
- Bearer-token auth is strict by default: requires CA certificate unless dev-only TLS bypass is enabled.
- Live namespace discovery reused by forms needing cluster namespace lists.
- Browser-driven kubeconfig upload with context parsing, extraction, and endpoint normalization for containerized dev setups (converts `0.0.0.0` / `127.0.0.1` / `localhost` to the container gateway IP).

### Helm Chart Catalog

- Helm repository registration and background chart sync.
- Full chart/version inventory rebuild from `helm search repo --versions`.
- Stale chart pruning when charts disappear upstream.
- Per-run sync tokens preventing stale workers from overwriting newer syncs.
- Optional `include_patterns` (comma-separated globs) to filter synced charts.
- Cached default `values.yaml` per chart, cleared when the latest version changes.

### Site Image Catalog

- Kubeport ships a DB-backed `Kubeport Site Image` catalog seeded from `kubeport/site_images/catalog.json`.
- Catalog rows are desired/product metadata, not observed cluster state: repository, release tag, digest, Frappe major, ERPNext version, apps hash, source revision, default/deprecated status, and display-only included apps.
- Each row carries an `is_curated` flag. Curated rows (`1`) are owned by the daily catalog sync; user-registered rows (`0`) persist independently. Sync only writes curated rows and skips entries that collide with a user-registered repository:tag (logging a warning), so user-registered images are never overwritten.
- Defaults (`is_default=1`) are reserved for curated rows; user rows can be selected on Helm Release but cannot be marked default.
- Curated active rows must record the pushed GHCR digest. The shipped `0.0.1-frappe16` row is deprecated until a real public GHCR image exists and its digest is recorded.
- The Frappe-native `owner` field records who created each row; no parallel "origin/user" field is maintained.
- Deletion is blocked when a Kubeport Site Image is linked to a Helm Release, and curated rows cannot be deleted at all (mark them `Deprecated` instead).
- Catalog sync runs through a long-queue task on install/migrate and the daily scheduler.
- Public GHCR images are the v1 registry scope; no imagePullSecret management is exposed in the UI. Frappe v16 is the v1 supported major. Helm value injection assumes ERPNext-style charts (`image.repository`, `image.tag`, `image.pullPolicy`).
- Kubeport does not run Docker builds or store GHCR PATs. The site image is built and pushed by GitHub Actions using `GITHUB_TOKEN`, SBOM/provenance settings, and registry attestations.

### Helm Release Management

- Desired release state scoped to `cluster/namespace/release_name`.
- Optional `site_image` links an ERPNext/Frappe bench release to a runtime image. Deploy workers render the selected catalog row into Helm values as `image.repository`, `image.tag`, and `image.pullPolicy=IfNotPresent`; when `image_digest` is recorded, `image.tag` is rendered as `tag@sha256:...` so Kubernetes pulls the exact pushed image digest through the chart's existing `repository:tag` template.
- Manual YAML remains available, but conflicting `values.image.repository`, `values.image.tag`, or `values.image.pullPolicy` entries are rejected while `site_image` is set.
- When `site_image` is set and the user's values do not specify `persistence.worker.storageClass`, the deploy worker discovers the cluster's default StorageClass (the one annotated `storageclass.kubernetes.io/is-default-class: "true"`) and injects it. User-supplied storage classes always win. When the discovered class is `local-path` (or another known RWO-only provisioner) and the user has not set `accessModes`, `accessModes: ["ReadWriteOnce"]` is also injected so the chart's RWX default can still schedule on k3s/local-path setups. Discovery is read-only — the result is not persisted to MariaDB. If no default-class annotation exists and the user has not set the key, the deploy fails fast with an actionable message instead of bubbling up the ERPNext chart's `required` template error.
- Pending-change hashes include the selected Site Image and digest, so digest changes are treated as deployable desired-state changes.
- Idempotent deploy via `helm upgrade --install` in background jobs.
- Uninstall via `helm uninstall` in background jobs.
- Release rows track desired spec hash vs. last-applied spec hash so operators can see saved pending changes before the next install/upgrade.
- Per-run operation tokens and status re-checks prevent stale deploy/uninstall workers from writing after a newer operation takes over.
- In-flight Helm operations record operation type and start time. Reconciliation recovers stale `In Progress` / `Uninstalling` rows after 30 minutes by checking live Helm status and readiness.
- Direct row deletion is allowed only from `Draft`; `Failed` rows are treated as potentially resource-owning and must be uninstalled first. If Helm reports the release is already gone during uninstall, Kubeport treats cleanup as successful and returns the row to `Draft`.
- Uninstall is dependency-aware: linked `Frappe Site` rows in active/in-flight states block normal uninstall, with a typed force-uninstall path for explicit operator override.
- Post-deploy and reconciliation health combine Helm runtime state with a rendered-manifest readiness walk (`Deployment`, `StatefulSet`, `DaemonSet`, `Pod`, `Job`, `PersistentVolumeClaim`, `Service`, `Ingress`). `deployed` plus all checked resources ready becomes `Deployed`; `deployed` plus unready resources or readiness probe failure becomes `Degraded`; pending/non-deployed Helm states become `Failed`.
- The Helm Release form exposes workload-readiness drilldown as read-only observed state. Per-resource rows are not persisted.
- Each unready readiness row opens an in-form observability panel with three sub-views: pod logs (resource-scoped pod list with one selected pod fetched per request, bounded by tail-line count and response size), scoped Kubernetes events, and Deployment / StatefulSet / DaemonSet rollout context. The panel is read-only, ephemeral, gated on System Manager plus document read access, and ignores stale async responses when operators switch resources, views, or pods.
- The Helm Release form exposes live release history and queues rollback as a background operation. A successful rollback updates the desired values/chart version to the selected live revision.
- Status lifecycle: `Draft` → `In Progress` → `Deployed` / `Degraded` / `Failed` → `Uninstalling` → `Draft`.
- Realtime events trigger form refresh on status changes.

### Service Bundle (Raw Manifests)

- Raw Kubernetes manifests validated against a fixed resource kind allowlist (17 built-in kinds).
- Server-side apply (idempotent) and deletion in background jobs.
- Per-run operation tokens and `Deleting` state for concurrency safety.
- Reconciliation checks resource existence and flags missing resources as `Degraded`.

### Frappe Site Provisioning

- `Frappe Site` DocType links to a Helm Release (the target bench).
- Background jobs run site lifecycle operations (`bench new-site`, `bench drop-site`, `bench migrate`, `bench backup`, `bench restore`) by submitting a Kubernetes Job whose pod template is cloned from a live bench workload pod (image, sites PVC mount, env, pod-level fields). A single `_build_op_job_manifest` builder is reused across operations; only the bench command and per-op env differ.
- Site lifecycle operations run through a single `_run_site_op` orchestrator: per-op behavior is supplied by a small `SiteOpConfig` dataclass plus plain callables (`build_command`, `build_env`, `build_creds_secret`). This is the single source of truth for the apply pipeline, post-apply token re-check, and exception cleanup.
- Reconciliation verifies actual site existence on the bench (via exec-based discovery checking for `site_config.json` and `bench list-apps`) rather than trusting Job exit codes — avoids false negatives. The probe is three-state (`exists` / `missing` / `unknown`); transient bench-exec failures return `unknown` and defer the status transition to the next tick instead of marking the site `Failed`.
- **Lifecycle states**: `Draft → In Progress → Active | Failed`, plus `Active → Migrating → Active | Failed` and `Active|Failed → Deleting → [doc deleted] | Failed`. Cancellation rotates the operation token, marks the row `Failed`, and best-effort deletes the Job. Cancelling `Migrating` is confirmation-gated because killing `bench migrate` can leave MariaDB schema changes half-applied.
- **Deletion (`bench drop-site`)** runs with `--no-backup --force`. On a confirmed-missing probe, reconciliation removes the `Frappe Site` row itself via `frappe.delete_doc`. On a still-present probe (drop-site Job ran but the bench still has the site), the row lands in `Failed` with the Job logs in `status_detail` so the operator can retry.
- **Migration (`bench migrate`)** does not need credentials — bench reads them from `site_config.json`. Reconciliation runs the same functional probe used for creation; if the Job exit code is non-zero but the site is still functional (false negative), the row recovers to `Active`.
- **Backup/restore** uses standalone `Frappe Site Backup` rows. Backup Jobs run `bench backup --with-files`, package the generated files into a tar archive, and store it on a namespace-local `kubeport-backups` RWX PVC. Restore Jobs extract the archive and run `bench restore --force`; reconciliation treats a functional bench probe as success even if the restore Job exits non-zero. A failed restore attempt marks the target `Frappe Site` as `Failed` but returns the backup row to `Available` with failure detail, because the archive remains usable for a later restore.
- Backup archive lifecycle is independent of the source site's PVC and row lifecycle. Available backup rows can outlive the original `Frappe Site` row and keep the metadata needed for recovery.
- **Backup ground truth via PVC probe.** Reconciliation does not finalize a backup as `Available` purely on Job exit code or stdout metadata. A short-lived `busybox` probe Pod mounts the `kubeport-backups` PVC and reads the `<archive>.size` sidecar that the bench backup script writes only on a fully-flushed success. The probe is consulted in two places: (a) the gone-Job recovery path, replacing a broken `size_bytes` heuristic that always marked aged-out Jobs `Failed`, and (b) post-success verification, defending against the narrow window where `tar` exited zero but the inode was lost before reconciliation read it. `unknown` defers to the next tick.
- **Cancel cascades to in-flight backups.** `cancel_site` and `Frappe Site.on_trash` rotate not only the parent site's `operation_token` but also force-fail every linked `Frappe Site Backup` row in `Pending` / `In Progress` / `Restoring`, rotating its independent `operation_token`, publishing a realtime event, and enqueueing cluster cleanup. Without this, backup rows would stay in-flight forever and `_has_in_flight_backup` would block all future backups for that site.
- **Failed backups clean up archive files.** `Frappe Site Backup.on_trash` enqueues archive deletion whenever `storage_path` is stamped, regardless of status. A backup that partial-wrote an archive then was marked Failed used to leak the file on the PVC; now trashing the row removes it.
- **Self-managed lifecycle Jobs are explicit.** Archive-delete Jobs carry `kubeport.io/operation=archive-delete`. The orphan sweep recognises this label and skips them so the auto-clean is no longer racing the sweep's grace window.
- **Credentials flow through Kubernetes Secrets, never as plaintext env vars.** Create-site builds a `{job_name}-creds` Secret with `ADMIN_PASSWORD` (and `DB_ROOT_PASSWORD` when the user chose the plaintext field). Drop-site builds the same Secret shape but only with `DB_ROOT_PASSWORD` (no admin involved). Migrate needs no Secret at all. All Secrets are owner-referenced to their Job so they are garbage-collected alongside the Job's TTL cleanup. The Job reads values via `secretKeyRef`.
- Only MariaDB is supported as a database backend. The `db_type` field is fixed to `mariadb`; postgres plumbing has been intentionally removed — there is no forward-compatibility shim and no UI path to select it.
- Supports both direct database root password and Kubernetes Secret references.
- Per-run operation tokens for concurrency safety, reused across all three operations.
- **`on_trash` refuses direct deletion of `Active` rows** to prevent orphaning the real site on the bench PVC; the operator must go through Delete Site first. It also refuses `Migrating` rows so operators must use the destructive-confirmation cancel path. For any other row with `operation_job_name`, `on_trash` rotates the operation token and best-effort deletes the K8s Job with `propagation_policy="Background"` (which also cleans up the creds Secret via owner-reference GC).
- On Job TTL expiration before reconciliation reads the final status, reconciliation falls back to the same bench ground-truth probe used for the Job-failed branch, transitioning the row appropriately or deferring on `unknown`.
- Every operation Job carries `activeDeadlineSeconds` (30 min default) so a pod stuck in `ImagePullBackOff` or unable to reach the DB eventually flips to `failed` instead of leaving the row in an in-flight state forever.
- Reconciliation validates each Job's `kubeport.io/frappe-site` label before trusting its status, defending against hash collisions and stale `operation_job_name` values.
- **Orphan-Job sweep**: each reconciliation tick lists Kubeport-managed Jobs in every cluster/namespace pair that has any `Frappe Site` or `Frappe Site Backup` row and deletes site/backup Jobs not referenced by any row's `operation_job_name`. Jobs younger than one reconciliation tick are skipped so the sweep cannot race a worker that has applied a Job but has not recorded it yet. The sweep is operation-agnostic — it picks up orphaned create, delete, migrate, backup, and restore Jobs equally.
- Site names are validated to hostname-style labels (lowercase alphanumerics plus `.`, `-`, `_`, starting/ending alphanumeric) so they are safe for the `{bench_release}/{site_name}` docname, the K8s Job slug, and the bench environment.

### Live Discovery

- Cluster-scoped, read-only Helm release discovery via `kubeport.api.discovery.get_cluster_discovery`.
- Explicit adoption of a discovered Helm release via `adopt_helm_release`; discovery annotates whether a release is already tracked but never creates rows without the user's Track action.
- Release-scoped, read-only Frappe site discovery via pod exec.
- Stable payload: `{ benches, sites, errors }`.
- Discovery never persists results to MariaDB.

### Reconciliation

- Scheduled every 5 minutes via `hooks.py`.
- Helm releases: combines `helm status` with workload readiness from `helm get manifest`; recovers `Degraded` rows to `Deployed` when workloads become ready and marks pending/non-deployed Helm states as `Failed`.
- Service Bundles: resource existence check via K8s API.
- Frappe Sites and Backups: Job status polling with ground-truth site verification for create/delete/migrate/restore and a PVC-side `<archive>.size` sidecar probe for backup completion (gone-Job recovery and post-success verification).

### Operator Tools

- **Kubernetes Command** is a deliberate operator-tools doctype for ad-hoc Get / List / Delete against a fixed allowlist of namespaced kinds. Read access spans 8 kinds (Pod, Job, Secret, ConfigMap, Service, Deployment, StatefulSet, PVC); Delete is restricted to `Pod`, `Job`, `ConfigMap` only — destructive changes to the dangerous quartet (Secret, PVC, Deployment, StatefulSet) must go through the proper controllers (Helm Release, Service Bundle, Frappe Site).
- System Manager only. Delete requires `confirm_destructive` enforced at both validate (form save) and execute (background worker).
- **Kubernetes Command Audit Log** is an append-only doctype written on every execute (success or failure). System Manager has read access; never written from the UI. Decoupled from the source row so the audit trail survives row deletion.

---

## Robustness Properties

The codebase actively defends against imperfect cluster conditions:

| Property | Implementation |
|---|---|
| Discovery stays read-only | No discovered state persisted to MariaDB |
| Partial data is still useful | Release-level failures are isolated; successful releases still returned |
| Runtime failure is reported, not hidden | Structured error payloads with scope and context |
| Infra pods are not mistaken for workloads | MariaDB, Valkey pods excluded from site discovery |
| Pending workloads are not misinterpreted | Treated as cluster/runtime issue, not a reason to infer state |
| Stale workers are stopped | Per-run tokens + status re-checks before acting |
| Stuck Helm rows can recover | Stale operation reconciliation checks live Helm state after 30 minutes |
| Failed Helm rows cannot silently orphan resources | Direct delete is blocked outside `Draft`; uninstall is the cleanup path |
| Bench sites protect their parent release | Normal Helm uninstall is blocked while linked `Frappe Site` rows may still own bench-side state |
| Helm release health has one policy | Deploy workers and reconciliation share the same runtime/readiness classifier |
| Storage/network readiness is visible | PVC, Service/endpoints, Ingress, and warning events participate in Helm release health |
| Pod selection is resilient | Label-first with namespace-scan fallback, ranked by stability |
| Job exit codes are not trusted blindly | Ground-truth verification via exec-based site existence check |
| Reconciliation can recover | Documents move from `Degraded` back to `Deployed` when live state normalizes |
| Transient probe failures do not mark sites failed | Site probe returns `unknown` on exec errors; status transition deferred |
| Hung lifecycle Jobs are reaped | `activeDeadlineSeconds` on every create/delete/migrate Job |
| Orphan Jobs are swept | Label-based sweep in each reconciliation tick (operation-agnostic) |
| Orphan sweep does not race workers | Jobs younger than the reconciliation tick are excluded from the sweep |
| Job identity is validated | `kubeport.io/frappe-site` label checked before any status write |
| Active rows cannot be silently orphaned | `on_trash` refuses direct deletion of `Active` Frappe Site rows |
| Backup metadata survives source deletion | `Frappe Site Backup` is a standalone DocType and stores source cluster/namespace/site metadata |
| Backup archives are decoupled from site PVCs | Archives land on namespace-local `kubeport-backups` PVCs, not the bench sites PVC |
| Destructive cancellation is gated | `cancel_site` requires `confirm_destructive=True` for `Migrating`; UI requires typed `CANCEL` |
| Drop-site exit code is not trusted blindly | Bench probe is the source of truth for "site really gone" |
| Job failure messages are operation-specific | `_extract_job_failure_detail` receives an `operation_label` so logs clearly name which `bench` command failed (`bench new-site`, `bench drop-site`, `bench migrate`) |

---

## Open Gaps

### Discovery and Observability

- Discovery is a UI payload, not a richer observed-state model. Helm Release health shows resource
  readiness and release-scoped drilldowns for pod logs, Kubernetes events, and workload rollout
  context, but there is still no per-Frappe-Site health surface, no real-time log streaming, and
  no cluster-wide event timeline.
- Supported bench discovery is intentionally narrow: only official `erpnext` chart releases. Widening to other chart variants requires deliberate design.
- Discovery data is not linked back to persisted `Helm Release` documents beyond matching names and namespaces.

### Health Depth

- Helm release health covers built-in readiness for `Deployment`, `StatefulSet`, `DaemonSet`, `Pod`, `Job`, `PersistentVolumeClaim`, `Service`, and `Ingress`, and surfaces partial per-resource rows in the form.
- Helm health still does not inspect stored log/event history, storage pressure beyond PVC binding,
  application-level HTTP health, or CRD-specific health.
- Service Bundle health only checks resource existence.

### Platform Coverage

- Service Bundle only supports a fixed allowlist of built-in resource kinds (17 kinds). CRDs and arbitrary custom resources are out of scope.
- Helm rollback and history are available. Helm diff/preview remains out of scope.

### Site Lifecycle

- `Frappe Site` covers create, delete, migrate, backup, and restore. Scheduled backups, object-store storage, cross-cluster restore, retention policy, and restore-to-different-site-name remain out of scope.
- Discovered sites are not automatically linked to `Frappe Site` documents.
- Postgres-backed benches are not supported. The `db_type` field is locked to `mariadb` and the postgres code paths have been removed.

### Testing Depth

- Strong coverage: discovery, reconciliation, manifest validation, concurrency guards, cleanup patches, shared Frappe Site operation orchestration (Secret+Job apply, ownerRef attach, failure rollback), cancel task error handling, direct delete/migrate reconciliation branch behavior, full lifecycle simulation scenarios (create→active, cancel mid-flight, fail→delete→row-removed, migrate false-negative recovery, concurrent supersession), site backup/restore guards and Job finalization, Helm release lifecycle (install/upgrade/rollback/uninstall token staleness, blocking-site detection, force-uninstall, stale-operation recovery), Helm Repository sync supersession and chart inventory rebuild, and per-kind workload readiness for all eight supported kinds with warning-event attachment.
- Weak coverage: broader cross-DocType integration tests.

### Operator Documentation

- No documented guide for running Kubeport inside Kubernetes with required service-account RBAC and Helm binary packaging.
- No production hardening guidance (resource limits, monitoring, backup).

---

## Next Steps

The remaining work is depth work — the core plumbing is in place:

1. **Broader health modeling**: add application-level health probes and CRD-specific health where those signals have clear semantics.
2. **Backup depth**: add scheduled backups, retention policy, object-store backends, encryption, cross-cluster restore, and restore-to-different-site-name when the storage model is expanded.
3. **Testing coverage**: integration tests for repo sync, chart metadata, and cross-DocType workflows.
4. **Operator documentation**: RBAC requirements, Helm binary packaging, deployment guide, production hardening.
5. **Wider chart support**: controlled expansion of bench discovery beyond `erpnext`-only chart identification.
6. **Pre-existing test failures**: `test_k8s_client` (Python 3.14 / kubernetes-client API call signature drift) and `test_reconcile_service_bundles` require investigation independent of the Frappe Site feature.
