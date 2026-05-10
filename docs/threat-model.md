# Kubeport — Threat Model

This document is the trust-boundary analysis for the Kubeport control plane. It complements [`SECURITY.md`](../SECURITY.md) (which states the disclosure policy and the high-level trust assumptions) with a per-boundary STRIDE catalogue, a justification of the destructive-operation allowlist, and a mapping of every whitelisted endpoint and every privileged worker call to the boundary it crosses.

For the architectural context — containers, components, runtime sequences — see [`docs/architecture.md`](architecture.md). For the catalogue of tolerated faults and recovery upper bounds, see [`docs/fault-model.md`](fault-model.md). The eight numbered safety / liveness / eventual-consistency properties enforced by the codebase are listed in [`docs/architecture.md`](architecture.md) §3.

---

## 1. Trust-boundary diagram

```mermaid
graph LR
    Operator([Operator browser<br/>System Manager session])
    Public([Unauthenticated visitor<br/>landing page])

    subgraph Bench[Bench host]
        Web[Frappe Web Worker<br/>request thread]
        Worker[RQ Long Worker<br/>cluster mutations]
        Sched[Scheduler<br/>5-min ticks]
        Helm[Helm 3 binary<br/>subprocess]
        DB[(MariaDB<br/>desired state + encrypted creds)]
        Redis[(Redis RQ<br/>long queue)]
    end

    subgraph Cluster[Target Kubernetes cluster]
        API[Cluster API server]
        BenchPod[Frappe bench pod<br/>kubectl exec target]
        BackupsPVC[(kubeport-backups PVC)]
    end

    GHCR[Public GHCR<br/>digest-pinned site images]

    Operator -. B1 CSRF + session .-> Web
    Public -. B1g allow_guest aggregate .-> Web
    Web -. B2 desired-state read/write .-> DB
    Worker -. B2 desired-state read/write .-> DB
    Web -. B3 enqueue_after_commit .-> Redis
    Sched -. B3 enqueue .-> Redis
    Redis -. B3 dequeue .-> Worker
    Worker -. B4 subprocess.run + per-call kubeconfig .-> Helm
    Worker -. B5 scoped k8s client .-> API
    Helm -. B5 scoped k8s client .-> API
    API -. B6 stream connect_get_namespaced_pod_exec .-> BenchPod
    Worker -- B7 git/HTTPS image refresh --> GHCR
    Cluster -. B8 PVC archives .-> BackupsPVC
```

**Boundary index**

| ID | Boundary | Crossing direction | Carrier |
|---|---|---|---|
| B1 | Operator browser → Frappe web worker | Inbound | Authenticated HTTPS / Frappe session cookie + CSRF |
| B1g | Unauthenticated visitor → Frappe web worker | Inbound | Public HTTPS, single guest endpoint |
| B2 | Frappe processes → MariaDB | Bidirectional, in-process | Frappe ORM + encrypted-field API |
| B3 | Web worker / scheduler → RQ long worker | One-way, deferred | Redis-backed RQ via `frappe.enqueue(..., queue="long", enqueue_after_commit=True)` |
| B4 | Long worker → Helm CLI | One-way, in-process spawn | `subprocess.run(list, ...)` with isolated kubeconfig file |
| B5 | Long worker / Helm → Kubernetes API | Outbound, per-cluster scoped | `kubernetes` Python client via `get_k8s_api_client(cluster_name)` |
| B6 | Cluster API → bench pod (exec) | Outbound, two-way stream | `stream(core_v1.connect_get_namespaced_pod_exec, ...)` |
| B7 | Long worker → public GHCR | Outbound, daily | HTTPS pull of curated catalog; digest-pinned at the row |
| B8 | Cluster Job pod → `kubeport-backups` PVC | Cluster-internal | RWX PVC mounted by the bench backup Job and a busybox probe Job |

---

## 2. Per-boundary analysis

### B1 — Operator browser → Frappe web worker

**Trust direction.** Inward: the operator is trusted only after Frappe has authenticated the session and validated the CSRF token. The endpoint then runs with that operator's role bindings.

**Data crossing.** Form values for desired-state writes (`Kubernetes Cluster` rows including kubeconfig, bearer tokens; `Helm Release` values YAML; `Service Bundle` manifest YAML); read requests for live cluster discovery; ad-hoc operator actions (`Kubernetes Command.execute`).

