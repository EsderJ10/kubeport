# Codebase Summary

Module-level architecture reference for the Kubeport repository. This document describes what each module does and how they relate. For current capabilities and open gaps, see [Control Plane State](control-plane-state.md).

---

## Top-Level Structure

```
kubeport/
├── api/              # Whitelisted read-only endpoints
│   ├── __init__.py        # Namespace lookup, kubeconfig parsing/extraction
│   ├── discovery.py       # Live release and site discovery
│   ├── observability.py   # Helm Release pod logs, events, rollout context
│   ├── dashboard.py       # Kubeport workspace dashboard data
│   ├── site_images.py     # DB-backed Kubeport Site Image catalog
│   └── site.py            # Frappe Site job log and backup listing support
├── utils/            # Stateless integration helpers
│   ├── k8s_client.py      # Scoped Kubernetes API client builder
│   ├── helm.py            # Helm CLI wrapper (subprocess, temp kubeconfig)
│   ├── discovery.py       # Read-only cluster and site discovery logic
│   ├── observability.py   # Pod / event / rollout helpers for diagnostics
│   ├── release_health.py  # Shared workload-readiness classifier
│   ├── k8s_resources.py   # Manifest parsing, CRUD, resource allowlist
│   └── constants.py       # Shared constants (resource kinds, labels)
├── tasks/            # Background jobs (all cluster-mutating work)
│   ├── helm_tasks.py             # Repo sync, release deploy/uninstall/rollback
│   ├── site_image_tasks.py       # Shipped site-image catalog sync
│   ├── service_bundle_tasks.py   # Manifest apply/delete
│   ├── site_tasks.py             # Frappe site lifecycle via K8s Jobs
│   ├── kubernetes_command_tasks.py  # Operator-tools execute path
│   └── reconciliation.py         # Scheduled drift detection + orphan-Job sweep
├── kubeport/doctype/  # Frappe DocType definitions and controllers
│   ├── kubernetes_cluster/
│   ├── helm_repository/
│   ├── helm_chart/
│   ├── helm_chart_version/
│   ├── helm_release/
│   ├── service_bundle/
│   ├── frappe_site/
│   ├── frappe_site_backup/
│   ├── kubeport_site_image/
│   ├── kubeport_site_image_app/
│   ├── kubernetes_command/
│   └── kubernetes_command_audit_log/
├── kubeport/workspace/kubeport_operations/  # Desk workspace (auto-installed)
├── kubeport/number_card/                    # Workspace dashboard cards (fixtures)
├── tests/            # Cross-module unit and integration tests
├── patches/          # Schema migration and cleanup
└── hooks.py          # App configuration, scheduled jobs
```

## Core Runtime Model

The codebase enforces a strict split between desired state and observed state:

- **Desired state** lives in MariaDB through Frappe DocTypes.
- **Observed state** comes from live Kubernetes and Helm queries.
- **Mutating operations** are routed through background jobs on the `long` queue.
- **Reconciliation** compares desired and observed state on a schedule and updates status fields.

This is the defining architectural decision in the repository.

---

## DocTypes

### Kubernetes Cluster

Stores cluster connectivity configuration and serves as the anchor for all live Kubernetes access.

- Validates auth-method-specific fields (kubeconfig content, bearer token + CA certificate, or in-cluster).
- Supports connection testing against the real Kubernetes API.
- Provides dev-only TLS verification bypass for kubeconfig and bearer-token local clusters.
- Requires a CA certificate for bearer-token auth unless dev-only TLS bypass is explicitly enabled.
- Normalizes imported kubeconfig server endpoints when the source file points at local-only addresses (`0.0.0.0`, `127.0.0.1`, `localhost`) unreachable from the app container.
- Client-side form renders live discovery tables via async API calls.

### Helm Repository

Stores Helm repo configuration and sync metadata.

- Auto-registers repos with Helm on creation.
- Syncs charts in background jobs with per-run sync tokens to prevent stale workers from overwriting newer syncs.
- Rebuilds full chart/version inventory from the current repo index and prunes stale chart rows that disappeared upstream.
- Tracks sync status and last sync timestamp.
- Supports optional `include_patterns` (comma-separated globs) to filter which charts are synced.

### Helm Chart

Stores repository-backed chart metadata and version history.

- Autonamed as `{repository}/{chart_name}`.
- Maintains a child table of `Helm Chart Version` records.
- Caches default `values.yaml` content.
- Clears cached default values when the latest chart version changes during sync.
- Builds chart references for Helm CLI commands.

