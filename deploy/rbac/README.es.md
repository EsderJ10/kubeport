# Kubeport — Manifiestos RBAC

RBAC de Kubernetes de mínimo privilegio para el modo de autenticación `In-Cluster`.
Cada verbo en este árbol se concede porque al menos un lugar de llamada
auditado de Kubeport lo requiere; la matriz de abajo cita la ruta de cada
lugar de llamada.

## Aplicar

```bash
kubectl apply -k deploy/rbac/
```

Esto crea una ServiceAccount `kubeport` en el namespace `kubeport-system`,
dos ClusterRoles (`kubeport:cluster-scoped` y
`kubeport:namespaced`) y dos ClusterRoleBindings que vinculan ambas a la
SA a nivel de clúster. Para reubicar la SA a un namespace distinto, edita
la directiva `namespace:` de `kustomization.yaml` **y** parchea
`subjects[].namespace` en `clusterrolebinding.yaml` (el campo `namespace:`
de kustomize no reescribe los subjects de un ClusterRoleBinding).

Tras aplicar, monta la SA en el pod del bench
(`spec.serviceAccountName: kubeport`) y crea una fila `Kubernetes Cluster`
con `Auth Method = In-Cluster` desde el Desk.

## Justificación de verbos

Auditado desde `kubeport/utils/k8s_resources.py`,
`kubeport/utils/discovery.py`, `kubeport/utils/observability.py` y
los cuatro módulos de tareas bajo `kubeport/tasks/`. Dos clases de verbo:

- **CRUD** = `get, list, create, patch, delete`. Concedido en cada kind
  de la lista permitida de Service Bundle porque `apply_resource` aplica
  server-side (PATCH con `application/apply-patch+yaml`, recurriendo a
  `create` en `404`) y `delete_resource` llama al método `delete_*`
  por kind.
- **Solo lectura** = `get, list` (o solo `get` / `list`). Kinds auxiliares
  que Kubeport solo inspecciona.

| Grupo API | Recursos | Alcance | Verbos | Justificación (lugar de llamada) |
|---|---|---|---|---|
| `""` | `pods`, `services`, `configmaps`, `secrets`, `persistentvolumeclaims`, `serviceaccounts` | namespaced | CRUD | apply / delete de Service Bundle (`utils/k8s_resources.py:apply_resource,delete_resource`); Secrets de almacenamiento de Helm release (driver por defecto); Secret `{job}-creds` de Frappe Site (`tasks/site_tasks.py`); creación del PVC de copia de seguridad (`tasks/site_tasks.py`); lecturas de preparación de release (`utils/release_health.py`). |
| `""` | `pods/log` | namespaced | `get` | Drilldown de logs de pod (`utils/observability.py:read_namespaced_pod_log`). |
| `""` | `pods/exec` | namespaced | `create` | El descubrimiento del bench y la sonda de existencia de site hacen stream con `connect_get_namespaced_pod_exec` (`utils/discovery.py`, `tasks/reconciliation.py`). La API de K8s trata `kubectl exec` como un `create` sobre el subrecurso exec. |
| `""` | `events` | namespaced | `list` | Drilldown de eventos (`utils/observability.py:list_namespaced_event`). |
| `""` | `namespaces` | cluster | CRUD | apply / delete de Namespace de Service Bundle; el barrido de Jobs huérfanos enumera namespaces (`tasks/reconciliation.py`); auto-creación en el primer deploy. |
| `apps` | `deployments`, `statefulsets`, `daemonsets` | namespaced | CRUD | apply / delete de Service Bundle; recorrido de preparación de release (`utils/release_health.py`). |
| `apps` | `replicasets`, `controllerrevisions` | namespaced | `get`, `list` | Drilldown de contexto de rollout (`utils/observability.py:list_namespaced_replica_set,list_namespaced_controller_revision`). |
| `batch` | `jobs`, `cronjobs` | namespaced | CRUD | apply de Service Bundle; todos los Jobs del ciclo de vida de Frappe Site (`tasks/site_tasks.py:_run_site_op`); Jobs de limpieza de archivos; delete del barrido de Jobs huérfanos (`tasks/reconciliation.py`). |
| `networking.k8s.io` | `ingresses` | namespaced | CRUD | apply / delete de Service Bundle; recorrido de preparación. |
| `rbac.authorization.k8s.io` | `roles`, `rolebindings` | namespaced | CRUD | apply / delete de Service Bundle (kinds en la lista permitida de `utils/k8s_resources.py`). |
| `rbac.authorization.k8s.io` | `clusterroles`, `clusterrolebindings` | cluster | CRUD | apply / delete de Service Bundle. El CRUD aquí está restringido por la comprobación de escalada RBAC de K8s — Kubeport no puede conceder verbos que él mismo no posea. |
| `storage.k8s.io` | `storageclasses` | cluster | `list` | Descubrimiento del StorageClass por defecto para charts estilo ERPNext (`tasks/helm_tasks.py`). |
| `events.k8s.io` | `events` | namespaced | `list` | Drilldown de eventos en clústeres que enrutan eventos a través de `events.k8s.io/v1` (`utils/observability.py`). |

