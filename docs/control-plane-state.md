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
- Background job discovers a running bench pod, dynamically extracts its container image and sites PVC mount, and submits a Kubernetes Job running `bench new-site`.
- Reconciliation verifies actual site existence on the bench (via exec-based discovery checking for `site_config.json` and `bench list-apps`) rather than trusting Job exit codes — avoids false negatives. The probe is three-state (`exists` / `missing` / `unknown`); transient bench-exec failures return `unknown` and defer the status transition to the next tick instead of marking the site `Failed`.
- **Credentials flow through a per-Job Kubernetes Secret, never as plaintext env vars.** The task creates a `{job_name}-creds` Secret carrying `ADMIN_PASSWORD` (and `DB_ROOT_PASSWORD` when the user chose the plaintext field), owner-referenced to the Job so it is garbage-collected alongside the Job's TTL cleanup. The Job reads both via `secretKeyRef`.
- Only MariaDB is a supported database backend today. The `db_type` field exposes only `mariadb`; postgres plumbing in `_build_env` / `_bench_new_site_command` is retained for forward compatibility but is not reachable from the UI and has not been validated on a real postgres-backed bench.
- Supports both direct database root password and Kubernetes Secret references.
- Per-run operation tokens for concurrency safety.
- `on_trash` cascades deletion to any in-flight Kubernetes Job: rotating the operation token invalidates concurrent workers and `cancel_site_task` deletes the Job with `propagation_policy="Background"`, which also cleans up the creds Secret via owner-reference GC.
- On Job TTL expiration before reconciliation reads the final status, reconciliation falls back to the same bench ground-truth probe used for the Job-failed branch, transitioning the site to Active, Failed, or (on `unknown`) deferring to the next tick.
- Every site-creation Job carries `activeDeadlineSeconds` (30 min default) so a pod stuck in `ImagePullBackOff` or unable to reach the DB eventually flips to `failed` instead of leaving the row in `In Progress` forever.
- Reconciliation validates each Job's `kubeport.io/frappe-site` label before trusting its status, defending against hash collisions and stale `creation_job_name` values.
- **Orphan-Job sweep**: each reconciliation tick lists Jobs labeled `app.kubernetes.io/managed-by=kubeport,kubeport.io/frappe-site` in every cluster/namespace pair that has any `Frappe Site` row and deletes those not referenced by any row's `creation_job_name`. Covers the narrow failure mode where a worker is hard-killed between applying the Job and recording its name.
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
| Hung site-creation Jobs are reaped | `activeDeadlineSeconds` on every Job |
| Orphan Jobs are swept | Label-based sweep in each reconciliation tick |
| Job identity is validated | `kubeport.io/frappe-site` label checked before any status write |

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

- `Frappe Site` currently supports creation only. Future work: deletion (`bench drop-site`), migration (`bench migrate`), backup/restore.
- Discovered sites are not automatically linked to `Frappe Site` documents.
- Postgres-backed benches are not yet supported end-to-end. The `db_type` field exposes only `mariadb`; the postgres code paths exist but have not been validated against a real postgres-backed bench and the root username is not user-configurable.

### Testing Depth

- Strong coverage: discovery, reconciliation, manifest validation, concurrency guards, cleanup patches, Frappe Site creation task orchestration (Secret+Job apply, ownerRef attach, failure rollback), cancel task error handling.
- Weak coverage: Helm Repository sync integration, Helm Chart metadata flows, broader cross-DocType integration tests.

### Operator Documentation

- No documented guide for running Kubeport inside Kubernetes with required service-account RBAC and Helm binary packaging.
- No production hardening guidance (resource limits, monitoring, backup).

---

## Next Steps

The remaining work is depth work — the core plumbing is in place:

1. **Broader health modeling**: surface pod readiness, rollout conditions, and event data in reconciliation and discovery.
2. **Site lifecycle expansion**: deletion, migration, backup/restore workflows for `Frappe Site`.
3. **Testing coverage**: integration tests for repo sync, chart metadata, site job submission, and cross-DocType workflows.
4. **Operator documentation**: RBAC requirements, Helm binary packaging, deployment guide, production hardening.
5. **Wider chart support**: controlled expansion of bench discovery beyond `erpnext`-only chart identification.