### Helm Release

Stores desired state for a Helm-managed workload deployment.

- Identity is scoped to `cluster/namespace/release_name`, matching real Helm release scope.
- Validates YAML values content.
- Rejects unsafe `local-path` StorageClass plus `ReadWriteMany` access mode combinations.
- Can link to a `Kubeport Site Image` for official ERPNext/Frappe bench charts. The selected catalog image is desired state and is rendered into Helm values during deploy as `image.repository`, digest-aware `image.tag`, and `image.pullPolicy=IfNotPresent`.
- Blocks conflicting manual `values.image.*` overrides while a Site Image is selected so image intent stays unambiguous. The desired spec hash includes the selected image row and digest for pending-change detection.
- Queues deploy (`helm upgrade --install`), rollback, and uninstall through background jobs.
- Tracks release lifecycle state (`Draft`, `In Progress`, `Deployed`, `Degraded`, `Failed`, `Uninstalling`).
- Tracks desired spec hash, last-applied spec hash, last-applied chart version, operation type, and operation start time. `pending_changes` is set when saved desired state differs from the last successful apply.
- Uses per-run operation tokens plus status re-checks so stale deploy/uninstall workers cannot overwrite a newer operation.
- Blocks normal uninstall while linked `Frappe Site` rows may still own bench-side state; force uninstall requires typed confirmation.
- Exposes live Helm history and queues rollback as a long-queue operation. Successful rollback updates the saved desired values/chart version to match the selected live revision.
- Allows direct row deletion only from `Draft`; `Failed` rows may still own cluster resources and must be cleaned up through uninstall.
- Classifies release state with the shared workload-readiness policy in `utils/release_health.py`.

### Service Bundle

Stores desired state for raw Kubernetes manifests.

- Validates manifest content against the supported resource kind allowlist.
- Queues apply (server-side apply) and delete through background jobs.
- Uses per-run operation tokens and a distinct `Deleting` state for concurrency safety.
- Tracks bundle lifecycle state.

### Frappe Site

Stores desired state for a Frappe site to be created on a running bench.

- Links to a `Helm Release` (the target bench).
- Stores site name, admin password, database credentials, and apps to install.
- Only `mariadb` is a supported `db_type`; the field is locked to that single value.
- Background jobs discover a running bench pod, dynamically extract its container image and sites PVC mount, and submit a Kubernetes Job running the appropriate `bench` command. A single `_build_op_job_manifest` builder is reused across create, delete, and migrate; only the command and per-operation env differ.
- Reconciliation verifies site existence via exec-based discovery (checks for `site_config.json`) rather than trusting Job exit codes — avoids false negatives when `--install-app` triggers non-fatal warnings.
- Uses per-run `operation_token` (concurrency control) and `operation_job_name` / `operation_job_token` (reconciliation identity) fields across site lifecycle operations.

### Frappe Site Backup

Stores metadata for backup archives created from a Frappe Site.

- Tracks backup lifecycle state (`Pending`, `In Progress`, `Available`, `Restoring`, `Failed`).
- Persists only metadata: timestamp, size, storage backend/path, operation Job identity, and source site metadata.
- Archives live on namespace-local `kubeport-backups` PVCs and are not stored in MariaDB.
- Backup records are standalone so recovery metadata can outlive the original `Frappe Site` row.

### Kubeport Site Image

Stores public GHCR Frappe/ERPNext runtime images available for Helm Release selection — both curated and user-registered images.

- Catalog rows are product metadata in MariaDB. Curated rows are seeded from `kubeport/site_images/catalog.json`; user-registered rows are created in the UI.
- Tracks repository, release tag, digest, Frappe major, ERPNext version, source revision, apps.json hash, status, default selection, and an `is_curated` flag (`1` for curated rows owned by the catalog sync, `0` for user-registered rows). Frappe's built-in `owner` field records who created each row.
- Active curated rows must include a pushed image digest; user rows may be created without a digest but then deploy by tag.
- Includes child rows for bundled apps; the grid is editable on user rows and read-only on curated rows.
- Sync runs as a long-queue background job on install/migrate and daily scheduler. The sync only writes `is_curated=1` rows and skips repository:tag collisions with user rows (logged as a warning) so user data is never overwritten.
- `is_default` is reserved for curated rows. Deletion is blocked when a Helm Release links to the row, and curated rows cannot be deleted (use `Deprecated`).