Verbos que Kubeport **no** solicita: `pods/portforward`,
`pods/proxy`, cualquier `*/scale`, cualquier `*/finalizers`, cualquier subrecurso
`*/status` más allá de lo que `patch` y `delete` ya cubren, persistent
volumes (`pv` a nivel de clúster), nodes, recursos personalizados / CRDs.

## Justificación de la división por alcance

Los manifiestos separan los verbos a nivel de clúster y los a nivel de namespace
en dos ClusterRoles para que un operador pueda ajustar a mínimo privilegio
por namespace sin tener que volver a derivar la lista de verbos:

- `kubeport:cluster-scoped` cubre los recursos que son inherentemente
  a nivel de clúster (`Namespace`, `StorageClass`, `ClusterRole`,
  `ClusterRoleBinding`). Siempre debe vincularse vía un ClusterRoleBinding.
- `kubeport:namespaced` cubre todo lo demás. La instalación por defecto
  lo vincula a nivel de clúster vía un ClusterRoleBinding de modo que un único
  `kubectl apply -k` produzca un bench operativo. Ajuste a continuación.

## Ajuste a nivel por namespace

Para restringir Kubeport a un conjunto fijo de namespaces de carga de trabajo, elimina
los `subjects` del segundo binding (o elimina el segundo bloque
`ClusterRoleBinding` de `clusterrolebinding.yaml` por completo) y reemplázalo con
un RoleBinding por namespace de carga de trabajo. Un ClusterRole referenciado desde
un RoleBinding acota sus verbos al namespace de ese binding.

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

Aplica uno de estos por cada namespace que Kubeport deba gestionar. Compromisos:

- A favor: Kubeport ya no puede tocar pods / secrets / jobs en namespaces
  arbitrarios — solo aquellos con un RoleBinding explícito.
- En contra: cada nuevo namespace de carga de trabajo requiere un RoleBinding
  explícito antes de que Kubeport pueda desplegar en él. La ruta de apply de Namespace
  de Service Bundle (concedida por `kubeport:cluster-scoped`) crea el namespace
  en sí, pero no puede autoconcederse los verbos a nivel de namespace.

El ClusterRoleBinding de `kubeport:cluster-scoped` debe permanecer — sus
recursos (`Namespace`, `ClusterRole`, `ClusterRoleBinding`,
`StorageClass`) no pueden acotarse a un único namespace.

## Prueba de humo

```bash
make rbac-smoke
```

Ejecuta `kubectl auth can-i` para cada par verbo-recurso de la matriz
de arriba contra la SA vinculada. Establece `KUBEPORT_NAMESPACE` si la SA está en un
namespace que no sea el predeterminado; establece `KUBEPORT_PROBE_NAMESPACE` para que las
sondas a nivel de namespace apunten a un namespace de carga de trabajo. El script
termina con código no cero ante cualquier FAIL e imprime un resumen. El llamante debe tener
permiso para suplantar a la SA kubeport (típicamente cluster-admin) — sin eso,
todas las sondas fallan en la etapa de admisión de la suplantación y la salida
no significa nada.

## Ver también

- [`docs/deploy.md`](../../docs/deploy.md) — guía completa de despliegue fuera del entorno de desarrollo; este árbol RBAC es su sección de autenticación dentro del clúster.
- [`docs/control-plane-state.md`](../../docs/control-plane-state.md) §Service Bundle y §Helm Release Management — explica por qué cada kind de la lista permitida necesita verbos CRUD.
- [`AGENTS.md`](../../AGENTS.md) §Cluster Access Scoping — el invariante que este RBAC hace cumplir en la frontera del clúster.