**Mitigations in code.**

- Frappe session + CSRF token on every `@frappe.whitelist()` endpoint (framework default; `allow_guest=True` is the explicit opt-out).
- Role-gating: every privileged DocType requires `System Manager`. The `Kubernetes Command` permission block is `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.json:161-170`.
- Type-annotated arguments: `require_type_annotated_api_methods = True` in `kubeport/hooks.py` rejects loosely-typed calls before execution.
- Server-side validation of every desired-state write, e.g. kubeconfig endpoint normalization in `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.py`, manifest allowlist in `kubeport/utils/k8s_resources.py`, GHCR coordinate validation in `kubeport/kubeport/doctype/kubeport_site_image/kubeport_site_image.py`.
- Destructive-operation typed confirmations: `Kubernetes Command.confirm_destructive` checked at `validate` and at `execute` (`kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.py:78-91, 113-114`); `Frappe Site.cancel_site` and `Frappe Site.restore_site` likewise.

### B1g — Unauthenticated visitor → Frappe web worker

**Trust direction.** Inward, but the visitor is untrusted. Exactly one endpoint is `allow_guest=True`: `kubeport.api.dashboard.public_helm_release_count` (`kubeport/api/dashboard.py:76-86`).

**Data crossing.** Outbound: a single integer (`frappe.db.count("Helm Release")`). No request body is honored beyond standard Frappe routing.

**Mitigations in code.** The endpoint takes no parameters, returns no row data, and reads only the cardinality of one DocType. No write path is exposed.

### B2 — Frappe processes → MariaDB

**Trust direction.** Internal, in-process. Anyone with database-level read access to the Frappe schema can read whatever a System Manager can read.

**Data crossing.** Desired-state rows; encrypted secret fields (Frappe `Password` fieldtype is encrypted at rest with the bench-level encryption key).

**Mitigations in code.**

- Bearer token uses `Password` fieldtype: `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.json:133` (encrypted at rest).
- Per-Job database-credential Secrets are created in-cluster, owner-referenced to the Job for GC, and never persisted on the bench pod's static env spec: `kubeport/tasks/site_tasks.py:149` (create), `:242` (delete).
- All queries use parameterised Frappe ORM calls (`frappe.db.get_value`, `frappe.db.set_value`); raw SQL is not used in the cluster-mutation path.

**Documented residual risk.** Kubeconfig YAML is stored in a `Code` field, not `Password`, so it is at rest in plaintext in the row. This is called out in `SECURITY.md` ("Anyone who can read those rows from the database can act as the cluster"). Operators are expected to gate database-level access accordingly.

### B3 — Web worker / scheduler → RQ long worker

**Trust direction.** One-way. Control flow leaves the request thread and resumes inside the long worker. Everything that crosses must be re-validated.

**Data crossing.** Job arguments only (typically a docname plus a 128-bit `operation_token`).

**Mitigations in code.**

- `enqueue_after_commit=True` ensures the worker only sees rows that have been committed: e.g. `kubeport/kubeport/doctype/helm_release/helm_release.py:148-155`.
- The worker re-fetches the document and re-checks `operation_token` before any state write — see fault `F5` in `docs/fault-model.md`.
- The queue itself (`long`) isolates cluster-mutation latency from short user-facing jobs.

### B4 — Long worker → Helm CLI

**Trust direction.** In-process spawn into an out-of-process binary on the bench host.

**Data crossing.** Command arguments, an isolated per-call kubeconfig file written to a temp dir, environment variables (`HELM_CACHE_HOME`, `HELM_CONFIG_HOME`, `HELM_DATA_HOME`, `KUBECONFIG`), values YAML on stdin where applicable.

**Mitigations in code.**

- Subprocess invocation is always a list, never `shell=True`: `kubeport/utils/helm.py:516, 525`. Module docstring at `kubeport/utils/helm.py:9` reiterates the rule.
- Per-call timeouts: `_HELM_WORKER_TIMEOUT_SECONDS = 600` (mutations), `_HELM_READ_TIMEOUT_SECONDS = 30` (reads). Defined at `kubeport/utils/helm.py:31`.
- Per-call kubeconfig is written to a temp path, only the long-worker process can read it, and the directory is removed after the call.
- The Helm binary is on the bench host's `PATH` and trusted; supply-chain integrity of that binary is the operator's responsibility (called out in `SECURITY.md` "Hardening recommendations").

