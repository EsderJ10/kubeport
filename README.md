# Kubeport

A Frappe application that provides a Kubernetes control plane inside the Frappe/ERPNext UI. Kubeport manages cluster connectivity, Helm repositories, Helm releases, raw Kubernetes manifests, and Frappe site provisioning — all while keeping live cluster state separate from persisted desired state.

## Overview

Kubeport bridges the Frappe framework with Kubernetes by following a clear architectural principle: **desired state lives in MariaDB through Frappe DocTypes, while observed state is always queried live from the cluster**. This separation ensures that the UI never displays stale data and that cluster-mutating operations are always explicit, auditable, and asynchronous.

### Key Capabilities

- **Cluster Connectivity** — connect to Kubernetes clusters via kubeconfig, bearer token, or in-cluster service account. Browser-side kubeconfig import with automatic endpoint normalization for containerized development.
- **Helm Chart Catalog** — register Helm repositories and sync chart metadata (versions, default values) into the Frappe database with daily background refresh.
- **Helm Release Management** — declare desired Helm releases (chart, version, namespace, values) and deploy or uninstall them through background jobs with idempotent `helm upgrade --install`.
- **Raw Manifest Deployment** — define raw Kubernetes manifests in Service Bundles and apply or delete them via server-side apply, with a fixed allowlist of supported resource kinds.
- **Frappe Site Provisioning** — create Frappe sites on running ERPNext benches by submitting Kubernetes Jobs that run `bench new-site`, with status verified through ground-truth site existence checks.
- **Live Discovery** — cluster-scoped, read-only discovery of Helm releases and Frappe sites. Discovery results are rendered client-side and never persisted to the database.
- **Reconciliation** — scheduled sweeps (every 5 minutes) compare desired state with live cluster state and flag drift as `Degraded`, with automatic recovery when live state returns to normal.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    Frappe UI                         │
│  (Forms, Realtime Events, Client-Side Discovery)     │
├──────────────┬───────────────────┬───────────────────┤
│  API Layer   │  DocType Layer    │  Task Layer        │
│  (read-only  │  (desired state   │  (background jobs, │
│   queries)   │   in MariaDB)     │   cluster writes)  │
├──────────────┴───────────────────┴───────────────────┤
│              Utility Layer                           │
│  (K8s client, Helm CLI wrapper, discovery helpers)   │
├──────────────────────────────────────────────────────┤
│         Kubernetes Cluster (live state)               │
└──────────────────────────────────────────────────────┘
```

| Layer | Path | Responsibility |
|---|---|---|
| DocTypes | `kubeport/kubeport/doctype/` | Desired-state documents backed by MariaDB |
| API | `kubeport/api/` | Whitelisted read-only endpoints for forms |
| Utilities | `kubeport/utils/` | Stateless K8s and Helm integration helpers |
| Tasks | `kubeport/tasks/` | Background jobs for all cluster-mutating work |
| Tests | `kubeport/tests/`, `doctype/*/test_*.py` | Unit and integration tests |
| Patches | `kubeport/patches/` | Schema migration and cleanup patches |

## Installation

### Prerequisites

- A running [Frappe Bench](https://frappeframework.com/docs/user/en/bench) environment
- Python 3.14+
- [Helm 3](https://helm.sh/docs/intro/install/) CLI available on the server `PATH`
- Access to a Kubernetes cluster (kubeconfig, bearer token, or in-cluster)

### Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch dev/jose
bench install-app kubeport
```

## Development

### Stack

- **Framework**: Frappe / ERPNext app
- **Build backend**: [flit](https://flit.pypa.io/)
- **Python**: 3.14+ (tabs, double quotes, 110-char line length)
- **Dependencies**: `kubernetes`, `urllib3`, `PyYAML`

### Pre-commit

Enable pre-commit hooks in your Bench checkout:

```bash
cd apps/kubeport
pre-commit install
```

### Formatting and Linting

| Tool | Scope |
|---|---|
| `ruff format` | Python formatting |
| `ruff` | Python linting |
| `prettier` | JavaScript / CSS formatting |
| `eslint` | JavaScript linting |

### Type Annotations

Python type annotations are enforced for all whitelisted API methods. Use `frappe.types.DF` for DocType field type hints. Configured via `export_python_type_annotations = True` in `hooks.py`.

## Testing

Run tests through Bench:

```bash
bench --site <site> run-tests --app kubeport --doctype <DocType>
```

When the full Bench environment is unavailable, use focused unit tests and syntax checks near the changed module.

## Troubleshooting

### Discovery Shows Releases but No Sites

1. Confirm the release is an official supported Frappe chart (currently `erpnext`).
2. Check the target namespace for running workload pods — infra pods (MariaDB, Valkey) are not valid discovery targets.
3. Verify the release has at least one **running** Frappe workload pod.
4. Inspect pending pod events and PVC state if workloads are stuck.
5. Confirm site creation completed inside the workload before expecting discovery.

```bash
kubectl get pods -n <namespace>
kubectl describe pod -n <namespace> <pod-name>
kubectl get pvc -n <namespace>
kubectl get events -n <namespace> --sort-by=.lastTimestamp
```

## Documentation

- [Control Plane State](docs/control-plane-state.md) — current capabilities, open gaps, and robustness status
- [Codebase Summary](docs/codebase-summary.md) — module-level architecture and component reference
- [Changelog](CHANGELOG.md) — architecture decision log

## License

MIT
