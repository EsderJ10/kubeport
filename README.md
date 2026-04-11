### Kubeport

Kubeport is a Frappe app that acts as a Kubernetes control plane inside the Frappe UI. It manages cluster credentials, Helm releases, raw manifest execution, and live discovery of Helm benches and Frappe sites.

### What It Does

- Manage `Kubernetes Cluster` records and connect to real clusters through kubeconfig or token-based auth.
- Deploy and uninstall Helm releases through background jobs.
- Execute raw Kubernetes commands from the UI.
- Discover Helm releases live from the cluster.
- Discover Frappe sites live from supported Helm releases without persisting discovered state to MariaDB.

### Live Discovery Model

Discovery is read-only and cluster-scoped.

- Helm releases shown in the `Kubernetes Cluster` form come from live cluster queries.
- Frappe sites are discovered per release by inspecting running workload pods and listing `/home/frappe/frappe-bench/sites`.
- Discovery returns partial results when possible. A cluster can show releases even when one or more releases fail site discovery.
- Discovery does not write child rows or cache site state in the database.

Current site discovery behavior for official `erpnext` chart releases:

- Release pod matching first tries the Helm label `app.kubernetes.io/instance=<release>`.
- If that selector misses workload pods, discovery falls back to a namespace scan and matches pods by release labels and pod-name prefix.
- Only running Frappe workload pods are valid discovery targets.
- `mariadb`, `valkey`, and other infra-only pods are ignored for site discovery.

### Important Operational Notes

- A Helm release being visible in discovery does not mean its Frappe workloads are ready.
- If all Frappe workload pods for a release are `Pending`, Kubeport will not discover sites for that release.
- If only infra pods are running, Kubeport will report a partial error instead of inventing site data.
- Release deployment and uninstall run in background jobs. Do not trigger duplicate deploys while a release is already `In Progress` or `Uninstalling`.

Common causes of zero discovered sites for a Frappe release:

- the release has no running Frappe workload pod yet
- only database/cache pods are running
- the chart created pods with delayed scheduling because of storage or node constraints
- the site creation job never completed, so there is no site directory to discover

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

Useful repository locations:

- DocTypes: `kubeport/kubeport/doctype/`
- Whitelisted API methods: `kubeport/api/`
- Kubernetes and Helm helpers: `kubeport/utils/`
- Background jobs: `kubeport/tasks/`
- Scheduler hooks: `kubeport/hooks.py`

### Quality Checks

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
- `pyupgrade`

### Tests

Prefer running targeted app tests through Bench:

```bash
bench --site <site> run-tests --app kubeport --doctype <doctype>
```

When the full Bench test environment is unavailable, use targeted syntax checks and focused unit coverage near the changed module.

### Troubleshooting Discovery

If discovery shows releases but zero sites:

1. Check whether the release is a supported Frappe chart release.
2. Check pod state in the target namespace.
3. Confirm at least one Frappe workload pod is `Running`.
4. Inspect pending pod events and PVC state if pods are stuck.
5. Confirm the site creation job completed successfully for the chart values you applied.

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
