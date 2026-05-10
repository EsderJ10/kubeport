# Kubeport — RBAC Manifests

Least-privilege Kubernetes RBAC for the `In-Cluster` auth mode. Every
verb in this tree is granted because at least one audited Kubeport call
site requires it; the matrix below cites the path of each call site.

## Apply

```bash
kubectl apply -k deploy/rbac/
```

This creates a `kubeport` ServiceAccount in the `kubeport-system`
namespace, two ClusterRoles (`kubeport:cluster-scoped` and
`kubeport:namespaced`), and two ClusterRoleBindings binding both to the
SA cluster-wide. To relocate the SA to a different namespace, edit
`kustomization.yaml`'s `namespace:` directive **and** patch
`subjects[].namespace` on `clusterrolebinding.yaml` (the kustomize
`namespace:` field does not rewrite ClusterRoleBinding subjects).

After apply, mount the SA on the bench pod
(`spec.serviceAccountName: kubeport`) and create a `Kubernetes Cluster`
row with `Auth Method = In-Cluster` from the Desk.

## Verb justification

Audited from `kubeport/utils/k8s_resources.py`,
`kubeport/utils/discovery.py`, `kubeport/utils/observability.py`, and
the four task modules under `kubeport/tasks/`. Two verb classes:

- **CRUD** = `get, list, create, patch, delete`. Granted on every kind
  in the Service Bundle allowlist because `apply_resource` server-side-
  applies (PATCH with `application/apply-patch+yaml`, falling back to
  `create` on `404`) and `delete_resource` calls the per-kind `delete_*`
  method.
- **Read-only** = `get, list` (or just `get` / `list`). Auxiliary kinds
  Kubeport only inspects.

| API group | Resources | Scope | Verbs | Justification (call site) |
|---|---|---|---|---|
| `""` | `pods`, `services`, `configmaps`, `secrets`, `persistentvolumeclaims`, `serviceaccounts` | namespaced | CRUD | Service Bundle apply / delete (`utils/k8s_resources.py:apply_resource,delete_resource`); Helm release storage Secrets (default driver); Frappe Site `{job}-creds` Secret (`tasks/site_tasks.py`); backup PVC creation (`tasks/site_tasks.py`); release-readiness reads (`utils/release_health.py`). |
| `""` | `pods/log` | namespaced | `get` | Pod log drilldown (`utils/observability.py:read_namespaced_pod_log`). |
| `""` | `pods/exec` | namespaced | `create` | Bench discovery and site-existence probe stream `connect_get_namespaced_pod_exec` (`utils/discovery.py`, `tasks/reconciliation.py`). The K8s API treats `kubectl exec` as a `create` on the exec subresource. |
| `""` | `events` | namespaced | `list` | Event drilldown (`utils/observability.py:list_namespaced_event`). |
| `""` | `namespaces` | cluster | CRUD | Service Bundle Namespace apply / delete; orphan-Job sweep enumerates namespaces (`tasks/reconciliation.py`); first-deploy auto-create. |
| `apps` | `deployments`, `statefulsets`, `daemonsets` | namespaced | CRUD | Service Bundle apply / delete; release-readiness walk (`utils/release_health.py`). |
| `apps` | `replicasets`, `controllerrevisions` | namespaced | `get`, `list` | Rollout-context drilldown (`utils/observability.py:list_namespaced_replica_set,list_namespaced_controller_revision`). |
| `batch` | `jobs`, `cronjobs` | namespaced | CRUD | Service Bundle apply; every Frappe Site lifecycle Job (`tasks/site_tasks.py:_run_site_op`); archive cleanup Jobs; orphan-Job sweep delete (`tasks/reconciliation.py`). |
| `networking.k8s.io` | `ingresses` | namespaced | CRUD | Service Bundle apply / delete; readiness walk. |
| `rbac.authorization.k8s.io` | `roles`, `rolebindings` | namespaced | CRUD | Service Bundle apply / delete (allowlisted kinds in `utils/k8s_resources.py`). |
| `rbac.authorization.k8s.io` | `clusterroles`, `clusterrolebindings` | cluster | CRUD | Service Bundle apply / delete. CRUD here is constrained by the K8s RBAC escalation check — Kubeport cannot grant verbs it does not itself hold. |
| `storage.k8s.io` | `storageclasses` | cluster | `list` | Default-StorageClass discovery for ERPNext-style charts (`tasks/helm_tasks.py`). |
| `events.k8s.io` | `events` | namespaced | `list` | Event drilldown on clusters that route events through `events.k8s.io/v1` (`utils/observability.py`). |

