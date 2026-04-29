# Codebase Summary

Module-level architecture reference for the Kubeport repository. This document describes what each module does and how they relate. For current capabilities and open gaps, see [Control Plane State](control-plane-state.md).

---

## Top-Level Structure

```
kubeport/
├── api/              # Whitelisted read-only endpoints
│   ├── __init__.py   # Namespace lookup, kubeconfig parsing/extraction
│   ├── discovery.py  # Live release and site discovery
│   └── site.py       # Frappe Site job log retrieval
├── utils/            # Stateless integration helpers
│   ├── k8s_client.py # Scoped Kubernetes API client builder
│   ├── helm.py       # Helm CLI wrapper (subprocess, temp kubeconfig)
│   ├── discovery.py  # Read-only cluster and site discovery logic
│   └── k8s_resources.py  # Manifest parsing, CRUD, resource allowlist
├── tasks/            # Background jobs (all cluster-mutating work)
│   ├── helm_tasks.py         # Repo sync, release deploy/uninstall
│   ├── service_bundle_tasks.py  # Manifest apply/delete
│   ├── site_tasks.py         # Frappe site creation via K8s Jobs
│   └── reconciliation.py     # Scheduled drift detection
├── kubeport/doctype/  # Frappe DocType definitions and controllers
│   ├── kubernetes_cluster/
│   ├── helm_repository/
│   ├── helm_chart/
│   ├── helm_chart_version/
│   ├── helm_release/
│   ├── service_bundle/
│   └── frappe_site/
├── tests/            # Cross-module unit tests
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
- Queues deploy (`helm upgrade --install`) and uninstall through background jobs.
- Tracks release lifecycle state (`Draft`, `In Progress`, `Deployed`, `Degraded`, `Failed`, `Uninstalling`).
- Workers re-check document status before acting, reducing stale duplicate execution.

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
- Uses per-run `operation_token` (concurrency control) and `operation_job_name` / `operation_job_token` (reconciliation identity) fields across all three lifecycle operations.

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
- Returns empty logs gracefully when the Job pod is not yet available or has been cleaned up by TTL

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

### `discovery.py`

Read-only cluster and site discovery logic. Contains most of the robustness milestone implementation.

- Release normalization: splits chart name and version, identifies Frappe bench charts.
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
- `install_or_upgrade_release` — idempotent `helm upgrade --install` with status re-check before acting
- `uninstall_release` — `helm uninstall` with stale-job guard

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
- All three share a single `_build_op_job_manifest` builder; only the command list and per-operation env differ.
- Job names are derived from the site name and operation token for uniqueness and traceability.
- Credentials flow through per-Job Kubernetes Secrets (never plaintext env values), owner-referenced to the Job for automatic garbage collection.
- Per-operation two-token concurrency guard: `operation_token` (rotated on each new action) and `operation_job_token` (snapshot captured when the Job is submitted, checked by reconciliation before any status write).

### `reconciliation.py`

Scheduled drift detection running every 5 minutes:

- **Helm Releases**: queries `helm status` for all `Deployed`/`Degraded` releases. Marks `Degraded` when Helm reports non-`deployed` status. Can recover back to `Deployed`.
- **Service Bundles**: checks resource existence via `check_resources_exist`. Marks `Degraded` on missing resources.
- **Frappe Sites**: polls `BatchV1Api.read_namespaced_job()` for all in-flight rows (`In Progress`, `Deleting`, `Migrating`). Verifies ground truth via exec-based bench probe before marking status transitions. Failure detail messages are operation-specific ("bench new-site failed", "bench drop-site failed", "bench migrate failed"). Falls back to bench probe on Job TTL expiry. Orphan-Job sweep runs on every tick.

---

## Frontend / Form Behavior

JavaScript form scripts in DocType folders follow an async-first pattern:

- `Kubernetes Cluster` renders live discovery tables in the form via `frappe.xcall`.
- `Helm Release` and `Service Bundle` listen for realtime status update events and refresh indicators.
- `Frappe Site` displays status indicators, creation triggers, and fetches Job logs asynchronously.
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
| Helm hooks and scheduling | Good | Scheduler event declarations |
| Helm Repository end-to-end sync | Weak | Needs integration test |
| Helm Chart metadata flows | Weak | Needs integration test |
| Cross-DocType integration | Weak | Needs broader workflow tests |

---

## Patches and Migration

The patch layer documents an architectural transition:

- **Pre-model-sync**: `migrate_kubernetes_manifests_to_service_bundles.py` — migrates data from legacy `Kubernetes Manifest` DocType into `Service Bundle`.
- **Post-model-sync**: `cleanup_legacy_kubeport_doctypes.py` — removes deprecated DocType tables. `rename_helm_release_docnames.py` — migrates Helm Release document names to the `cluster/namespace/release_name` identity scheme. `rename_frappe_site_job_fields.py` — renames `creation_job_name` → `operation_job_name` and `creation_job_token` → `operation_job_token` to reflect that these fields track the current operation regardless of type (create, delete, migrate).
