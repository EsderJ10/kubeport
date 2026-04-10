# Kubeport - Agent Instructions

## Project

Kubeport is a Frappe app for managing Kubernetes clusters, Helm releases, and raw manifests from the Frappe UI. Treat it as a control plane: cluster state is queried live, while desired-state records remain in MariaDB.

## Stack

- Type: Frappe app for ERPNext/Frappe
- Python: 3.14+
- Package manager: `flit`
- Install: `bench get-app <url> --branch dev/jose` then `bench install-app kubeport`

## Repository Rules

- Prefer `rg` / `rg --files` for search and file discovery.
- Use `apply_patch` for manual edits.
- Never revert user changes unless explicitly asked.
- Do not use destructive git commands such as `git reset --hard` or `git checkout --`.
- Keep edits ASCII unless the file already uses non-ASCII text.

## Code Quality

- Pre-commit is expected to be installed in a bench checkout: `cd apps/kubeport && pre-commit install`
- Python formatting: `ruff format`
- Python linting: `ruff`
- JS/CSS formatting: `prettier`
- JavaScript linting: `eslint`

## Tests

- Run app tests through bench: `bench --site <site> run-tests --app kubeport --doctype <doctype>`
- Test classes usually use `IntegrationTestCase`; add `UnitTestCase` only when the logic is isolated.
- For local syntax checks, prefer targeted validation over full bench runs when the bench environment is unavailable.

## Architecture

- DocTypes live in `kubeport/kubeport/doctype/`.
- API endpoints live in `kubeport/api/`.
- K8s and Helm helpers live in `kubeport/utils/`.
- Background jobs live in `kubeport/tasks/`.
- Scheduled jobs are declared in `hooks.py`.
- Discovery is read-only and should not persist discovered cluster state into MariaDB.

## Current Feature Shape

- `Kubernetes Cluster` owns cluster connectivity and live discovery UI.
- Live Helm release discovery is exposed through whitelisted API methods, not through persisted child rows.
- Frappe site discovery should be treated as cluster-scoped, live, and non-blocking.
- For this codebase, prefer async form calls plus client rendering over framework features that load external data during document fetch.

## Type Annotations

- `export_python_type_annotations = True` is enabled in `hooks.py`
- Use `frappe.types.DF` for DocType field type hints
- All whitelisted API methods must be type annotated

## Implementation Expectations

- Keep Kubernetes access scoped to the target cluster document.
- Long-running or failure-prone external calls should degrade gracefully and return partial results when possible.
- When adding a new API or form behavior, update or add tests close to the changed module.
