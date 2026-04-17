# Kubeport Changelog

A decision log for future agents and contributors.  Each entry records what
changed, why the approach was chosen, and what alternatives were rejected.

---

## 2026-04-17 — Frappe Site creation via Kubernetes Jobs

### What changed

New `Frappe Site` DocType and supporting infrastructure for creating ERPNext
sites on running Frappe benches managed by kubeport.

**New files:**
- `kubeport/kubeport/doctype/frappe_site/frappe_site.json` — DocType schema
- `kubeport/kubeport/doctype/frappe_site/frappe_site.py` — controller
- `kubeport/tasks/site_tasks.py` — background task: Job submission
- `kubeport/api/site.py` — `get_site_job_logs()` endpoint

**Modified files:**
- `kubeport/tasks/reconciliation.py` — added `_reconcile_frappe_sites()` to
  the every-5-minute sweep

### How it works

1. User creates a `Frappe Site` document, links it to a `Helm Release` (the
   bench), provides `admin_password` and DB root credentials.
2. Clicking **Create Site** calls `FrappeSite.create_site()`, which sets
   status → `In Progress` and enqueues `create_site_task` on the `long` queue.
3. The background task:
   - Finds a running bench pod (reuses `_select_site_discovery_pod` from
     `discovery.py`) to discover the container image and sites PVC mount.
   - Builds a `batch/v1 Job` manifest modelled after the ERPNext Helm chart's
     `job-create-site.yaml`.
   - Submits the Job via `apply_resource()` (already supports the `Job` kind).
   - Stores the Job name in `creation_job_name` for reconciliation to track.
4. Reconciliation (`_reconcile_frappe_sites`) polls `BatchV1Api.read_namespaced_job`
   every 5 minutes. On `status.succeeded > 0` → `Active`; on
   `status.failed > 0` → `Failed` with pod termination detail.

### Why direct Job submission (not `helm upgrade`)

The ERPNext Helm chart provides `jobs.createSite` — a standard K8s Job
(confirmed: no `helm.sh/hook` annotations) rendered when
`jobs.createSite.enabled: true`.  Using `helm upgrade` to trigger it was
**rejected** for these reasons:

| Problem | Impact |
|---|---|
| Helm values = steady-state config | Toggling `enabled` on/off conflates one-off ops with deployment config |
| Re-fire risk | Any subsequent upgrade re-creates the Job unless the operator manually toggles `enabled` back to `false` |
| Credentials exposed | `adminPassword` / `dbRootPassword` would be stored as plain text in the `values` YAML field |
| No net benefit | We must watch Job status regardless; the Helm layer saves nothing |

Direct Job submission keeps site lifecycle fully independent of the Helm
release, stores only the minimal secret surface (DB root password is
preferably referenced via `db_root_secret` pointing to an existing K8s
Secret), and fits the existing DocType → background task → reconciliation
pattern.

### Why dynamic pod inspection for image and volumes

The Job container must use the same image and PVC mounts as the running
bench to guarantee `bench` is available and the correct sites directory is
mounted.  Hard-coding image tags or PVC naming conventions from the Helm chart
would break on version upgrades or custom chart values.

By reading the spec from a live reference pod (`_select_site_discovery_pod`),
the task is resilient to chart evolution without code changes.

### Key design constraints preserved

- **Async-first**: site creation never blocks a web request.
- **Concurrency safety**: `operation_token` guards against stale workers
  (same pattern as `Service Bundle`).
- **Discovery is still read-only**: `_select_site_discovery_pod` is only
  used to inspect the pod spec; no writes occur during discovery.
- **No desired-state contamination in Helm values**: the Job manifest is
  owned entirely by kubeport and cleaned up by `ttlSecondsAfterFinished`.

### What is NOT implemented (follow-on work)

- Site deletion (`bench drop-site`)
- Site migration (`bench migrate`)
- Backup / restore
- Auto-discovery of DB credentials from Helm release secrets
- UI for displaying `get_site_job_logs` output inline in the form
