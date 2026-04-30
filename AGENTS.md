# Kubeport — Agent Instructions

This file is the authoritative reference for AI coding agents working on the Kubeport repository. It defines project context, architectural invariants, implementation patterns, and behavioral rules that agents must follow.

---

## Project Context

Kubeport is a Frappe app that acts as a Kubernetes control plane inside the Frappe/ERPNext UI. It manages cluster connectivity, Helm releases, raw Kubernetes manifests (via Service Bundles), and Frappe site provisioning.

**The defining architectural invariant**: desired state lives in MariaDB through Frappe DocTypes; observed state is always queried live from the cluster. Never blur these two categories.

## Stack and Tooling

| Attribute | Value |
|---|---|
| Type | Frappe app for ERPNext/Frappe |
| Python | 3.14+ |
| Build backend | `flit` |
| Install | `bench get-app <url> --branch dev/jose` then `bench install-app kubeport` |
| Dependencies | `kubernetes`, `urllib3`, `PyYAML` |

### Code Style

- **Indentation**: tabs (not spaces)
- **Quote style**: double quotes
- **Line length**: 110 characters
- **Python formatting**: `ruff format`
- **Python linting**: `ruff`
- **JS/CSS formatting**: `prettier`
- **JavaScript linting**: `eslint`

### Type Annotations

- `export_python_type_annotations = True` is enabled in `hooks.py`
- Use `frappe.types.DF` for DocType field type hints
- All whitelisted API methods must have type annotations

### Pre-commit

Pre-commit is expected to be installed in a bench checkout:

```bash
cd apps/kubeport && pre-commit install
```

## Repository Rules

- Prefer `rg` / `rg --files` for search and file discovery.
- Use `apply_patch` for manual edits when available.
- Never revert user changes unless explicitly asked.
- Do not use destructive git commands (`git reset --hard`, `git checkout --`, etc.).
- Keep edits ASCII unless the file already uses non-ASCII text.

---

## Architecture

### Layer Map

| Layer | Path | Responsibility |
|---|---|---|
| DocTypes (desired state) | `kubeport/kubeport/doctype/` | MariaDB-backed documents; one per resource kind |
| API endpoints | `kubeport/api/` | Whitelisted, read-only queries supporting forms |
| K8s / Helm utilities | `kubeport/utils/` | Stateless clients and helpers |
| Background tasks | `kubeport/tasks/` | All cluster-mutating operations |
| Scheduled jobs | `hooks.py` | Reconciliation (every 5 min), daily repo sync |
| Tests | `kubeport/tests/`, `doctype/*/test_*.py` | Unit and integration tests |
| Patches | `kubeport/patches/` | Schema migration and cleanup |

### DocTypes

| DocType | Purpose | Key Behavior |
|---|---|---|
| `Kubernetes Cluster` | Cluster credentials and connectivity | Multi-auth (kubeconfig, bearer token, in-cluster). Dev-only TLS bypass. Kubeconfig endpoint normalization. Client-side live discovery rendering. |
| `Helm Repository` | Helm repo configuration | Background chart sync with per-run sync tokens. Full chart/version inventory rebuild. Stale chart pruning. |
| `Helm Chart` | Synced chart metadata | Version history and cached default values. Autonamed as `{repository}/{chart_name}`. |
| `Helm Chart Version` | Individual chart version record | Child table of `Helm Chart`. |
| `Helm Release` | Desired Helm release state | Identity scoped to `cluster/namespace/release_name`. YAML values validation. Background deploy/uninstall. |
| `Service Bundle` | Desired raw-manifest state | Validates against supported resource kind allowlist. Per-run operation tokens. Background apply/delete. |
| `Frappe Site` | Desired Frappe site on a bench | Links to a Helm Release (the bench). Submits K8s Jobs for create/delete/migrate/backup/restore. Ground-truth reconciliation. |
| `Frappe Site Backup` | Site backup archive metadata | Standalone metadata rows for PVC-backed backups. Archives live on namespace-local `kubeport-backups` PVCs and can outlive the source site row. |

### Scheduled Jobs

Declared in `hooks.py`:

- `*/5 * * * *` → `kubeport.tasks.reconciliation.reconcile_all_releases` (drift detection for Helm Releases, Service Bundles, and Frappe Sites)
- Daily → `kubeport.tasks.helm_tasks.sync_all_repos` (chart catalog refresh)

---

## Design Invariants

These are non-negotiable rules. Every code change must respect them.

### 1. Desired State vs. Observed State

- **DO**: Store intent in DocType fields. Query live state from Kubernetes/Helm at read time.
- **DO**: Store backup metadata in MariaDB, but keep backup archives outside MariaDB on the configured storage backend.
- **DO NOT**: Persist discovered cluster state into MariaDB. Discovery is read-only.

### 2. Async-First Execution

- **DO**: Route all cluster-mutating operations (Helm install/upgrade/uninstall, Service Bundle apply/delete, Frappe Site job submission) through `frappe.enqueue` background jobs on the `long` queue.
- **DO NOT**: Make K8s/Helm API calls that modify cluster state from the web thread.

### 3. Concurrency Safety

- **DO**: Use per-run operation/sync tokens. Re-check document status and token before acting in background workers. Use targeted `frappe.db.set_value` / `db_set` updates.
- **DO NOT**: Use broad `doc.reload()` calls in background tasks when concurrency matters. Allow duplicate actions while a document is `In Progress` or `Uninstalling` without token checks.

### 4. Discovery Is Read-Only

