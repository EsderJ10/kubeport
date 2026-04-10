# Kubeport - Agent Instructions

## Definition of the project

Kubeport is a Frappe app that allows you to manage your Kubernetes clusters and Helm releases from the Frappe framework. It is a tool intended to be a Control Plane for managing multiple Kubernetes clusters and Helm releases from a single interface.

## Framework & Dependencies
- **Type**: Frappe app (ERPNext framework)
- **Python**: 3.14+
- **Package manager**: flit
- **Install via**: `bench get-app <url> --branch dev/jose` then `bench install-app kubeport`

## Code Quality
- **Pre-commit**: Required (`cd apps/kubeport && pre-commit install`)
- **Lint**: `ruff` (configured in `pyproject.toml`)
- **Format**: `ruff format` for Python, `prettier` for JS/CSS
- **ESLint**: Enabled for JavaScript

## Running Tests
- Run via bench: `bench --site <site> run-tests --app kubeport --doctype <doctype>`
- Tests use Frappe's `IntegrationTestCase`

## Key Architecture
- **DocTypes** in `kubeport/kubeport/doctype/`: helm_release, helm_repository, helm_chart, helm_chart_version, kubernetes_cluster, kubernetes_manifest, kubernetes_command, service_bundle
- **Tasks** in `kubeport/tasks/`: reconciliation, helm_tasks, service_bundle_tasks, manifest_tasks
- **Scheduled tasks** (in `hooks.py`): every 5 min (`reconcile_all_releases`), daily (`sync_all_repos`)
- **API** in `kubeport/api/`: helm.py

## Type Annotations
- `export_python_type_annotations = True` in hooks.py
- Use `frappe.types.DF` for type hints on DocType fields
- `require_type_annotated_api_methods = True` - all whitelisted API methods need type annotations