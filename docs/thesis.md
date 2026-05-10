# Kubeport Context

This document frames the repository as the technical artefact of a project. It states the problem, surveys the prior art, defines the objectives, summarises the methodology, and reports the achieved results against those objectives.

The remaining documents in the repository (`README.md`, `docs/architecture.md`, `docs/operator-guide.md`, `docs/control-plane-state.md`, `docs/codebase-summary.md`, `docs/evaluation.md`, `CHANGELOG.md`) are the technical evidence supporting the claims made here.

---

## 1. Problem Statement

Frappe and ERPNext are widely deployed open-source business platforms. Operators that already run a Frappe instance for ERP, CRM, accounting, or HR have two unrelated control surfaces when they extend their deployment to Kubernetes:

1. The **Frappe Desk UI**, which is where day-to-day operators (administrators, integrators, fractional sysadmins) already work.
2. A separate **Kubernetes toolchain** (`kubectl`, `helm`, dashboards, IaC repositories), which requires a different mental model, a separate identity story, and a separate audit trail.

This split creates real cost:

- Operators must context-switch between two UIs to perform a single business operation (e.g., "create a new customer-facing site on the bench cluster").
- Cluster operations are typically performed by hand with `kubectl` / `helm`, which leaves no first-class audit record inside the same system that records the business state.
- "What is actually deployed?" is answered by reading live cluster state; "what should be deployed?" is answered by reading IaC files in another repository. The two diverge silently.

**The thesis problem**: design and implement a Kubernetes control plane *inside* Frappe that lets an operator manage clusters, Helm releases, raw manifests, and Frappe sites from the same Desk UI they already use, without losing the operational invariants a real Kubernetes operator expects (correctness under concurrency, no silent drift, no false-positive health, ground-truth verification of side effects).

---

## 2. State of the Art

The thesis is positioned against the current generation of Kubernetes management UIs. None of the systems below close the specific gap identified above.

| System | What it does | Why it does not solve this problem |
|---|---|---|
| **Lens** (Mirantis) | Desktop IDE for Kubernetes | Per-operator desktop tool; not a multi-user control plane; not embedded in any business platform. |
| **Headlamp** (CNCF) | Web Kubernetes UI | Generic Kubernetes dashboard; no concept of a Frappe site, ERPNext bench, or business-state link. |
| **Rancher** (SUSE) | Multi-cluster Kubernetes manager | Strong multi-cluster story but is itself a separate product with its own identity, audit, and UI. |
| **OpenShift Console** (Red Hat) | Bundled UI for OpenShift | Tied to OpenShift; does not federate into an external business platform. |
| **Komodor / Spacelift / Argo CD UI** | DevOps-team-facing dashboards | Focus on platform engineers, not operators of business apps; no first-class notion of a Frappe site lifecycle. |
| **`kubectl` + `helm` + IaC repo** | The status quo | No unified audit, drift detection, or business-state link; high operator skill floor. |

The conceptual gap is not "another Kubernetes UI". It is: **a control plane whose unit of management is a Frappe site or ERPNext bench, backed by Kubernetes, integrated into the same Desk where the rest of the business runs**, with the operational invariants normally associated with platform-engineering tooling (background-job execution, drift reconciliation, ground-truth verification) rather than the looser invariants of a UI shell over `kubectl`.

---

## 3. Objectives

The project pursues five objectives. They are stated as testable claims so the results section (§5) can be evaluated against them.