### B5 — Long worker / Helm → Kubernetes API

**Trust direction.** Outbound. The cluster trusts whatever credential the kubeconfig / bearer token presents. Kubeport must keep that credential scoped.

**Data crossing.** Authentication material (kubeconfig, bearer token, in-cluster SA token); CRUD requests against the resource kinds in the Service Bundle allowlist plus the Frappe Site Job lifecycle resources; pod-list / pod-log / event reads for observability.

**Mitigations in code.**

- Per-cluster scoped client: `get_k8s_api_client(cluster_name)` in `kubeport/utils/k8s_client.py`. No global state is shared.
- Bearer-token auth without a CA certificate is rejected by default; the dev-only TLS bypass is named `Skip TLS Verification (Development Only)` and gated by a separate checkbox: see `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.json` and the controller validation block.
- Resource-kind allowlist for Service Bundle apply: `kubeport/utils/k8s_resources.py` (17 built-in kinds; CRDs explicitly out of scope).
- Per-call `_request_timeout` on every reconciliation-time and observability-time client call (see fault `F7` in `docs/fault-model.md`).
- Service Bundle apply uses server-side apply (does not impersonate a user; uses the registered cluster credential).

### B6 — Cluster API → bench pod (exec)

**Trust direction.** The Cluster API forwards an exec request from Kubeport into the bench pod's main container. The bench pod is treated as ground truth for site existence (see `docs/control-plane-state.md`).

**Data crossing.** A fixed shell command (`bench list-apps`, `find` over the sites directory) — never operator-supplied; the response stream of stdout/stderr.

**Mitigations in code.**

- Commands are constructed from string literals in code (`kubeport/utils/discovery.py:309-322`, `kubeport/tasks/reconciliation.py:1641` and below), never from operator input. No shell-injection vector reaches the exec channel.
- `_request_timeout` bounds the stream (`_POD_EXEC_TIMEOUT_SECONDS`).
- Three-state probe protects desired-state writes: a transient exec failure returns `unknown` and the row is left in-flight rather than mis-marked `Failed` (fault `F3`).

**Documented residual risk.** Code execution inside the bench pod compromises the ground-truth source. `docs/fault-model.md` §"Faults explicitly out of scope" calls this out.

### B7 — Long worker → public GHCR

**Trust direction.** Outbound only. Kubeport never publishes to GHCR; the publish-site-image workflow runs on a tag push and is out-of-process from the bench.

**Data crossing.** HTTPS pulls of the curated `kubeport/site_images/catalog.json` deltas during the daily site-image catalog sync.

**Mitigations in code.**

- Curated rows must be `ghcr.io/owner/image` repositories with a `sha256:` digest pinned: `kubeport/kubeport/doctype/kubeport_site_image/kubeport_site_image.py` validation.
- The catalog scrub runs in a background task (`kubeport/tasks/site_image_tasks.py`) and is idempotent: re-import never broadens the curated set without a corresponding manifest commit.
- The publish-site-image workflow does not auto-commit — it uploads an artifact and the operator commits the bump through the normal review flow (see [`AGENTS.md`](../AGENTS.md) §"Scheduled Jobs").

### B8 — Cluster Job pod → `kubeport-backups` PVC

**Trust direction.** Cluster-internal. Bench-side backup Jobs write archives; a short-lived busybox probe Job reads the `<archive>.size` sidecar.

**Data crossing.** Backup tarballs and their `.size` sidecar files; a per-archive `archive-delete` cleanup Job.

**Mitigations in code.**

- The PVC is namespace-local; access requires being scheduled into that namespace.
- The probe Job mounts the PVC read-only and runs only the size sidecar read (`kubeport/tasks/reconciliation.py:1245`).
- Archive lifecycle is independent of the source `Frappe Site` row (Invariant 7 in `AGENTS.md`); a deletion of the source site does not orphan the archive.

---

## 3. STRIDE catalog

One row per (boundary, threat) pair with a non-trivial mitigation. Threats that map to "out of scope" (e.g. denial-of-service via legitimate System Manager actions, per `SECURITY.md`) are not duplicated here.