### Kubernetes Command

A deliberate operator-tools doctype for ad-hoc Get / List / Delete against a fixed allowlist of namespaced kinds.

- Read access spans 8 kinds: `Pod`, `Job`, `Secret`, `ConfigMap`, `Service`, `Deployment`, `StatefulSet`, `PersistentVolumeClaim`.
- Delete is restricted to `Pod`, `Job`, `ConfigMap` only — destructive changes to the dangerous quartet (Secret, PVC, Deployment, StatefulSet) must go through their dedicated controllers (`Helm Release`, `Service Bundle`, `Frappe Site`).
- System Manager only. Delete requires `confirm_destructive` enforced both at validate (form save) and execute (background worker).
- Execution is enqueued onto the `long` queue via `kubeport.tasks.kubernetes_command_tasks`.

### Kubernetes Command Audit Log

Append-only audit row written on every `Kubernetes Command` execute (success or failure).

- System Manager has read access; the doctype is never written from the UI.
- Decoupled from the source row — the audit trail survives `Kubernetes Command` row deletion.

---

## API Layer

### `kubeport.api.__init__`

Form-supporting endpoints for cluster interaction:

- `get_cluster_namespaces(cluster_name)` — live namespace listing for form dropdowns
- `parse_kubeconfig_contexts(kubeconfig_content)` — parse uploaded kubeconfig, return context metadata with normalization info
- `extract_kubeconfig_context(kubeconfig_content, context_name)` — extract a minimal, self-contained kubeconfig for a single context

Includes kubeconfig endpoint normalization: detects local-only API server addresses (`0.0.0.0`, `127.0.0.1`, `localhost`) and replaces them with the container's default gateway IP for containerized development setups.

### `kubeport.api.discovery`

Live cluster discovery endpoint:

- `get_cluster_discovery(cluster_name)` — returns `{ benches, sites, errors }` payload
- Discovers Helm releases cluster-wide, then runs site discovery against releases identified as Frappe benches (currently `erpnext` chart only)
- Release-level failures are isolated and reported as partial errors — a single failing release does not block discovery for other releases

### `kubeport.api.site`

Frappe Site form support:

- `get_site_job_logs(site_docname)` — fetches stdout from the current operation Job pod for display in the form; uses the `operation_job_name` field to locate the pod
- `list_site_backups(site_docname)` — returns backup rows for the site form, newest first
- `get_site_backup_job_logs(backup_docname)` — fetches stdout from backup/restore Jobs
- Returns empty logs gracefully when the Job pod is not yet available or has been cleaned up by TTL

### `kubeport.api.site_images`

Read-only site-image catalog endpoint:

- `list_site_images(include_deprecated=False)` — returns active catalog rows and included app metadata for Helm Release form rendering

### `kubeport.api.dashboard`

Backs the Kubeport Operations workspace dashboard. Read-only counters and stale-operation summaries; no cluster I/O — everything is sourced from desired-state DocTypes.

### `kubeport.api.observability`

Read-only Helm Release drilldown endpoints:

- Resolves cluster identity from the `Helm Release` row after System Manager and document read checks.
- Validates the requested resource against the live Helm manifest before reading logs, events, or rollout context.
- Returns the ownership-proven pod list and bounded logs for one selected pod, plus release-scoped event and rollout payloads without persisting observed state.
- Uses explicit `{rows, error}` wrappers for event and rollout lookup failures so the form can render panel-level degradation.

---

## Utility Layer

### `k8s_client.py`

Builds scoped Kubernetes API clients from cluster documents. Foundational for all K8s-facing flows.

- Supports three auth modes: kubeconfig, bearer token, in-cluster.
- Avoids mutating global Kubernetes client state — each call produces an isolated client.
- Handles bearer-token CA material and TLS bypass configuration.

### `helm.py`

Stateless wrapper around the Helm 3 binary.

- Every function writes a temporary kubeconfig file, runs `helm` via `subprocess.run` (as a list, never `shell=True`), and cleans up in a `finally` block.
- JSON output parsing where Helm supports it; raw YAML for `helm show values`.
- In-Cluster auth yields `None` for the kubeconfig path, allowing Helm to auto-detect pod credentials.
- Concurrent workers never share kubeconfig state due to per-call temp file isolation.

### `release_health.py`

Read-only observed health for Helm Releases.