| # | Objective | Success criterion |
|---|---|---|
| O1 | Operators can connect to Kubernetes clusters from the Frappe Desk using any of the three real-world auth modes (kubeconfig, bearer token, in-cluster service account). | A `Kubernetes Cluster` DocType exists, validates each auth mode, supports browser-side kubeconfig import, and successfully exercises a connection test against a real cluster. |
| O2 | Operators can register Helm repositories and deploy / upgrade / rollback / uninstall Helm releases from the Desk. | `Helm Repository`, `Helm Chart`, and `Helm Release` DocTypes exist; chart catalogue is synchronised in the background; deploys execute through background jobs and are idempotent. |
| O3 | Operators can apply and remove arbitrary (allowlisted) raw Kubernetes manifests from the Desk. | A `Service Bundle` DocType validates manifests against a fixed allowlist of built-in resource kinds and applies / deletes them through background jobs using server-side apply. |
| O4 | Operators can create, migrate, back up, restore, and drop Frappe sites on a running ERPNext bench from the Desk. | A `Frappe Site` DocType (linked to a `Helm Release`) and a `Frappe Site Backup` DocType orchestrate the lifecycle through Kubernetes Jobs and verify outcomes via ground-truth bench probes rather than trusting Job exit codes. |
| O5 | The control plane respects the platform-engineering invariants normally associated with reconcile-loop tooling: desired state and observed state are never confused, all cluster-mutating work runs out of the request thread, and concurrent or stale workers cannot corrupt state. | Discovery code path persists nothing to MariaDB; all cluster writes are routed through `frappe.enqueue(... queue="long")`; all background workers carry per-run operation tokens that are re-checked before any state write; a scheduled reconciliation loop runs every 5 minutes. |

---

## 4. Methodology

### 4.1 Architectural decision

The defining architectural decision is the strict separation of **desired state** (Frappe DocTypes backed by MariaDB) from **observed state** (live queries against the cluster). Every later design choice — read-only discovery, background-job-only mutations, ground-truth verification — falls out of this decision. See [`docs/architecture.md`](architecture.md) for the full rationale and the C4 diagrams.

### 4.2 Process

The implementation followed an iterative, capability-driven cycle:

1. Implement the minimum viable version of a capability (e.g., "deploy a Helm release") end to end, including the DocType, the background task, and the form behaviour.
2. Stress-test that capability against imperfect cluster conditions (worker crashes, stale workers, transient probe failures, TTL-expired Jobs, hung pods, missing PVCs) and harden it.
3. Capture the architectural decision and the rejected alternatives in [`CHANGELOG.md`](../CHANGELOG.md).
4. Move to the next capability.

This produced a product whose capability surface (cluster connectivity → Helm releases → raw manifests → Frappe sites → backups → operator tools) is broader than the initial scope, while keeping each capability robust enough to be defended on its own. The current capability and robustness inventory is in [`docs/control-plane-state.md`](control-plane-state.md).

### 4.3 Verification strategy

- **Unit / integration tests** run in CI on every PR (`bench --site test_site run-tests --app kubeport`). The repository has 18 test modules covering discovery, reconciliation state transitions, Helm worker concurrency guards, manifest validation, backup/restore guards, full-lifecycle simulation scenarios, and per-kind workload readiness.
- **Smoke procedures** for behaviour that cannot be credibly asserted from mocked tests (e.g., `bench new-site` against a real bench, ownerReference cascade GC, RBAC sufficiency). The current procedure is documented in [`docs/operator-guide.md`](operator-guide.md); the historical version is archived under [`docs/history/frappe-site-smoke.md`](history/frappe-site-smoke.md).
- **Architecture decision log** in [`CHANGELOG.md`](../CHANGELOG.md) records what was decided, why, and what was rejected, so each design choice is auditable.

---

## 5. Results vs. Objectives