| Boundary | STRIDE | Threat | Mitigation | Witness |
|---|---|---|---|---|
| B1 | Spoofing | Forged session impersonates an operator. | Frappe session + CSRF (framework default). | `kubeport/hooks.py` (no CSRF opt-outs registered for Kubeport routes). |
| B1 | Tampering | Operator submits a manifest with disallowed kinds (e.g. CRD) to escape the allowlist. | Server-side `Service Bundle` validation against the resource-kind allowlist; rejected before enqueue. | `kubeport/utils/k8s_resources.py`, `kubeport/kubeport/doctype/service_bundle/service_bundle.py:44-60`. |
| B1 | Repudiation | Operator deletes a `Pod`/`Job`/`ConfigMap` and denies the action. | Append-only `Kubernetes Command Audit Log` row written per execute, decoupled from the source row so it survives source deletion. | `kubeport/tasks/kubernetes_command_tasks.py:110-143`. |
| B1 | Information disclosure | Browser exfiltrates `Kubernetes Cluster` credentials. | Bearer token uses encrypted `Password` fieldtype; field is not echoed back to forms in plaintext after first save (Frappe default for `Password`). | `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.json:133`. |
| B1 | Denial of service | Operator clicks Deploy / Cancel rapidly to corrupt in-flight state. | Per-run `operation_token` rotated on every enqueue; worker re-checks before writes (fault `F5`). | `kubeport/kubeport/doctype/helm_release/helm_release.py:141`. |
| B1 | Elevation of privilege | Non-System-Manager triggers a destructive ad-hoc op. | DocType-level role check on `Kubernetes Command`, plus typed `confirm_destructive` field gated at `validate` and `execute`. | `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.json:161-170`, `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.py:78-91`. |
| B1g | Information disclosure | Guest endpoint is widened to expose row data. | Endpoint returns only `frappe.db.count("Helm Release")`; takes no parameters. | `kubeport/api/dashboard.py:76-86`. |
| B2 | Information disclosure | Database backup is exfiltrated; bearer tokens leak. | Bearer tokens encrypted at rest via Frappe's `Password` field (encryption key is bench-level). Operators are expected to encrypt DB backups (`SECURITY.md` "Hardening recommendations"). | `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.json:133`. |
| B2 | Tampering | Forged values in a desired-state row mislead reconciliation. | All writes from the worker path go through `db_set` with token re-check; writes from the controller path go through Frappe's validation pipeline. | `kubeport/tasks/site_tasks.py:1357`, `kubeport/tasks/reconciliation.py:1820`. |
| B3 | Spoofing | Job picked up by a worker before the source row is committed. | `enqueue_after_commit=True` on every cluster-mutating enqueue. | `kubeport/kubeport/doctype/helm_release/helm_release.py:148-155`, mirrored in `service_bundle.py:78`, `frappe_site.py:149` (and sibling enqueue sites). |
| B3 | Tampering | Worker acts on rotated state. | Pre-write `operation_token` re-check (fault `F5`). | `kubeport/tasks/site_tasks.py:1357`. |
| B4 | Tampering / EoP | Operator-controlled string injected into a shell command. | `subprocess.run(list, ...)` only; module docstring forbids `shell=True`. | `kubeport/utils/helm.py:516, 525` and module rule at `:9`. |
| B4 | Denial of service | Helm CLI hangs against an unreachable cluster. | Hard timeouts (`_HELM_WORKER_TIMEOUT_SECONDS = 600`, `_HELM_READ_TIMEOUT_SECONDS = 30`). Stuck row recovers via fault `F1`. | `kubeport/utils/helm.py:31`. |
| B4 | Information disclosure | Per-call kubeconfig leaks to other processes on the bench host. | Written to a per-call temp file, removed on subprocess exit. | `kubeport/utils/helm.py` (helm wrapper, `_with_kubeconfig` block). |
| B5 | Spoofing | Cluster API server forgery. | `ca_certificate` mandatory unless dev-only TLS bypass is explicitly enabled. | `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.py` (validation block). |
| B5 | Tampering | Cluster credential scope creeps across registered clusters. | `get_k8s_api_client(cluster_name)` returns a freshly-built scoped client per call; no global cache shared between clusters. | `kubeport/utils/k8s_client.py`. |
| B5 | Denial of service | API server stalls reads. | `_request_timeout` on every read (15 s for orphan-sweep job list, 10 s for storage-class discovery, configurable for per-form pod listing). | `kubeport/tasks/reconciliation.py:1577`, `kubeport/tasks/site_tasks.py:382`, `kubeport/utils/observability.py:63`. |
| B5 | Elevation of privilege | Service Bundle apply mutates a kind outside the allowlist. | Allowlist enforced before apply; CRDs and arbitrary CRs are rejected. | `kubeport/utils/k8s_resources.py`. |
| B6 | Tampering | Operator-supplied string reaches the exec command. | Command is hard-coded in source; no operator input flows to the shell. | `kubeport/utils/discovery.py:309-322`, `kubeport/tasks/reconciliation.py:1641` and below. |
| B6 | Repudiation | Pod-exec result is silently treated as a write. | Three-state probe — transient exec failure returns `unknown` and the next tick retries; final state writes only on a definitive answer. | `kubeport/tasks/reconciliation.py:31, 1192`. |
| B6 | Denial of service | Hung exec stream blocks the worker. | `_request_timeout` (`_POD_EXEC_TIMEOUT_SECONDS`). | `kubeport/utils/discovery.py:333`, `kubeport/tasks/reconciliation.py:1666` (exec_kwargs). |
| B7 | Tampering | Curated catalog is replaced by a forged image reference. | Curated rows are digest-pinned (`sha256:`); the `update_site_catalog` workflow does not auto-commit, so a curated bump goes through normal code review. | `kubeport/kubeport/doctype/kubeport_site_image/kubeport_site_image.py` (digest validation), `.github/workflows/publish-site-image.yml` (artifact-only flow). |
| B8 | Tampering | Truncated archive is mistakenly marked `Available`. | Reconciliation does not trust Job exit codes; the PVC-side `<archive>.size` sidecar probe must also succeed (fault `F9`). | `kubeport/tasks/reconciliation.py:1245`. |