- Reads `helm get manifest` to enumerate Helm-owned resources and checks readiness for built-in resource kinds (`Deployment`, `StatefulSet`, `DaemonSet`, `Pod`, `Job`, `PersistentVolumeClaim`, `Service`, `Ingress`).
- Converts missing/API-error resources into unready rows so the form can show partial results instead of failing the whole health read.
- Provides the shared classifier used by deploy workers and reconciliation: `deployed` plus all workloads ready is `Deployed`; `deployed` plus unready/probe error is `Degraded`; pending or non-deployed Helm states are `Failed`.
- Keeps observed per-resource rows ephemeral; only lifecycle/status metadata (`status`, `helm_revision`, short `helm_status_detail`, spec hashes, and operation markers) is persisted on the Helm Release row.

### `observability.py`

Read-only Kubernetes helpers for Helm Release diagnostics.

- Resolves pods for `Deployment`, `StatefulSet`, `DaemonSet`, and standalone `Pod` resources using controller owner references and selectors.
- Fetches one selected pod's logs per request with server-side tail limits and a response-size cap.
- Normalizes scoped Kubernetes events and workload rollout context for form rendering.

### `discovery.py`

Read-only cluster and site discovery logic. Contains most of the robustness milestone implementation.

- Release normalization: splits chart name and version, identifies Frappe bench charts.
- Discovery annotates whether a Helm release is already tracked and exposes explicit adoption; it does not create rows during read-only discovery.
- Pod selection: label-first lookup with fallback to namespace scan. Workload-only filtering excludes infra pods (mariadb, valkey). Candidate pods are ranked by phase, readiness, and component label.
- Site listing: exec into a selected pod and enumerate directories containing `site_config.json`.

### `k8s_resources.py`

Manifest parsing, validation, and CRUD for Service Bundle resources.

- Validates managed manifest content against a fixed allowlist of built-in resource kinds.
- Supports server-side apply (`application/apply-patch+yaml`) with fallback to `create_from_dict` on 404.
- Supports deletion with silent success on 404 (resource already gone).
- Provides `check_resources_exist` for reconciliation health checks.

---

## Task Layer

### `helm_tasks.py`

Helm repository and release operations:

- `add_and_sync_repo` — register repo with Helm, sync chart inventory
- `sync_repo_charts` — re-register repo, refresh index, sync charts with per-run token check
- `sync_all_repos` — daily scheduler entry point, enqueues `sync_repo_charts` for each repo
- `install_or_upgrade_release` — idempotent `helm upgrade --install` with operation-token stale-job guards, shared health classification, and last-applied spec writeback
- `rollback_release` — `helm rollback` with stale-job guard; successful rollback updates saved desired values/chart version to the selected live revision
- `uninstall_release` — `helm uninstall` with stale-job guard; Helm "release not found" is treated as successful cleanup and returns the row to `Draft`, clearing last-applied metadata

Chart sync rebuilds full version inventory from `helm search repo --versions`, groups by chart name, deduplicates versions, prunes charts that disappeared upstream, and clears cached default values when the latest version changes.

### `service_bundle_tasks.py`

Raw manifest lifecycle:

- `apply_service_bundle` — iterate manifest objects and call `apply_resource` for each
- `delete_service_bundle` — iterate manifest objects and call `delete_resource` for each
- Both use per-run operation tokens and publish realtime events.

### `site_tasks.py`

Frappe site lifecycle operations via Kubernetes Jobs:

- `create_site_task` — discovers a live bench pod, extracts its image and sites PVC mount dynamically, builds a Job manifest running `bench new-site`, and submits it.
- `delete_site_task` — builds a Job running `bench drop-site --no-backup --force` with DB root credentials injected.
- `migrate_site_task` — builds a Job running `bench migrate`; no credentials Secret needed (bench reads from `site_config.json`).
- `backup_site_task` — ensures a namespace-local `kubeport-backups` RWX PVC, mounts it into a cloned bench Job, runs `bench backup --with-files`, and writes a tar archive plus metadata markers.
- `restore_site_task` — mounts the same backup PVC, extracts the selected archive, and runs `bench restore --force` with public/private file archives when present. Restore submission or reconciliation failures leave the backup row `Available` with failure detail while the target site becomes `Failed`.
- Site operations share a single `_build_op_job_manifest` builder; only the command list, per-operation env, and backup PVC mount differ.
- Job names are derived from the site name and operation token for uniqueness and traceability.
- Credentials flow through per-Job Kubernetes Secrets (never plaintext env values), owner-referenced to the Job for automatic garbage collection.
- Per-operation two-token concurrency guard: `operation_token` (rotated on each new action) and `operation_job_token` (snapshot captured when the Job is submitted, checked by reconciliation before any status write).

