# Codebase Summary

## Top-Level Structure

- `kubeport/api/`
  - Whitelisted APIs for live cluster interactions.
  - `__init__.py` handles namespace lookup and kubeconfig parsing/extraction.
  - `discovery.py` handles live release and site discovery.
- `kubeport/utils/`
  - Core integration layer for Kubernetes and Helm.
  - `k8s_client.py` builds scoped Kubernetes API clients.
  - `helm.py` wraps the Helm CLI with temporary kubeconfig handling.
  - `discovery.py` contains read-only cluster and site discovery logic.
  - `k8s_resources.py` handles supported manifest parsing and CRUD behavior.
- `kubeport/tasks/`
  - Background execution and reconciliation.
  - `helm_tasks.py` handles repo sync plus Helm release deploy/uninstall.
  - `service_bundle_tasks.py` handles raw manifest apply/delete.
  - `reconciliation.py` performs scheduled drift detection.
- `kubeport/kubeport/doctype/`
  - Frappe document models and form scripts.
  - Main DocTypes are `Kubernetes Cluster`, `Helm Repository`, `Helm Chart`, `Helm Release`, `Service Bundle`, and `Helm Chart Version`.
- `kubeport/tests/`
  - Cross-module unit tests for discovery, tasks, reconciliation, hooks, manifests, patches, and Helm utilities.
- `kubeport/patches/`
  - Migration and cleanup patches for the shift from legacy manifest models to `Service Bundle`.

## Core Runtime Model

The codebase follows a clear split between desired state and observed state.

- Desired state lives in MariaDB through Frappe DocTypes.
- Observed state comes from live Helm and Kubernetes queries.
- Mutating operations are routed through background jobs.
- Scheduled reconciliation compares desired and observed state and updates status fields.

That is the defining architectural decision in the repository.

## Main DocTypes

### `Kubernetes Cluster`

Purpose:
- Stores cluster connectivity configuration.
- Serves as the anchor for all live Kubernetes access.

Key behavior:
- Validates auth-method-specific fields.
- Supports connection testing.
- The client-side form renders live discovery using async API calls.

### `Helm Repository`

Purpose:
- Stores Helm repo configuration and sync settings.

Key behavior:
- Auto-registers new repos with Helm.
- Syncs charts in background jobs.
- Tracks sync status and last sync timestamp.

### `Helm Chart`

Purpose:
- Stores repository-backed chart metadata and version history.

Key behavior:
- Builds Helm chart references.
- Fetches and caches default `values.yaml`.

### `Helm Release`

Purpose:
- Stores desired state for a Helm-managed workload deployment.

Key behavior:
- Validates YAML values.
- Rejects unsafe `local-path` plus `ReadWriteMany` combinations.
- Queues deploy and uninstall through background jobs.
- Tracks release lifecycle state and Helm status detail.

### `Service Bundle`

Purpose:
- Stores desired state for raw Kubernetes manifests.

Key behavior:
- Validates supported manifest content.
- Queues apply and delete through background jobs.
- Tracks bundle lifecycle state.

## API Layer

### `kubeport.api.__init__`

Provides:
- live namespace discovery
- kubeconfig context parsing
- kubeconfig context extraction

These APIs are form-supporting APIs, not long-running orchestration endpoints.

### `kubeport.api.discovery`

Provides:
- cluster-wide live Helm release discovery
- per-release site discovery aggregation
- partial-error reporting in one response payload

This is one of the most important files for the current control-plane UX.

## Utility Layer

### `k8s_client.py`

Strengths:
- supports three auth modes
- avoids mutating global Kubernetes client state
- handles bearer-token CA material carefully

This file is foundational because almost every Kubernetes-facing flow depends on it being safe in a multi-request, multi-worker environment.

### `helm.py`

Strengths:
- stateless wrapper around the Helm binary
- per-call temporary kubeconfig handling
- JSON parsing where supported
- no `shell=True`

This is the repository’s Helm integration boundary.

### `discovery.py`

Strengths:
- read-only by design
- release normalization
- Frappe-chart identification
- fallback pod matching
- workload-only site discovery
- pod ranking for more reliable exec targets

This file contains most of the current robustness milestone logic.

### `k8s_resources.py`

Strengths:
- validates managed manifest content
- supports server-side apply
- supports deletion and existence checks
- keeps Service Bundle scope constrained to a known allowlist

Current limitation:
- no CRD or arbitrary resource support

## Task Layer

### `helm_tasks.py`

Responsibilities:
- repo registration and sync
- release install/upgrade
- release uninstall
- realtime event publication

Notable quality point:
- release workers re-check status before acting, which reduces stale duplicate execution risk.

### `service_bundle_tasks.py`

Responsibilities:
- apply and delete raw manifests in background workers
- update state and publish realtime events

### `reconciliation.py`

Responsibilities:
- scheduled drift detection every five minutes
- Helm release health checks through `helm status`
- Service Bundle health checks through resource existence

This file keeps the control plane honest after out-of-band cluster changes.

## Frontend/Form Behavior

The JavaScript in DocType folders is pragmatic and async-first.

- `Kubernetes Cluster` renders live discovery tables in the form.
- `Helm Release` and `Service Bundle` listen for realtime status updates.
- Namespace suggestions are fetched live from the selected cluster.
- The UI avoids trying to persist externally discovered state during document fetch.

This matches the repository’s architectural direction.

## Tests

The strongest test coverage today is around:

- discovery behavior
- API timeout usage
- reconciliation state transitions
- manifest validation
- Helm worker concurrency safeguards
- cleanup and migration patches

The weakest coverage today is around:

- repository sync behavior end to end
- chart metadata behavior end to end
- broader integration flows across DocTypes and workers

## Patches And Migration Story

The patch layer shows an architectural transition:

- old manifest-centric models existed before
- their data is migrated into `Service Bundle`
- legacy DocTypes are then cleaned up

That is a sign the project is already evolving its domain model rather than staying static.

## Overall Assessment

The codebase is coherent. The boundaries are sensible:

- DocTypes define desired state
- API modules expose live reads
- utils isolate Helm and Kubernetes mechanics
- tasks own side effects
- reconciliation owns drift handling

The current implementation is strongest where control-plane correctness matters most: keeping external calls out of the request thread, keeping discovery read-only, and distinguishing live observed state from persisted intent. The main opportunities now are broader platform coverage, deeper health modeling, and stronger integration testing.