---

## 4. Endpoint × boundary mapping

Every whitelisted entry-point and every privileged worker call is mapped to the boundary it crosses, so the boundary table above is exhaustive over the Kubeport codebase.

### 4.1 Whitelisted endpoints (B1, plus B1g where noted)

| Endpoint | File:line | Crosses |
|---|---|---|
| `get_cluster_namespaces` | `kubeport/api/__init__.py:27` | B1 → B5 (live read) |
| `parse_kubeconfig_contexts` | `kubeport/api/__init__.py:65` | B1 (parse only, no cluster contact) |
| `extract_kubeconfig_context` | `kubeport/api/__init__.py:132` | B1 (parse only) |
| `get_cluster_discovery` | `kubeport/api/discovery.py:19` | B1 → B5 |
| `adopt_helm_release` | `kubeport/api/discovery.py:94` | B1 → B2 (writes a desired-state row from a discovered live release) |
| `get_release_resource_logs` | `kubeport/api/observability.py:28` | B1 → B5 |
| `get_release_resource_events` | `kubeport/api/observability.py:96` | B1 → B5 |
| `get_release_resource_rollout` | `kubeport/api/observability.py:129` | B1 → B5 |
| `get_site_job_logs` | `kubeport/api/site.py:14` | B1 → B5 |
| `list_site_backups` | `kubeport/api/site.py:101` | B1 → B2 |
| `get_site_backup_job_logs` | `kubeport/api/site.py:122` | B1 → B5 |
| `list_site_images` | `kubeport/api/site_images.py:13` | B1 → B2 |
| `count_stale_operations` | `kubeport/api/dashboard.py:32` | B1 → B2 |
| `stale_operations_card_value` | `kubeport/api/dashboard.py:64` | B1 → B2 |
| `public_helm_release_count` | `kubeport/api/dashboard.py:76` | **B1g** → B2 (count only) |
| `Kubernetes Cluster.test_connection` | `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.py:66` | B1 → B5 |
| `Helm Repository.sync_charts` | `kubeport/kubeport/doctype/helm_repository/helm_repository.py:59` | B1 → B3 → B4 |
| `Helm Chart.fetch_default_values` | `kubeport/kubeport/doctype/helm_chart/helm_chart.py:42` | B1 → B4 (read-only `helm show values`) |
| `Helm Release.deploy_release` | `kubeport/kubeport/doctype/helm_release/helm_release.py:125` | B1 → B3 → B4/B5 |
| `Helm Release.uninstall_release` | `kubeport/kubeport/doctype/helm_release/helm_release.py:162` | B1 → B3 → B4/B5 |
| `Helm Release.rollback_release` | `kubeport/kubeport/doctype/helm_release/helm_release.py:203` | B1 → B3 → B4/B5 |
| `Helm Release.get_release_health` | `kubeport/kubeport/doctype/helm_release/helm_release.py:241` | B1 → B5 |
| `Helm Release.load_defaults` | `kubeport/kubeport/doctype/helm_release/helm_release.py:267` | B1 → B4 (read-only) |
| `Helm Release.get_release_history` | `kubeport/kubeport/doctype/helm_release/helm_release.py:304` | B1 → B4 (read-only) |
| `Service Bundle.apply_bundle` | `kubeport/kubeport/doctype/service_bundle/service_bundle.py:44` | B1 → B3 → B5 |
| `Service Bundle.delete_bundle` | `kubeport/kubeport/doctype/service_bundle/service_bundle.py:62` | B1 → B3 → B5 |
| `Frappe Site.create_site` | `kubeport/kubeport/doctype/frappe_site/frappe_site.py:124` | B1 → B3 → B5 |
| `Frappe Site.delete_site` | `kubeport/kubeport/doctype/frappe_site/frappe_site.py:163` | B1 → B3 → B5 |
| `Frappe Site.migrate_site` | `kubeport/kubeport/doctype/frappe_site/frappe_site.py:210` | B1 → B3 → B5 |
| `Frappe Site.backup_site` | `kubeport/kubeport/doctype/frappe_site/frappe_site.py:244` | B1 → B3 → B5 → B8 |
| `Frappe Site.restore_site` | `kubeport/kubeport/doctype/frappe_site/frappe_site.py:292` | B1 → B3 → B5 ← B8 |
| `Frappe Site.cancel_site` | `kubeport/kubeport/doctype/frappe_site/frappe_site.py:368` | B1 → B3 → B5 |
| `Kubernetes Command.execute` | `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.py:98` | B1 → B5 (read inline) or B1 → B3 → B5 (delete enqueue) |