Verbs that Kubeport does **not** request: `pods/portforward`,
`pods/proxy`, any `*/scale`, any `*/finalizers`, any `*/status`
subresources beyond what `patch` and `delete` already cover, persistent
volumes (cluster-scoped `pv`), nodes, custom resources / CRDs.

## Scope split rationale

The manifests separate cluster-scoped and namespaced verbs into two
ClusterRoles so an operator can tighten to per-namespace least privilege
without re-deriving the verb list:

- `kubeport:cluster-scoped` covers resources that are inherently cluster-
  scoped (`Namespace`, `StorageClass`, `ClusterRole`,
  `ClusterRoleBinding`). It must always be bound via a ClusterRoleBinding.
- `kubeport:namespaced` covers everything else. The default install
  binds it cluster-wide via a ClusterRoleBinding so a single
  `kubectl apply -k` yields a working bench. Tightening below.

## Tightening to per-namespace

To restrict Kubeport to a fixed set of workload namespaces, drop
`subjects` of the second binding (or remove `clusterrolebinding.yaml`'s
second `ClusterRoleBinding` block entirely) and replace it with one
RoleBinding per workload namespace. A ClusterRole referenced from a
RoleBinding scopes its verbs to that binding's namespace.

```yaml
# Save as deploy/rbac/overlays/per-namespace/<workload-ns>-rolebinding.yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: kubeport
  namespace: <workload-ns>
subjects:
  - kind: ServiceAccount
    name: kubeport
    namespace: kubeport-system
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: kubeport:namespaced
```

Apply one of these per namespace Kubeport should manage. Trade-offs:

- Pro: Kubeport can no longer touch pods / secrets / jobs in arbitrary
  namespaces — only the ones with an explicit RoleBinding.
- Con: every new workload namespace requires an explicit RoleBinding
  before Kubeport can deploy into it. The Service Bundle Namespace-apply
  path (granted by `kubeport:cluster-scoped`) creates the namespace
  itself, but cannot self-grant the namespaced verbs.

`kubeport:cluster-scoped`'s ClusterRoleBinding must remain — its
resources (`Namespace`, `ClusterRole`, `ClusterRoleBinding`,
`StorageClass`) cannot be scoped to a single namespace.

## Smoke test

```bash
make rbac-smoke
```

Runs `kubectl auth can-i` for every verb-resource pair in the matrix
above against the bound SA. Set `KUBEPORT_NAMESPACE` if the SA is in a
non-default namespace; set `KUBEPORT_PROBE_NAMESPACE` to point
namespaced probes at a workload namespace. The script exits non-zero on
any FAIL and prints a summary. The caller must have permission to
impersonate the kubeport SA (typically cluster-admin) — without that,
every probe fails at the impersonation admission stage and the output
is meaningless.

## See also

- [`docs/deploy.md`](../../docs/deploy.md) — full non-development deploy guide; this RBAC tree is its in-cluster auth section.
- [`docs/control-plane-state.md`](../../docs/control-plane-state.md) §Service Bundle and §Helm Release Management — explains why each allowlisted kind needs CRUD verbs.
- [`AGENTS.md`](../../AGENTS.md) §Cluster Access Scoping — the invariant this RBAC enforces at the cluster boundary.
