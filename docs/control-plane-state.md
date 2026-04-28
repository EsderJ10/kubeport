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

### Helm Release Management

- Desired release state scoped to `cluster/namespace/release_name`.
- Idempotent deploy via `helm upgrade --install` in background jobs.
- Uninstall via `helm uninstall` in background jobs.
- Workers re-check document status before acting (stale-job guard).
- Status lifecycle: `Draft` → `In Progress` → `Deployed` / `Degraded` / `Failed` → `Uninstalling` → `Draft`.
- Realtime events trigger form refresh on status changes.

### Service Bundle (Raw Manifests)

- Raw Kubernetes manifests validated against a fixed resource kind allowlist (17 built-in kinds).
- Server-side apply (idempotent) and deletion in background jobs.
- Per-run operation tokens and `Deleting` state for concurrency safety.
- Reconciliation checks resource existence and flags missing resources as `Degraded`.

### Frappe Site Provisioning

- `Frappe Site` DocType links to a Helm Release (the target bench).
- Background jobs run all three lifecycle operations (`bench new-site`, `bench drop-site`, `bench migrate`) by submitting a Kubernetes Job whose pod template is cloned from a live bench workload pod (image, sites PVC mount, env, pod-level fields). A single `_build_op_job_manifest` builder is reused across operations; only the bench command and per-op env differ.
- All three lifecycle operations run through a single `_run_site_op` orchestrator: per-op behavior is supplied by a small `SiteOpConfig` dataclass plus plain callables (`build_command`, `build_env`, `build_creds_secret`). This is the single source of truth for the apply pipeline, post-apply token re-check, and exception cleanup.
- Reconciliation verifies actual site existence on the bench (via exec-based discovery checking for `site_config.json` and `bench list-apps`) rather than trusting Job exit codes — avoids false negatives. The probe is three-state (`exists` / `missing` / `unknown`); transient bench-exec failures return `unknown` and defer the status transition to the next tick instead of marking the site `Failed`.
- **Lifecycle states**: `Draft → In Progress → Active | Failed`, plus `Active → Migrating → Active | Failed` and `Active|Failed → Deleting → [doc deleted] | Failed`. Cancellation rotates the operation token, marks the row `Failed`, and best-effort deletes the Job. Cancelling `Migrating` is confirmation-gated because killing `bench migrate` can leave MariaDB schema changes half-applied.
- **Deletion (`bench drop-site`)** runs with `--no-backup --force`. On a confirmed-missing probe, reconciliation removes the `Frappe Site` row itself via `frappe.delete_doc`. On a still-present probe (drop-site Job ran but the bench still has the site), the row lands in `Failed` with the Job logs in `status_detail` so the operator can retry.
- **Migration (`bench migrate`)** does not need credentials — bench reads them from `site_config.json`. Reconciliation runs the same functional probe used for creation; if the Job exit code is non-zero but the site is still functional (false negative), the row recovers to `Active`.
- **Credentials flow through Kubernetes Secrets, never as plaintext env vars.** Create-site builds a `{job_name}-creds` Secret with `ADMIN_PASSWORD` (and `DB_ROOT_PASSWORD` when the user chose the plaintext field). Drop-site builds the same Secret shape but only with `DB_ROOT_PASSWORD` (no admin involved). Migrate needs no Secret at all. All Secrets are owner-referenced to their Job so they are garbage-collected alongside the Job's TTL cleanup. The Job reads values via `secretKeyRef`.
- Only MariaDB is supported as a database backend. The `db_type` field is fixed to `mariadb`; postgres plumbing has been intentionally removed — there is no forward-compatibility shim and no UI path to select it.
- Supports both direct database root password and Kubernetes Secret references.
- Per-run operation tokens for concurrency safety, reused across all three operations.
- **`on_trash` refuses direct deletion of `Active` rows** to prevent orphaning the real site on the bench PVC; the operator must go through Delete Site first. It also refuses `Migrating` rows so operators must use the destructive-confirmation cancel path. For any other row with `operation_job_name`, `on_trash` rotates the operation token and best-effort deletes the K8s Job with `propagation_policy="Background"` (which also cleans up the creds Secret via owner-reference GC).
- On Job TTL expiration before reconciliation reads the final status, reconciliation falls back to the same bench ground-truth probe used for the Job-failed branch, transitioning the row appropriately or deferring on `unknown`.
- Every operation Job carries `activeDeadlineSeconds` (30 min default) so a pod stuck in `ImagePullBackOff` or unable to reach the DB eventually flips to `failed` instead of leaving the row in an in-flight state forever.
- Reconciliation validates each Job's `kubeport.io/frappe-site` label before trusting its status, defending against hash collisions and stale `operation_job_name` values.
- **Orphan-Job sweep**: each reconciliation tick lists Jobs labeled `app.kubernetes.io/managed-by=kubeport,kubeport.io/frappe-site` in every cluster/namespace pair that has any `Frappe Site` row and deletes those not referenced by any row's `operation_job_name`. Jobs younger than one reconciliation tick are skipped so the sweep cannot race a worker that has applied a Job but has not recorded it yet. The sweep is operation-agnostic — it picks up orphaned create, delete, and migrate Jobs equally.
- Site names are validated to hostname-style labels (lowercase alphanumerics plus `.`, `-`, `_`, starting/ending alphanumeric) so they are safe for the `{bench_release}/{site_name}` docname, the K8s Job slug, and the bench environment.

