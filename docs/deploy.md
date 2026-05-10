# Kubeport — Deployment Guide

This guide walks an operator through deploying Kubeport for non-development use:
sizing the bench, baking a Frappe image that includes the Helm CLI, choosing a
topology (in-cluster vs. out-of-cluster), wiring service-account RBAC, surfacing
the internal metrics, and backing up the control plane itself.

If you only need a development install, follow the
[`README.md`](../README.md) and [`CONTRIBUTING.md`](../CONTRIBUTING.md) and skip
straight to [§9 Quick smoke](#9-quick-smoke).

For day-to-day operator workflows once Kubeport is up, see
[`docs/operator-guide.md`](operator-guide.md).

---

## 1. Topology

Kubeport runs as a normal Frappe app inside a Frappe / ERPNext bench. Two
deployment shapes are supported, distinguished by how the bench reaches the
target Kubernetes API server.

| Topology | Bench location | Auth modes | When to pick it |
|---|---|---|---|
| **In-cluster** | A pod inside the *same* Kubernetes cluster Kubeport manages | `In-Cluster` (auto-mounted SA token), or `Kubeconfig` / `Bearer Token` for *other* clusters | The cluster is your primary deployment target; you want the strongest network locality and the smallest credential surface. |
| **Out-of-cluster** | A bench host (VM, container) outside Kubernetes | `Kubeconfig`, `Bearer Token` | You manage one or more clusters from a central operations bench, you cannot run the bench inside the workload cluster, or the bench is shared with non-Kubeport ERPNext workloads. |

The two topologies are not exclusive. An in-cluster bench can still register
*additional* clusters via kubeconfig or bearer-token rows; the auto-mounted
service-account token is only used by `Kubernetes Cluster` rows whose
`auth_method = In-Cluster`.

Trade-offs:

- **Latency and reliability** — an in-cluster bench reaches `kubernetes.default.svc` over the cluster network; out-of-cluster benches go through whatever path the kubeconfig / bearer-token URL resolves to (usually the LB / node port).
- **Credential blast radius** — in-cluster auth has no kubeconfig at rest. Out-of-cluster auth stores the kubeconfig YAML or bearer token as Frappe encrypted fields (see `kubeport/utils/k8s_client.py:_client_from_bearer_token`).
- **Lifecycle coupling** — an in-cluster bench that manages its own cluster cannot trivially recover from a control-plane outage that takes the bench down with it. See [§8 Control-plane backup](#8-control-plane-backup).

---

## 2. Bench image

Kubeport drives Helm via the `helm` CLI as a subprocess (see
`kubeport/utils/helm.py`). The CLI must be present on the bench `PATH`,
both for the web process (which validates / previews) and for the
RQ `long` worker (which runs deploys, upgrades, uninstalls). It must
also be present in any container that runs the scheduler.

Add `helm` to your Frappe bench image. The minimal change to a vanilla
`frappe/erpnext` Docker build is a multi-stage copy:

```dockerfile
# syntax=docker/dockerfile:1.7
FROM alpine:3 AS helm
ARG HELM_VERSION=v3.16.4
RUN apk add --no-cache curl tar \
 && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
    | tar -xz -C /tmp \
 && install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm

FROM ghcr.io/frappe/frappe-worker:latest
COPY --from=helm /usr/local/bin/helm /usr/local/bin/helm
RUN helm version --short
```

Pin `HELM_VERSION` explicitly. Kubeport only invokes the stable Helm 3.x
subset (`helm repo`, `helm search`, `helm upgrade --install`, `helm
uninstall`, `helm get manifest`, `helm history`, `helm rollback`).

### Python dependency matrix

| Component | Pin | Why |
|---|---|---|
| Python | `>= 3.14` (see [`pyproject.toml`](../pyproject.toml)) | Frappe v16 baseline. |
| `kubernetes` | `>= 34.1.0` | Matches the upstream client API surface Kubeport calls (`CoreV1Api`, `AppsV1Api`, `BatchV1Api`, `StorageV1Api`, `EventsV1Api`). |
| `urllib3` | `>= 2.5.0` | Used by the bearer-token TLS bypass path in `_client_from_bearer_token`. |
| `PyYAML` | `>= 6.0.2` | Kubeconfig parsing in `_client_from_kubeconfig`. |

The Frappe app is installed inside the bench in the standard way:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app kubeport <repository-url> --branch main
bench install-app kubeport
bench --site <site> migrate
```

---

## 3. Resource limits

Kubeport runs in three Frappe roles: the **web** process (form requests
and read-only API endpoints), the **RQ `long`** worker (every cluster
mutation), and the **scheduler** (5-minute reconciliation, daily catalog
sync). Kubeport-specific load on each:

| Role | What it does for Kubeport | Recommended baseline |
|---|---|---|
| Web | Form rendering, async `frappe.xcall` discovery, dashboard cards (`kubeport/api/dashboard.py`). No cluster mutations. | 2 vCPU, 1 GiB RAM per replica. |
| RQ `long` worker | All Helm subprocess calls, all K8s `apply`/`delete`, all Frappe Site Job submissions, archive upload / cleanup. The `helm` subprocess is the dominant CPU and RAM consumer. | 2 vCPU, 2 GiB RAM. Run **at least one** worker; multiple workers are safe — every operation rotates a per-run token (`AGENTS.md` §3). |
| Scheduler | Drives `reconcile_all_releases`, `reconcile_site_backups`, `sync_all_repos`, and `enqueue_sync_site_image_catalog` on cron. Reconciliation enqueues into the `long` queue rather than acting directly. | 1 vCPU, 512 MiB RAM. |

Kubeport itself has no Prometheus exporter — the in-process counters live
in Redis via `frappe.cache()` (see [§7 Monitoring](#7-monitoring)). Size
the bench's Redis according to Frappe's normal guidance; the metrics
namespace adds bounded keys (one per counter, plus a 1000-sample rolling
list per histogram).

The authoritative latency number for your environment is the
`HISTOGRAM_HELM_LATENCY` p95 surfaced on the operator dashboard
(`kubeport.api.dashboard.helm_p95_latency_card_value`).

---

## 4. In-cluster auth and RBAC

When Kubeport runs as a pod inside the cluster it manages, the
`In-Cluster` auth mode reads the auto-mounted service-account token at
`/var/run/secrets/kubernetes.io/serviceaccount/token` (see
`kubeport/utils/k8s_client.py:_client_from_incluster`). The pod's
service account therefore needs every API verb Kubeport actually calls.

### Apply the manifests

```bash
kubectl apply -k deploy/rbac/
```

This installs a `kubeport` ServiceAccount in the `kubeport-system`
namespace, two ClusterRoles (`kubeport:cluster-scoped`,
`kubeport:namespaced`), and two ClusterRoleBindings binding both to the
SA cluster-wide. Mount the SA on the bench pod
(`spec.serviceAccountName: kubeport`); then in the Desk create a
`Kubernetes Cluster` row with `Auth Method = In-Cluster` (no other
fields — the pod-mounted token is read at use time).

To verify the bound permissions match every call site Kubeport makes,
run the smoke test:

```bash
make rbac-smoke
```

It runs `kubectl auth can-i --as=system:serviceaccount:kubeport-system:kubeport`
for every verb-resource pair in the matrix below and exits non-zero on
any FAIL. The caller must have impersonation permission (typically
cluster-admin) for `--as` to take effect.

For per-namespace tightening (drop the cluster-wide binding on
`kubeport:namespaced`, replace with one RoleBinding per workload
namespace), see [`deploy/rbac/README.md`](../deploy/rbac/README.md)
§Tightening to per-namespace.

### Verb-resource matrix

The full matrix — verb, resource, scope, and the audited Kubeport call
site that motivates each row — lives in
[`deploy/rbac/README.md`](../deploy/rbac/README.md) §Verb justification,
alongside the manifests that grant it. Kubeport does **not** request
`pods/portforward`, `nodes/*`, persistent volumes, custom resources, or
any `*/scale`, `*/finalizers`, `*/status` subresources beyond what
`delete` and `patch` already cover.

If your operations policy further forbids granting CRUD on namespaced
resources cluster-wide, the per-namespace tightening pattern in
[`deploy/rbac/README.md`](../deploy/rbac/README.md) keeps
`kubeport:namespaced`'s verbs scoped to an explicit set of workload
namespaces (one RoleBinding each), at the cost of an extra
`kubectl apply` per new workload namespace.

---

## 5. Out-of-cluster auth

When Kubeport runs outside the target cluster, register clusters using
either of:

- **Kubeconfig** — paste / upload a kubeconfig YAML. Stored in the
  Frappe Password field on `Kubernetes Cluster.kubeconfig`. The
  endpoint is rewritten on import if it points at `0.0.0.0` /
  `127.0.0.1` / `localhost` (so it remains reachable from a containerised
  bench). Dev-only `Skip TLS verification` is supported but disabled by
  default.
- **Bearer Token** — supply API server URL, token (encrypted at rest),
  and CA certificate. CA is **required** unless `Skip TLS verification`
  is enabled — Kubeport refuses bearer-token auth without a CA.

Both modes use the same scoped client builder (`get_k8s_api_client`),
so RBAC requirements on the remote cluster match [§4 above](#4-in-cluster-auth-and-rbac).

For step-by-step UI walk-throughs see
[`docs/operator-guide.md`](operator-guide.md) §1.

---

## 6. Helm release storage

Kubeport relies on the Helm CLI's own release-state storage, which
defaults to Kubernetes `Secrets` in the release's namespace
(`storage: secret` driver). The RBAC matrix above grants `secrets`
verbs accordingly. If you have switched the cluster's Helm install to
the `configmap` driver, no extra config is needed — `configmaps` verbs
are also granted.

Kubeport does not maintain its own Helm release storage and does not
proxy `helm history` through Frappe; the live `helm` CLI is the source
of truth.

---

## 7. Monitoring

Kubeport exposes its internal observability through the
`kubeport/api/dashboard.py` whitelisted endpoints. These are designed
to feed Frappe Number Cards on the operator workspace; they are also
suitable as scrape targets for any Frappe-aware monitoring agent.

| Endpoint | Returns |
|---|---|
| `kubeport.api.dashboard.internal_metrics_summary` | All counters and the helm-latency histogram in one call. |
| `kubeport.api.dashboard.reconcile_ticks_card_value` | `reconcile_ticks_total` |
| `kubeport.api.dashboard.stale_ops_recovered_card_value` | `stale_ops_recovered_total` |
| `kubeport.api.dashboard.orphan_jobs_swept_card_value` | `orphan_jobs_swept_total` |
| `kubeport.api.dashboard.helm_p95_latency_card_value` | `helm_subprocess_latency_seconds` p95 over the rolling 1000-sample window. |

Counters live in Redis under the `kubeport_metrics` namespace via
`frappe.cache()`, so they are visible to the web process even when
incremented in the `long` worker. There is no Prometheus / OpenMetrics
exposition format — wrap the JSON endpoints in your existing Frappe
authentication if you want to scrape them externally.

Each operation is also tagged with a UUID `correlation_id` threaded
from web → enqueue → worker (`kubeport/utils/metrics.py:correlation_scope`).
Search for `[correlation_id=<uuid>]` in `frappe.logger("kubeport.*")`
output to follow a single operation across processes. Set up your log
aggregator's grep / filter on that prefix.

For the design of the metrics module see
[`docs/architecture.md`](architecture.md) and
[`docs/control-plane-state.md`](control-plane-state.md) §Robustness
Properties (which the counters quantify under load).

---

## 8. Control-plane backup

Kubeport stores its **desired state** in MariaDB via Frappe DocTypes:
`Kubernetes Cluster`, `Helm Repository`, `Helm Chart`, `Helm Release`,
`Service Bundle`, `Kubeport Site Image`, `Frappe Site`, `Frappe Site
Backup`, `Kubernetes Command`, `Kubernetes Command Audit Log`.
Discovery / observed state is **never** persisted, so a control-plane
restore only needs to recover the desired-state rows and the encrypted
field secrets — the live cluster supplies everything else when
reconciliation runs again.

Use Frappe's built-in backup for the bench:

```bash
bench --site <kubeport-site> backup --with-files
```

This produces a database dump (and a public / private files archive
when `--with-files` is set). Store the dump **and**
`sites/<kubeport-site>/site_config.json` — the latter carries the
`encryption_key` JSON field used to decrypt encrypted DocType fields
(kubeconfig payloads, bearer tokens, database root passwords on
`Frappe Site`). Without it, the restored bench cannot decrypt those
secrets. For Frappe's own restore procedure see the upstream Bench docs
on `bench restore`.

For the *managed* sites Kubeport provisions, see
[`docs/operator-guide.md`](operator-guide.md) §Backup and Restore — that
flow uses `Frappe Site Backup` rows and namespace-local
`kubeport-backups` PVCs and is independent of the Kubeport bench's own
backup.

After a restore:

1. Restart the `long` queue worker(s) and the scheduler so the
   reconciliation cron is registered.
2. Run **Test Connection** on each `Kubernetes Cluster` row.
3. The next reconciliation tick reconciles `Helm Release`, `Service
   Bundle`, and `Frappe Site` rows against live cluster state. In-flight
   rows transition through their stale-operation recovery path
   automatically (see [`docs/fault-model.md`](fault-model.md)).

---

## 9. Quick smoke

Once the bench is up, cluster registered, and at least one curated
`Kubeport Site Image` row present (the daily catalog sync seeds these on
install / migrate), run the end-to-end harness:

```bash
make eval
```

This drives the 10-step golden path against a live k3d cluster and
writes a JSON report to `eval/results/<utc-timestamp>.json`. A passing
run confirms cluster connectivity, chart sync, Helm deploy, Frappe Site
create / migrate / backup / restore / drop, and reconciliation
short-circuiting. See [`eval/README.md`](../eval/README.md) for the
full prerequisites list, the per-phase schema, and how to interpret the
report.

For fault-injection scenarios that empirically validate the robustness
defences, run `make eval-faults` (see [`eval/README.md`](../eval/README.md)
§Fault scenarios).

---

## See also

- [`docs/operator-guide.md`](operator-guide.md) — day-to-day workflows once the deploy is live.
- [`docs/architecture.md`](architecture.md) — C4 diagrams, runtime sequences, design invariants.
- [`docs/control-plane-state.md`](control-plane-state.md) — capability surface, robustness defences, open gaps.
- [`docs/fault-model.md`](fault-model.md) — tolerated faults and recovery upper bounds.
- [`docs/threat-model.md`](threat-model.md) — trust boundaries, STRIDE catalogue.
- [`AGENTS.md`](../AGENTS.md) — invariants and implementation patterns.
