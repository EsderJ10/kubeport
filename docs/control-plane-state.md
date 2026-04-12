# Control Plane State

## Executive Summary

Kubeport already behaves like a real control plane in one important sense: desired state is stored in Frappe DocTypes, while live cluster state is queried on demand from Kubernetes and Helm. The implementation is no longer just CRUD around records. It can connect to clusters, discover live releases, discover sites from running Frappe workloads, deploy Helm releases asynchronously, apply raw manifests asynchronously, and reconcile persisted state against the cluster on a schedule.

The current milestone is robustness. Most of the meaningful recent work is about making discovery and background execution safer under imperfect cluster conditions instead of assuming happy-path behavior.

## What We Have Achieved

### Cluster Connectivity

- `Kubernetes Cluster` supports:
  - kubeconfig auth
  - bearer-token auth
  - in-cluster auth
- Dev-only TLS verification bypass is available for kubeconfig and bearer-token clusters whose local certificates do not match the configured endpoint.
- Connection testing hits the real Kubernetes API and updates the cluster record status.
- Namespace lookup is live and reused by forms that need cluster namespaces.
- Kubeconfig upload flows are browser-driven and allow context extraction without reading server-side files.
- Imported kubeconfig contexts now normalize clearly local-only API server endpoints such as `0.0.0.0` and `127.0.0.1` to a container-reachable gateway IP when possible.

### Desired-State Control Plane

- `Helm Repository` persists repo metadata and triggers background sync into chart records.
- `Helm Chart` stores synced chart metadata, version history, and cached default values.
- `Helm Release` persists desired Helm release intent:
  - chart
  - version
  - namespace
  - cluster
  - values
- `Service Bundle` persists desired raw-manifest intent for supported Kubernetes resource kinds.

### Runtime Execution

- Helm installs, upgrades, uninstalls, repo add, and repo sync run in background jobs.
- Raw manifest apply and delete operations run in background jobs.
- Realtime events trigger form refreshes after worker updates.
- Helm worker execution now uses targeted field reads and explicit updates, which is the correct direction for concurrency safety.
- Helm repository sync jobs now use per-run tokens so stale workers cannot overwrite the newest sync attempt.

### Live Discovery

- Cluster discovery is exposed through `kubeport.api.discovery.get_cluster_discovery`.
- The payload shape is stable and UI-oriented:
  - `benches`
  - `sites`
  - `errors`
- Helm release discovery is cluster-scoped and live.
- Site discovery is release-scoped and live.
- Discovery does not persist discovered benches or sites into MariaDB.

### Robustness Improvements Already Landed

- Discovery no longer assumes that every Helm release is healthy or fully ready.
- Frappe site discovery only runs for supported Frappe releases.
- Pod matching prefers `app.kubernetes.io/instance=<release>` first.
- If label matching misses the actual workload pods, discovery falls back to namespace scanning and matches by:
  - Helm release labels
  - legacy `release` labels
  - pod-name prefix
- Infra-only pods are excluded from site discovery.
- Site discovery only execs into running workload pods.
- Candidate pods are ranked so more stable targets like gunicorn/scheduler are preferred.
- Release-level discovery failures are isolated and returned as partial errors instead of failing the whole cluster response.
- Worker tasks skip stale jobs when the persisted document status no longer matches the queued operation.
- Repo sync tasks roll back partial chart upserts before marking a sync as failed or stale.
- Reconciliation can move documents back from `Degraded` to `Deployed` when live state recovers.

## What The Robustness Milestone Means In Practice

The codebase is now trying to be honest about failure. That is the right milestone.

Before this kind of work, control-plane apps tend to fake certainty: a Helm release is marked deployed, so the UI assumes sites exist; a selector is expected to match, so discovery assumes pod naming is perfect; one release fails, so the whole page fails. The current implementation moves away from that.

The important robustness properties now present are:

- live discovery stays read-only
- partial data is still useful data
- runtime failure is reported instead of hidden
- infra pods are not mistaken for Frappe workloads
- pending workloads are treated as a cluster/runtime problem, not a signal to invent state
- background workers defend against duplicate or stale execution

This is the right shape for a control plane. It separates desired state, observed state, and failure reporting.

## What Is Left To Do

### Discovery And Observability

- Discovery remains a UI payload, not a richer observed-state model. That is acceptable for now, but there is still no deeper in-app drilldown for:
  - pod events
  - PVC failures
  - rollout conditions
  - per-site health
- Supported site discovery is intentionally narrow. The code currently treats official `erpnext` chart releases as discoverable benches. If more chart variants are expected, the discovery contract needs to widen deliberately.
- Discovery data is not linked back to persisted `Helm Release` docs in any richer way than shared names and namespaces.

### Release And Bundle Health

- Helm release health is mostly mapped from Helm runtime status to `Deployed` or `Degraded`.
- Service Bundle health only checks whether expected resources still exist.
- Neither path yet surfaces richer readiness semantics such as pod readiness, rollout completion, failed jobs, or resource condition messages.

### Platform Coverage

- `Service Bundle` only supports a fixed allowlist of built-in resource kinds.
- CRDs and arbitrary custom resources are out of scope in the current implementation.
- There is no first-class support yet for higher-level operational workflows such as rollback, diff, preview, or release-history inspection in the Frappe UI.

### Testing Depth

- The repository has meaningful unit coverage around discovery, tasks, reconciliation, hooks, manifest validation, and migration patches.
- Some areas still need stronger coverage:
  - `Helm Repository`
  - `Helm Chart`
  - broader integration tests that exercise Frappe docs plus background workflows together
  - runtime/error-path coverage for repo sync and Service Bundle worker execution

### Operator Documentation

- The repo documents app development well enough, but there is still room for operator-facing docs covering:
  - required Helm binary availability
  - deployment packaging
  - in-cluster RBAC expectations
  - production hardening guidance

## Done So Far

This is the concise record of work already reflected in the codebase:

- Established Kubeport as a Frappe-based Kubernetes control plane rather than a static registry.
- Added `Kubernetes Cluster` as the cluster connectivity anchor.
- Added multiple auth modes for Kubernetes access.
- Added kubeconfig parsing and context extraction APIs.
- Hardened kubeconfig import by normalizing local-only server endpoints for containerized development flows.
- Added live namespace discovery APIs for forms.
- Added Helm repository registration and background chart syncing.
- Hardened Helm repository sync with per-run tokens and rollback on stale or failed workers.
- Added chart metadata and version persistence.
- Added Helm release desired-state docs with async deploy and uninstall.
- Added manifest-bundle desired-state docs with async apply and delete.
- Added reconciliation sweeps on a five-minute schedule.
- Migrated legacy `Kubernetes Manifest` records to `Service Bundle`.
- Added cleanup for removed legacy DocTypes.
- Implemented live cluster discovery for Helm releases.
- Implemented live site discovery for Frappe benches.
- Hardened discovery with partial errors, infra-pod filtering, fallback pod matching, and running-workload checks.
- Hardened workers with stale-job protection and targeted field reads.
- Added focused tests for discovery, tasks, reconciliation, manifest validation, Helm kubeconfig generation, hooks, and cleanup patches.

## Bottom Line

The control plane is already useful. The current code is strongest in cluster connectivity, desired-state persistence, async execution, live discovery, and the first serious round of robustness work. What remains is not basic plumbing. The remaining work is mostly depth work: broader chart support, richer health signals, stronger operator workflows, and fuller integration coverage.