### `kubernetes_command_tasks.py`

Operator-tools execution path used by `Kubernetes Command`:

- `execute_kubernetes_command(command_name, operation_token)` — re-checks the document's status and `confirm_destructive` flag before executing the requested Get / List / Delete against the allowlisted kinds.
- Writes one `Kubernetes Command Audit Log` row per execute (success or failure), including the cluster, namespace, kind, name, operation, exit status, and operator.

### `reconciliation.py`

Scheduled drift detection running every 5 minutes:

- **Helm Releases**: queries `helm status` for all `Deployed`/`Degraded` releases, then applies the shared manifest/readiness classifier. It can recover `Degraded` rows to `Deployed`, flag unready workloads as `Degraded`, and mark pending/non-deployed Helm states as `Failed`. It also recovers stale `In Progress` / `Uninstalling` operations after 30 minutes.
- **Service Bundles**: checks resource existence via `check_resources_exist`. Marks `Degraded` on missing resources.
- **Frappe Sites and Backups**: polls `BatchV1Api.read_namespaced_job()` for all in-flight site rows (`In Progress`, `Deleting`, `Migrating`) and backup rows (`In Progress`, `Restoring`). Verifies ground truth via exec-based bench probe before marking site/restore transitions. Failure detail messages are operation-specific ("bench new-site failed", "bench drop-site failed", "bench migrate failed", "bench backup failed", "bench restore failed"). Falls back to bench probe on Job TTL expiry where possible. Orphan-Job sweep runs on every tick.

---

## Frontend / Form Behavior

JavaScript form scripts in DocType folders follow an async-first pattern:

- `Kubernetes Cluster` renders live discovery tables in the form via `frappe.xcall`.
- `Helm Release` and `Service Bundle` listen for realtime status update events and refresh indicators. Helm Release health loads asynchronously as `{rows, error}` and unready rows open a persistent in-form observability panel exposing pod logs (with a pod picker when more than one pod backs the resource), scoped Kubernetes events, and rollout context without blocking document load. The observability panel guards async callbacks so stale responses cannot overwrite the active resource, view, or pod selection.
- `Frappe Site` displays status indicators, lifecycle triggers, backup rows, restore actions, and fetches Job logs asynchronously.
- Namespace suggestions are fetched live from the selected cluster.
- Forms never attempt to persist externally discovered state during document fetch.

---

## Test Coverage

| Area | Coverage Level | Notes |
|---|---|---|
| Discovery behavior | Strong | Pod selection, fallback matching, error categories |
| API timeout usage | Strong | Request timeout assertions |
| Reconciliation state transitions | Strong | Including Frappe Site ground-truth verification for all three operations |
| Manifest validation | Strong | Resource kind allowlist, field validation |
| Helm worker concurrency guards | Strong | Token checks, status re-checks |
| Cleanup/migration patches | Strong | Legacy DocType removal, data migration |
| Frappe Site lifecycle simulation | Strong | End-to-end mocked scenarios: create→active, cancel mid-flight, fail→delete→row-removed, migrate false-negative recovery, concurrent supersession |
| Frappe Site backup/restore | Good | Backup row guards, PVC/job construction, reconciliation finalization, restore confirmation |
| Helm hooks and scheduling | Good | Scheduler event declarations |
| Helm Repository end-to-end sync | Weak | Needs integration test |
| Helm Chart metadata flows | Weak | Needs integration test |
| Cross-DocType integration | Weak | Needs broader workflow tests |

---

## Patches and Migration

The patch layer documents an architectural transition:

- **Pre-model-sync**: `migrate_kubernetes_manifests_to_service_bundles.py` — migrates data from legacy `Kubernetes Manifest` DocType into `Service Bundle`.
- **Post-model-sync**: `cleanup_legacy_kubeport_doctypes.py` — removes deprecated DocType tables. `rename_helm_release_docnames.py` — migrates Helm Release document names to the `cluster/namespace/release_name` identity scheme. `rename_frappe_site_job_fields.py` — renames `creation_job_name` → `operation_job_name` and `creation_job_token` → `operation_job_token` to reflect that these fields track the current operation regardless of type (create, delete, migrate).
