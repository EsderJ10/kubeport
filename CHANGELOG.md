# Kubeport Changelog

Architecture decision log for contributors and agents. Each entry records what changed, why the approach was chosen, and what alternatives were rejected.

**Format**: Entries are ordered newest-first. Each entry includes Context (the problem or need), the Decision (what was chosen and why), Rejected Alternatives (what was not chosen and why), and Implementation Details (how it was built).

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
