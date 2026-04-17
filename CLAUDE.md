# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Kubeport is a Frappe app that acts as a Kubernetes control plane inside the Frappe/ERPNext UI. It manages clusters, Helm releases, and raw Kubernetes manifests (Service Bundles). The core architectural invariant: **desired state lives in MariaDB (DocTypes); observed state is always queried live from the cluster**.

## Commands

**Linting and formatting:**
```bash
ruff format          # Python formatting
ruff                 # Python linting (line-length: 110, target: py314, tab indentation)
prettier             # JS/CSS formatting
eslint               # JavaScript linting
```

**Pre-commit setup** (required in a bench checkout):
```bash
cd apps/kubeport && pre-commit install
```

**Running tests** (requires a running Frappe bench with a site):
```bash
bench --site <site> run-tests --app kubeport --doctype <DocType>
```

## Code Style

- Indentation: **tabs** (not spaces)
- Quote style: **double quotes**
- Type annotations are required on all whitelisted API methods (`export_python_type_annotations = True` in `hooks.py`)
- Use `frappe.types.DF` for DocType field type hints

## Architecture

### Layer Map

| Layer | Path | Responsibility |
|---|---|---|
| DocTypes (desired state) | `kubeport/kubeport/doctype/` | MariaDB-backed documents; one per resource kind |
| API endpoints | `kubeport/api/` | Whitelisted, form-supporting, read-only queries |
| K8s / Helm utilities | `kubeport/utils/` | Stateless clients and helpers |
| Background tasks | `kubeport/tasks/` | All mutating cluster operations |
| Scheduled jobs | `hooks.py` | Reconciliation (every 5 min), daily repo sync |

### Key DocTypes

- **`Kubernetes Cluster`** — cluster credentials (kubeconfig, bearer token, in-cluster); live discovery UI rendered client-side via async form calls
- **`Helm Repository` / `Helm Chart` / `Helm Chart Version`** — desired chart catalog state; synced in background
- **`Helm Release`** — desired Helm release; identity scoped to `cluster/namespace/release_name`
- **`Service Bundle`** — desired raw-manifest intent for supported Kubernetes resource kinds (replaces legacy `Kubernetes Manifest`)

### Critical Design Rules

**Async-first:** All cluster-mutating operations (Helm install/upgrade/uninstall, Service Bundle apply/delete) run in background jobs. Web threads only query.

**Concurrency safety:** Background workers use per-run operation/sync tokens to prevent stale jobs from overwriting newer intent. Workers re-check document status before acting. Never block a new action while a release is `In Progress` or `Uninstalling` without token checks.

**Discovery is read-only:** Discovery must never persist discovered cluster state into MariaDB. Return partial results and degrade gracefully on failure.

**Discovery error taxonomy** — distinguish these cases explicitly:
- No release pods found
- Only infra pods found (mariadb, valkey, etc. — not valid exec targets)
- Workload pods found but none running (cluster/runtime issue, not a code bug)
- exec/listing failure inside a selected pod

**Kubeconfig handling:** Kubeconfig is always imported browser-side; the server never reads from the local filesystem. The API normalizes endpoints for containerized dev environments (converts `0.0.0.0`/`127.0.0.1` to the gateway IP).

**Field updates in tasks:** Prefer targeted field reads and explicit updates over broad `doc.reload()` calls when concurrency matters.

### Scheduled Jobs (`hooks.py`)

- `*/5 * * * *` → `kubeport.tasks.reconciliation.reconcile_all_releases` (drift detection)
- Daily → `kubeport.tasks.helm_tasks.sync_all_repos`

## Documentation Files

- `README.md` — current shipped feature set (keep aligned with reality, not aspirational plans)
- `AGENTS.md` — agent-specific rules (subset of what's here)
- `docs/control-plane-state.md` — current control-plane status, open gaps, robustness milestone notes
- `docs/codebase-summary.md` — module-level architecture summaries

When a change affects discovery, background-task behavior, or desired-state semantics, update these docs in the same commit.
