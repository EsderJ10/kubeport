### Kubeport

Kubeport is a Frappe app that acts as a Kubernetes control plane inside the Frappe UI. It manages cluster connectivity, Helm repositories, Helm releases, and raw Kubernetes manifests while keeping live cluster discovery separate from persisted desired state.

### Current State

The control plane is functional in these areas:

- `Kubernetes Cluster` stores cluster credentials and exposes live discovery in the form UI.
- `Helm Repository` syncs chart metadata from Helm repos into MariaDB.
- Helm repository sync now rebuilds full chart/version inventory from the live repo index and prunes stale chart rows.
- `Helm Chart` stores chart metadata and available versions discovered from repositories.
- `Helm Release` persists desired release state with identity scoped to cluster + namespace + release name, and deploys or uninstalls via background jobs.
- `Service Bundle` persists raw manifest bundles and applies or deletes them via the Kubernetes API with stale-worker protection.
- `Frappe Site` persists desired site intent (site name, admin password, apps to install) and submits Kubernetes Jobs that run `bench new-site` inside running benches.
- Reconciliation sweeps compare desired state with live cluster state and mark drift as `Degraded`. Site creation status is verified against actual site existence on the bench (not just Job exit code) to avoid false negatives.

The current milestone is robustness, especially around discovery and asynchronous cluster operations:

- Discovery is live, read-only, and cluster-scoped.
- Release discovery tolerates partial failures and still returns usable data.
- Site discovery only targets running Frappe workload pods.
- Pod lookup prefers stable labels and falls back to namespace scans when charts are inconsistent.
- Background workers guard against stale duplicate work by re-checking document status before acting.
- Cluster-mutating work is pushed off the web thread into long workers.

### What We Have Achieved

- Multi-auth Kubernetes connectivity:
  - kubeconfig
  - bearer token
  - in-cluster service account auth
- Dev-only TLS verification bypass for kubeconfig and bearer-token local clusters.
- Bearer-token auth now requires either a CA certificate or an explicit dev-only TLS bypass.
- Live namespace discovery for cluster-backed forms.
- Browser-side kubeconfig import, context parsing, context extraction, and local endpoint normalization for containerized dev setups.
- Helm repository registration and chart sync.
- Helm chart metadata and default values retrieval.
- Helm release deploy and uninstall workflows through background jobs.
- Raw manifest deployment through `Service Bundle`.
- Periodic reconciliation for Helm releases, Service Bundles, and Frappe site creation Jobs.
- Frappe site creation workflow: submit Kubernetes Jobs that run `bench new-site` inside running benches, with robust status tracking that checks actual site existence rather than trusting Job exit codes.
- Legacy migration from `Kubernetes Manifest` to `Service Bundle`.
- Cleanup patching for removed legacy DocTypes.

### What Is Left To Do

The main gaps still visible in the codebase are:

- Site lifecycle beyond creation. `Frappe Site` currently supports creation only. Future work includes: deletion (`bench drop-site`), migration (`bench migrate`), backup/restore.
- Site discovery remains observational only — discovered sites are not automatically linked to `Frappe Site` documents.
- Site discovery is intentionally narrow and currently recognizes official `erpnext` chart releases only.
- Service Bundle support is limited to a fixed allowlist of built-in Kubernetes resource kinds; CRDs and arbitrary custom resources are not supported.
- Health reporting is still coarse:
  - Helm releases rely mostly on `helm status`
  - Service Bundles only check resource existence
  - workload readiness, events, and pod-level diagnostics are not surfaced deeply in-app
- Some modules still have thin or placeholder tests, especially around `Helm Repository`, `Helm Chart`, and broader integration flows.
- There is no documented operator guide yet for running Kubeport inside Kubernetes with the required service-account RBAC and Helm binary packaging.

### Codebase Map

- DocTypes: `kubeport/kubeport/doctype/`
- API endpoints: `kubeport/api/`
- Kubernetes and Helm helpers: `kubeport/utils/`
- Background tasks: `kubeport/tasks/`
- Tests: `kubeport/tests/` and DocType-local `test_*.py`
- Patches: `kubeport/patches/`

### Additional Docs

- [Control Plane State](docs/control-plane-state.md)
- [Codebase Summary](docs/codebase-summary.md)

### Installation

Install into a Bench environment:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch dev/jose
bench install-app kubeport
```

### Development

Project details:

- Framework: Frappe / ERPNext app
- Python: `3.14+`
- Build backend: `flit`
- Main Python dependencies: `kubernetes`, `urllib3`, `PyYAML`

Enable pre-commit in your Bench checkout:

```bash
cd apps/kubeport
pre-commit install
```

Formatting and linting tools used by the project:

- `ruff format`
- `ruff`
- `prettier`
- `eslint`

### Tests

Prefer targeted app tests through Bench:

```bash
bench --site <site> run-tests --app kubeport --doctype <doctype>
```

When the full Bench environment is unavailable, use focused unit tests and syntax checks near the changed module.

### Troubleshooting Discovery

If discovery shows releases but zero sites:

1. Confirm the release is an official supported Frappe chart release.
2. Check the target namespace for running workload pods.
3. Verify the release has at least one running Frappe pod, not only infra pods.
4. Inspect pending pod events and PVC state if workloads are stuck.
5. Confirm site creation completed inside the workload before expecting discovery.

Typical cluster-side commands:

```bash
kubectl get pods -n <namespace>
kubectl describe pod -n <namespace> <pod-name>
kubectl get pvc -n <namespace>
kubectl describe pvc -n <namespace>
kubectl get events -n <namespace> --sort-by=.lastTimestamp
```

### License

MIT