| # | Objective | Status | Evidence |
|---|---|---|---|
| O1 | Cluster connectivity (3 auth modes) | **Met** | `Kubernetes Cluster` DocType implements kubeconfig, bearer token, and in-cluster auth; browser-side kubeconfig import normalises local-only API server endpoints; connection-test endpoint exercises the real cluster API. |
| O2 | Helm release lifecycle | **Met** | `Helm Repository`, `Helm Chart`, `Helm Release` DocTypes; chart catalogue synchronised daily; deploy / upgrade / rollback / uninstall all execute through background jobs; idempotent `helm upgrade --install`; live release health combines Helm status with workload-readiness from `helm get manifest`. |
| O3 | Raw manifest deployment | **Met (within scope)** | `Service Bundle` validates manifests against an allowlist of 17 built-in resource kinds and applies / deletes via server-side apply. CRDs and arbitrary custom resources are out of scope by design. |
| O4 | Frappe site lifecycle | **Met** | `Frappe Site` orchestrates `bench new-site`, `bench drop-site`, `bench migrate`, `bench backup`, `bench restore` through Kubernetes Jobs cloned from a live bench workload pod; `Frappe Site Backup` is a standalone DocType so backup metadata can outlive the source site row; ground-truth bench probes verify outcomes rather than trusting Job exit codes. |
| O5 | Platform-engineering invariants | **Met** | Discovery code path is read-only and never persists to MariaDB; every cluster mutation goes through `frappe.enqueue(... queue="long")`; background workers carry per-run operation / sync tokens that are re-checked before any state write; a 5-minute reconciliation loop detects drift and recovers stale operations; an orphan-Job sweep removes Jobs no row references. |

The empirical numbers behind each "Met" claim are summarised in §6 below and developed in full in [`docs/evaluation.md`](evaluation.md).

The robustness properties table in [`docs/control-plane-state.md`](control-plane-state.md) lists the specific defences implemented for each invariant, including:

- per-run operation tokens with re-check before writes,
- per-call kubeconfig isolation in the Helm subprocess wrapper,
- Job identity validation by `kubeport.io/frappe-site` label before any status write,
- ground-truth probe via pod-exec for site existence,
- PVC-side `<archive>.size` sidecar for backup completion verification,
- typed force-uninstall and destructive-cancel confirmation gates,
- shared workload-readiness classifier between deploy workers and reconciliation.

---

## 6. Evaluación

This section is the per-objective summary of the empirical evaluation. Each row pairs an objective from §3 with the measured number that grounds the corresponding "Met" claim in §5; the source column points at the JSON report under [`eval/results/`](../eval/results/) that produced the number. The full chapter — methodology, per-axis tables, fault-by-fault MTTRs, scaling regression — is in [`docs/evaluation.md`](evaluation.md).

| # | Objective | Measured result | Source |
|---|---|---|---|
| O1 | Cluster connectivity (3 auth modes) | Golden-path `setup_cluster_doc` phase passes end-to-end against a real k3d cluster, exercising the kubeconfig auth path through the `Kubernetes Cluster` DocType. | [`eval/results/sample.json`](../eval/results/sample.json) (`make eval`) |
| O2 | Helm release lifecycle | Golden-path phases `setup_helm_repo`, `verify_chart`, `create_release`, `deploy_release` all pass; release reaches `Deployed` and stays there across the rest of the run. | [`eval/results/sample.json`](../eval/results/sample.json) (`make eval`) |
| O3 | Raw manifest deployment (Service Bundle) | Service Bundle apply / delete state-transition cases pass in the unit test suite. Not part of the operator-facing golden-path harness. | `kubeport/tests/test_reconciliation.py` |
| O4 | Frappe site lifecycle | Golden-path phases `create_site`, `migrate_site`, `backup_site`, `restore_site`, `drop_site` all pass; ground-truth bench probes verify each transition. | [`eval/results/sample.json`](../eval/results/sample.json) (`make eval`) |
| O5 | Platform-engineering invariants | All four documented robustness defences recover within the 30-min stale-op bound (1800 s): `worker_kill_mid_helm_upgrade` MTTR 2.30 s, `job_ttl_expired_before_reconcile` 6.68 s, `pod_exec_timeout_during_site_probe` 9.63 s (1 deferred tick), `corrupt_archive_size_sidecar` 22.34 s with archive removed from PVC. Reconciliation tick scales sublinearly to 1000 rows-per-kind (mean 10.46 s, power-law slope ≈ 0.745) — ≈ 28× under the scheduled 5-min cadence. The comparative baseline (raw `kubectl` + `helm`, same workflow) requires 24 distinct shell commands and 13 operator interventions; Kubeport elides both. | [`eval/results/sample-faults.json`](../eval/results/sample-faults.json) (`make eval-faults`); [`eval/results/sample-scaling.json`](../eval/results/sample-scaling.json) (`make eval-scaling`); [`eval/results/sample-baseline.json`](../eval/results/sample-baseline.json) and [`eval/results/comparison.md`](../eval/results/comparison.md) (`make eval-baseline`). |

