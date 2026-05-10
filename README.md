# Kubeport

A Frappe app that turns a Frappe / ERPNext bench into a Kubernetes control plane.

Operators connect to clusters, register Helm repositories, declare Helm releases, apply raw Kubernetes manifests, and orchestrate Frappe site lifecycle (`bench new-site`, `migrate`, `backup`, `restore`, `drop-site`) — all from the same Desk UI they already use, with the operational invariants normally associated with platform-engineering tooling: read-only discovery, background-job execution, ground-truth verification, and a 5-minute drift reconciliation loop.

> Defining invariant: **desired state lives in MariaDB through Frappe DocTypes; observed state is always queried live from the cluster.** Discovery never persists to the database. Every cluster mutation runs out of the request thread.

---

## Documentation map

Start with the document that matches what you want to do.

| Goal | Read |
|---|---|
| Understand what Kubeport is and why it was built | [`docs/thesis.md`](docs/thesis.md) |
| Use Kubeport end-to-end (operator workflows) | [`docs/operator-guide.md`](docs/operator-guide.md) |
| Understand the architecture (C4 diagrams, sequences, invariants) | [`docs/architecture.md`](docs/architecture.md) |
| See current capability surface, robustness defences, open gaps | [`docs/control-plane-state.md`](docs/control-plane-state.md) |
| Find a specific module / DocType / API | [`docs/codebase-summary.md`](docs/codebase-summary.md) |
| Contribute (setup, lint, test, PR conventions) | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Architecture decision history | [`CHANGELOG.md`](CHANGELOG.md) |
| Report a security issue | [`SECURITY.md`](SECURITY.md) |
| Detailed invariants and patterns for AI / human contributors | [`AGENTS.md`](AGENTS.md) |

---

## Capabilities

- **Cluster connectivity** — kubeconfig, bearer-token, or in-cluster service-account auth. Browser-side kubeconfig import with automatic endpoint normalisation for containerised dev setups.
- **Helm chart catalogue** — register repos and synchronise chart / version inventory in the background; cached default `values.yaml`; daily refresh.
- **Site image catalogue** — public GHCR Frappe / ERPNext runtime images. Curated rows are digest-pinned and bumped automatically by `.github/workflows/publish-site-image.yml` on every `v*` tag. Operators can also register their own images.
- **Helm release management** — declare desired state scoped to `cluster/namespace/release_name`; idempotent deploy, upgrade, rollback, uninstall through background jobs; live workload-readiness drilldown with pod logs, events, and rollout context.
- **Raw manifest deployment** — `Service Bundle` validates manifests against an allowlist of 17 built-in resource kinds and applies / deletes them via server-side apply.
- **Frappe site lifecycle** — `Frappe Site` orchestrates `bench new-site`, `bench migrate`, `bench backup`, `bench restore`, `bench drop-site` through Kubernetes Jobs cloned from a live bench workload pod. `Frappe Site Backup` is a standalone DocType so backup metadata can outlive the source site row.
- **Live discovery** — read-only enumeration of Helm releases and Frappe sites in any registered cluster. Discovery never persists to MariaDB.
- **Reconciliation** — 5-minute scheduled sweep compares desired and observed state, recovers transient drift, finalises in-flight rows via ground-truth probes, and sweeps orphan Jobs.
- **Operator tools** — `Kubernetes Command` for ad-hoc Get / List / Delete against an allowlist; `Kubernetes Command Audit Log` for an append-only execute history.

For the full robustness inventory see [`docs/control-plane-state.md`](docs/control-plane-state.md).

---

## Architecture at a glance

```
┌──────────────────────────────────────────────────────┐
│                    Frappe Desk                        │
│   (Forms, Realtime Events, Client-Side Discovery)     │
├──────────────┬───────────────────┬────────────────────┤
│  API Layer   │  DocType Layer    │   Task Layer       │
│  (read-only  │  (desired state   │   (background jobs,│
│   queries)   │   in MariaDB)     │    cluster writes) │
├──────────────┴───────────────────┴────────────────────┤
│                  Utility Layer                        │
│ (K8s client, Helm CLI wrapper, discovery, observ.)    │
├───────────────────────────────────────────────────────┤
│           Kubernetes Cluster (live state)             │
└───────────────────────────────────────────────────────┘
```

| Layer | Path | Responsibility |
|---|---|---|
| DocTypes | `kubeport/kubeport/doctype/` | Desired-state documents backed by MariaDB |
| API | `kubeport/api/` | Whitelisted, read-only endpoints for forms |
| Utilities | `kubeport/utils/` | Stateless K8s and Helm integration helpers |
| Tasks | `kubeport/tasks/` | Background jobs for all cluster-mutating work |
| Tests | `kubeport/tests/`, `doctype/*/test_*.py` | Unit and integration tests |
| Patches | `kubeport/patches/` | Schema migration and cleanup |

For C4 diagrams and runtime sequences see [`docs/architecture.md`](docs/architecture.md).

---

## Installation

### Prerequisites

- A running [Frappe Bench](https://frappeframework.com/docs/user/en/bench) (Frappe 16, MariaDB, Redis).
- Python 3.14+.
- [Helm 3](https://helm.sh/docs/intro/install/) on the bench host's `PATH`.
- Network reach from the bench host (or pod) to a Kubernetes cluster (kubeconfig, bearer token, or in-cluster service account).

### Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app kubeport <repository-url> --branch main
bench install-app kubeport
bench --site <site> migrate
```

The first `bench install-app` enqueues the curated site-image catalogue sync onto the `long` queue. The 5-minute reconciliation loop is registered automatically by `hooks.py`.

For the supported dev-container workflow see [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Getting started

1. Open the Desk and navigate to the **Kubeport Operations** workspace.
2. **Connect a cluster** → create a `Kubernetes Cluster` row in your preferred auth mode and run **Test Connection**.
3. **Register a Helm repo** → create a `Helm Repository` row; the chart catalogue syncs in the background.
4. **Deploy a release** → create a `Helm Release` row, pick a chart, edit values (optionally pick a Site Image for ERPNext / Frappe charts), click **Deploy**.
5. **Create a Frappe site** → from a `Helm Release` of an ERPNext bench, create a `Frappe Site` row and click **Create Site**.

Step-by-step instructions for every workflow are in [`docs/operator-guide.md`](docs/operator-guide.md), including back up / restore, cancellation, force uninstall, and the manual smoke-test procedure for pre-release validation.

---

## Development

### Code style

| Concern | Rule | Tool |
|---|---|---|
| Python formatting | tabs, double quotes, 110-char lines | `ruff format` |
| Python linting | repo `pyproject.toml` config | `ruff` |
| JS / CSS formatting | repo defaults | `prettier` |
| JS linting | repo `.eslintrc` | `eslint` |
| Type annotations | required on every `@frappe.whitelist()` method | enforced via `hooks.py` |

### Pre-commit

```bash
cd apps/kubeport
pre-commit install
```

### Testing

```bash
bench --site <site> run-tests --app kubeport
bench --site <site> run-tests --app kubeport --doctype "Helm Release"
```

CI runs the same suite plus `ruff format --check` and `ruff check` on every PR (`.github/workflows/ci.yml`). For details see [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## License

[MIT](LICENSE).