### 4.2 Privileged worker calls (B3 → B4/B5/B6)

| Worker function | File:line | Crosses |
|---|---|---|
| `add_and_sync_repo` | `kubeport/tasks/helm_tasks.py:42` | B4 |
| `sync_repo_charts` | `kubeport/tasks/helm_tasks.py:89` | B4 |
| `sync_all_repos` (scheduled, daily) | `kubeport/tasks/helm_tasks.py:140` | B4 |
| `install_or_upgrade_release` | `kubeport/tasks/helm_tasks.py:174` | B4 → B5 |
| `rollback_release` | `kubeport/tasks/helm_tasks.py:306` | B4 → B5 |
| `uninstall_release` | `kubeport/tasks/helm_tasks.py:430` | B4 → B5 |
| `apply_bundle_task` | `kubeport/tasks/service_bundle_tasks.py:20` | B5 |
| `delete_bundle_task` | `kubeport/tasks/service_bundle_tasks.py:70` | B5 |
| `create_site_task` | `kubeport/tasks/site_tasks.py:163` | B5 |
| `cancel_site_task` | `kubeport/tasks/site_tasks.py:175` | B5 |
| `delete_site_task` | `kubeport/tasks/site_tasks.py:255` | B5 |
| `migrate_site_task` | `kubeport/tasks/site_tasks.py:466` | B5 |
| `backup_site_task` | `kubeport/tasks/site_tasks.py:478` | B5 → B8 |
| `restore_site_task` | `kubeport/tasks/site_tasks.py:542` | B5 ← B8 |
| `delete_backup_archive_task` | `kubeport/tasks/site_tasks.py:597` | B5 → B8 |
| `run_kubernetes_command` | `kubeport/tasks/kubernetes_command_tasks.py:44` | B5 (Delete only; reads run inline at B1) |
| `sync_site_image_catalog` (scheduled, daily) | `kubeport/tasks/site_image_tasks.py:20` | B7 → B2 |
| `reconcile_all_releases` (scheduled, `*/5`) | `kubeport/tasks/reconciliation.py` (registered in `kubeport/hooks.py`) | B5, B6, B2 |
| `reconcile_site_backups` (scheduled, `*/5`) | `kubeport/tasks/reconciliation.py` (registered in `kubeport/hooks.py`) | B5, B6, B8, B2 |