- **DO**: Return partial results when individual releases or pods fail. Distinguish error categories clearly (no pods, only infra pods, workload pods not running, exec failure).
- **DO NOT**: Persist discovered benches, sites, or pod state into the database. Discovery data is ephemeral.

### 5. Form Rendering

- **DO**: Prefer async form calls (`frappe.xcall` / `frappe.call`) plus client-side rendering for external data.
- **DO NOT**: Use framework features that load external Kubernetes data during `doc.onload` or document fetch.

### 6. Cluster Access Scoping

- **DO**: Keep all Kubernetes access scoped to the target cluster document. Build scoped API clients via `get_k8s_api_client(cluster_name)`.
- **DO NOT**: Use global Kubernetes client configuration or share client state between requests.

### 7. Backup Archive Independence

- **DO**: Treat backup archive lifecycle as independent from the source site's bench PVC and `Frappe Site` row lifecycle.
- **DO NOT**: Delete or require the source `Frappe Site` row to restore from an `Available` `Frappe Site Backup` whose cluster/namespace/site metadata matches the target site.

---

## Implementation Patterns

### Adding a New Background Task

1. Create the task function in the appropriate `kubeport/tasks/` module.
2. Accept a document identifier and an `operation_token` parameter.
3. Re-check the document's current status and token before performing work (stale-job guard).
4. Wrap the cluster operation in try/except. On failure: update status, log the error, publish realtime event.
5. On success: update status fields with targeted `db_set` or `frappe.db.set_value`, publish realtime event.
6. The task is enqueued from the DocType controller using `frappe.enqueue(..., queue="long", enqueue_after_commit=True)`.

### Adding a New API Endpoint

1. Place it in `kubeport/api/` in the appropriate module.
2. Decorate with `@frappe.whitelist()`.
3. Add full type annotations (required by `require_type_annotated_api_methods = True`).
4. Keep it read-only — no cluster mutations from API endpoints.
5. Handle failures gracefully — return empty results or structured error payloads instead of raising.

### Adding a New DocType Field

1. Update the DocType JSON definition via the Frappe DocType editor or manually.
2. Add the field to the Python controller if it requires validation or business logic.
3. Use `frappe.types.DF` for type hints on the controller class.
4. If the field affects discovery, reconciliation, or background-task behavior, update the relevant docs in the same change.

### Discovery Pod Selection

Pod lookup for site discovery follows a priority chain:
1. Label selector: `app.kubernetes.io/instance=<release_name>` (preferred)
2. Fallback: namespace-wide scan matching by `release` label or pod-name prefix
3. Filter: only Frappe workload pods (gunicorn, scheduler, workers — not mariadb, valkey)
4. Rank: score by Running phase, ready containers, component label

### Supported Resource Kinds (Service Bundle)

Service Bundle only supports a fixed allowlist of built-in Kubernetes resource kinds defined in `kubeport/utils/k8s_resources.py`. CRDs and arbitrary custom resources are out of scope.

Current allowlist: `Pod`, `Service`, `Deployment`, `ConfigMap`, `Secret`, `Namespace`, `Ingress`, `PersistentVolumeClaim`, `StatefulSet`, `DaemonSet`, `Job`, `CronJob`, `ServiceAccount`, `ClusterRole`, `ClusterRoleBinding`, `Role`, `RoleBinding`.

---

## Testing

### Running Tests

```bash
bench --site <site> run-tests --app kubeport --doctype <DocType>
```

### Test Conventions

- Test classes use `IntegrationTestCase` by default. Use `UnitTestCase` only for pure, isolated logic.
- For local syntax checks when the Bench environment is unavailable, prefer targeted validation over full bench runs.
- When adding a new API or form behavior, update or add tests close to the changed module.

### Current Coverage

Strongest coverage: discovery behavior, API timeouts, reconciliation state transitions, manifest validation, Helm worker concurrency guards, cleanup/migration patches.

Weakest coverage: Frappe Site job submission end-to-end, `Helm Repository` sync behavior, `Helm Chart` metadata flows, broader cross-DocType integration tests.

---

## Documentation

- Keep `README.md` aligned with the actual shipped feature set, not aspirational plans.
- Use `docs/control-plane-state.md` for current capabilities, open gaps, and robustness notes.
- Use `docs/codebase-summary.md` for module-level architecture summaries.
- When a change affects discovery, background-task behavior, or desired-state semantics, update docs in the same commit.

---

## Common Pitfalls

These are specific mistakes to avoid:

| Pitfall | Why It's Wrong | Correct Approach |
|---|---|---|
| Persisting discovered sites/benches to MariaDB | Violates read-only discovery invariant | Return discovery data as ephemeral API responses |
| Calling Helm CLI from the web thread | Blocks the request; can time out | Enqueue on the `long` queue via `frappe.enqueue` |
| Using `doc.reload()` in a background worker | Creates a race condition with concurrent updates | Use `frappe.db.get_value` for targeted reads, `db_set` for writes |
| Assuming `helm status == deployed` means sites exist | Workload pods may be Pending or CrashLooping | Check actual pod phase and site existence separately |
| Trusting K8s Job exit codes as ground truth | `bench new-site` can exit non-zero despite success | Verify site existence via exec-based discovery before marking Failed |
| Mixing cluster/runtime failures with code bugs | Confuses debugging and root-cause analysis | Document whether a failure is code-path related or a real cluster/runtime issue |
| Hardcoding chart image tags or PVC names | Breaks on chart version upgrades | Dynamically extract from a live reference pod |
| Using `shell=True` in subprocess calls | Security risk, injection vector | Always pass command as a list of strings |