### Live Discovery

- Cluster-scoped, read-only Helm release discovery via `kubeport.api.discovery.get_cluster_discovery`.
- Release-scoped, read-only Frappe site discovery via pod exec.
- Stable payload: `{ benches, sites, errors }`.
- Discovery never persists results to MariaDB.

### Reconciliation

- Scheduled every 5 minutes via `hooks.py`.
- Helm releases: `helm status` check, marks `Degraded` on non-`deployed` status, recovers to `Deployed` when live state normalizes.
- Service Bundles: resource existence check via K8s API.
- Frappe Sites: Job status polling with ground-truth site verification.

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
| Pod selection is resilient | Label-first with namespace-scan fallback, ranked by stability |
| Job exit codes are not trusted blindly | Ground-truth verification via exec-based site existence check |
| Reconciliation can recover | Documents move from `Degraded` back to `Deployed` when live state normalizes |
| Transient probe failures do not mark sites failed | Site probe returns `unknown` on exec errors; status transition deferred |
| Hung lifecycle Jobs are reaped | `activeDeadlineSeconds` on every create/delete/migrate Job |
| Orphan Jobs are swept | Label-based sweep in each reconciliation tick (operation-agnostic) |
| Orphan sweep does not race workers | Jobs younger than the reconciliation tick are excluded from the sweep |
| Job identity is validated | `kubeport.io/frappe-site` label checked before any status write |
| Active rows cannot be silently orphaned | `on_trash` refuses direct deletion of `Active` Frappe Site rows |
| Destructive cancellation is gated | `cancel_site` requires `confirm_destructive=True` for `Migrating`; UI requires typed `CANCEL` |
| Drop-site exit code is not trusted blindly | Bench probe is the source of truth for "site really gone" |
| Job failure messages are operation-specific | `_extract_job_failure_detail` receives an `operation_label` so logs clearly name which `bench` command failed (`bench new-site`, `bench drop-site`, `bench migrate`) |

---

## Open Gaps

### Discovery and Observability

- Discovery is a UI payload, not a richer observed-state model. No in-app drilldown for pod events, PVC failures, rollout conditions, or per-site health.
- Supported bench discovery is intentionally narrow: only official `erpnext` chart releases. Widening to other chart variants requires deliberate design.
- Discovery data is not linked back to persisted `Helm Release` documents beyond matching names and namespaces.

### Health Depth

- Helm release health relies mostly on `helm status` output (`deployed` vs. other).
- Service Bundle health only checks resource existence.
- Neither path surfaces richer readiness semantics: pod readiness probes, rollout completion, failed jobs, or resource condition messages.

### Platform Coverage

- Service Bundle only supports a fixed allowlist of built-in resource kinds (17 kinds). CRDs and arbitrary custom resources are out of scope.
- No first-class support for higher-level Helm workflows: rollback, diff, preview, release-history inspection.

### Site Lifecycle

- `Frappe Site` covers create, delete, and migrate. Backup/restore is the remaining gap and is intentionally deferred — it requires a separate storage strategy (PVC vs object store), a child DocType for backup runs, and file-upload plumbing for restore.
- Discovered sites are not automatically linked to `Frappe Site` documents.
- Postgres-backed benches are not supported. The `db_type` field is locked to `mariadb` and the postgres code paths have been removed.

### Testing Depth

- Strong coverage: discovery, reconciliation, manifest validation, concurrency guards, cleanup patches, shared Frappe Site operation orchestration (Secret+Job apply, ownerRef attach, failure rollback), cancel task error handling, direct delete/migrate reconciliation branch behavior, and full lifecycle simulation scenarios (create→active, cancel mid-flight, fail→delete→row-removed, migrate false-negative recovery, concurrent supersession).
- Weak coverage: Helm Repository sync integration, Helm Chart metadata flows, broader cross-DocType integration tests.

### Operator Documentation

- No documented guide for running Kubeport inside Kubernetes with required service-account RBAC and Helm binary packaging.
- No production hardening guidance (resource limits, monitoring, backup).

---

## Next Steps

The remaining work is depth work — the core plumbing is in place:

1. **Broader health modeling**: surface pod readiness, rollout conditions, and event data in reconciliation and discovery.
2. **Site backup/restore**: design backup storage (PVC vs object store), introduce a `Frappe Site Backup` child DocType for run history, expose download/upload flows.
3. **Testing coverage**: integration tests for repo sync, chart metadata, and cross-DocType workflows.
4. **Operator documentation**: RBAC requirements, Helm binary packaging, deployment guide, production hardening.
5. **Wider chart support**: controlled expansion of bench discovery beyond `erpnext`-only chart identification.
6. **Pre-existing test failures**: `test_k8s_client` (Python 3.14 / kubernetes-client API call signature drift) and `test_reconcile_service_bundles` require investigation independent of the Frappe Site feature.