Every "Met" status in §5 is now anchored to a measurement in this table or the chapter it links to; the previous formulation asserted readiness without evidence.

---

## 7. Limitations

These are deliberate scope decisions, not bugs:

- **Frappe site discovery is narrowed to the official `erpnext` chart.** Widening to other bench chart variants is design work, not implementation work.
- **`Service Bundle` only supports built-in Kubernetes resource kinds.** CRDs, arbitrary custom resources, and admission-webhook concerns are out of scope.
- **Postgres-backed benches are not supported.** The `db_type` field is locked to `mariadb`; the postgres code paths were intentionally removed (no forward-compatibility shim).
- **Site image registry scope is public GHCR.** No `imagePullSecret` UI is exposed in v1.
- **Backup storage is namespace-local PVCs.** Object-store backends, encryption, retention policies, scheduled backups, and restore-to-different-site-name are out of scope.
- **No production hardening guide is shipped.** RBAC requirements, resource limits, and monitoring guidance are listed as future work in [`docs/control-plane-state.md`](control-plane-state.md).

---

## 8. Future Work

The remaining work is depth work; the core plumbing is in place.

1. **Broader health modelling**: add application-level HTTP probes and CRD-aware health where signals have clear semantics.
2. **Backup depth**: scheduled backups, retention policies, object-store backends, encryption, cross-cluster restore, restore-to-different-site-name.
3. **Wider chart support**: controlled expansion of bench discovery beyond `erpnext`.
4. **Operator hardening guide**: RBAC manifests, Helm binary packaging, deployment topology, monitoring.
5. **Cross-DocType integration tests**: broader workflow coverage to complement the strong per-module coverage already in place.

---

## 9. References (Repository-Internal)

| Document | Role |
|---|---|
| [`README.md`](../README.md) | User-facing entry point and quick start. |
| [`docs/architecture.md`](architecture.md) | C4 context / container / component diagrams, sequence diagrams, design invariants. |
| [`docs/operator-guide.md`](operator-guide.md) | How to use Kubeport end-to-end. |
| [`docs/control-plane-state.md`](control-plane-state.md) | Capability and robustness inventory; open gaps. |
| [`docs/codebase-summary.md`](codebase-summary.md) | Module-level architecture reference. |
| [`docs/evaluation.md`](evaluation.md) | Empirical evaluation chapter — functional, reliability, baseline, scaling. |
| [`AGENTS.md`](../AGENTS.md) | Authoritative invariants and implementation patterns. |
| [`CHANGELOG.md`](../CHANGELOG.md) | Architecture decision log (what / why / rejected alternatives). |
| [`CONTRIBUTING.md`](../CONTRIBUTING.md) | Development setup, lint and test workflow. |
| [`SECURITY.md`](../SECURITY.md) | Security disclosure policy. |
| [`docs/history/`](history/) | Archived planning documents kept for thesis traceability. |

---

## 10. Project Components

This thesis describes the backend artefact (Kubeport). The complete TFG deliverable comprises three repositories:

| Component | Repository | Role |
|---|---|---|
| Backend / control plane | [`EsderJ10/kubeport`](https://github.com/EsderJ10/kubeport) (this repo) | Frappe app, the technical artefact this document describes. |
| Marketing landing page | [`1DAW-victorjim551/lp-KubePort`](https://github.com/1DAW-victorjim551/lp-KubePort) | Public-facing site, deployed at [`1daw-victorjim551.github.io/lp-KubePort`](https://1daw-victorjim551.github.io/lp-KubePort/). Authored by Víctor Jiménez. |
| Project umbrella | [`EsderJ10/tfg`](https://github.com/EsderJ10/tfg) | Dev-container, design notes, task tracker. |