---

## 5. Justification of the Kubernetes Command Delete allowlist

`Kubernetes Command` is the only doctype that exposes ad-hoc cluster operations to the operator. Read actions (`Get`, `List`) accept the full eight-kind read allowlist:

```
Pod, Job, ConfigMap, Secret, PersistentVolumeClaim,
Service, Deployment, StatefulSet
```

Delete actions accept only three kinds — `Pod`, `Job`, `ConfigMap` — defined in `_DELETABLE_KINDS` at `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.py:36`. The exclusions are deliberate. For each excluded kind, this section names the ground reason and the controller that should own the destructive path instead.

| Kind | Excluded from Delete because… | Operator should use |
|---|---|---|
| `Secret` | A live workload's pods may have the Secret projected as env/volume; deleting it does not roll the consuming Pods, leaving them with stale credentials and a confusing failure mode at the next restart. The Secret's lifecycle should be owned by the controller that created it (Helm, the Frappe Site Job), not by an out-of-band operator delete. | `Helm Release` upgrade / uninstall; `Frappe Site` cancel flow (which removes the per-Job credentials Secret via owner reference). |
| `PersistentVolumeClaim` | Deleting a PVC destroys data with no in-band undo. The Frappe Site bench PVC and the `kubeport-backups` PVC carry irreplaceable state; the backup PVC is also designed to outlive the source `Frappe Site` row (Invariant 7). Allowing PVC delete via an audit-log row would defeat the durability guarantee that backup metadata survives source-row deletion. | `Frappe Site Backup` trash flow (which uses an `archive-delete` Job to remove specific archives without disturbing the PVC); cluster-admin tooling for PVC removal. |
| `Deployment` | Deleting a Deployment mass-terminates its replica set and orphans the workload Pods, causing an outage that no Kubeport row reflects. The desired-state row would still claim `Deployed`, breaking the invariant that the row predicts the cluster shape. | `Helm Release` upgrade / uninstall — the only path that updates the desired-state row in the same transaction. |
| `StatefulSet` | Same outage profile as `Deployment`, with the additional cost that StatefulSet-managed PVCs may be retained or deleted depending on `persistentVolumeClaimRetentionPolicy` — a foot-gun for ad-hoc deletes. The Helm-managed lifecycle handles this consistently. | `Helm Release` upgrade / uninstall. |
| `Service` | Deleting a Service silently breaks every consumer (intra-cluster traffic, Ingress backends, Frappe site routing) without surfacing on any Kubeport row. | `Helm Release` or `Service Bundle` (which re-applies via server-side apply and updates the row). |

The kinds that **are** allowed for Delete share a property: the resource is restartable or recoverable on its own.

- `Pod` — the parent controller (Deployment, StatefulSet, Job) recreates it.
- `Job` — already terminal at delete time in normal operation; recreation is the operator's normal recovery path for stuck reconciliation, and the orphan-Job sweep (fault `F6`) already deletes Jobs not referenced by any row.
- `ConfigMap` — recoverable from source (the desired-state row that defined it can re-apply). No data loss.

The double-gate (typed `confirm_destructive` enforced at both `validate` and `execute`, audit row written on every execute) ensures every Delete is recorded by source row and by audit row, and that the audit row outlives the source.

---

## 6. Cross-references

- [`SECURITY.md`](../SECURITY.md) — disclosure policy, in-scope / out-of-scope, hardening recommendations.
- [`docs/architecture.md`](architecture.md) §3 — eight numbered safety / liveness / eventual-consistency properties enforced by code.
- [`docs/fault-model.md`](fault-model.md) — tolerated faults, defences, recovery upper bounds.
- [`docs/control-plane-state.md`](control-plane-state.md) §Robustness Properties — prose inventory of the same defences in product-feature framing.
- [`AGENTS.md`](../AGENTS.md) — non-negotiable invariants for contributors that keep these properties true.
