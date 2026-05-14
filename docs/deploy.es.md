# Kubeport — Guía de despliegue

Esta guía recorre, junto a un operador, el despliegue de Kubeport para uso no
relacionado con el desarrollo: dimensionar el bench, cocinar una imagen de
Frappe que incluya la CLI de Helm, elegir una topología (dentro del clúster
vs. fuera del clúster), cablear el RBAC de la cuenta de servicio, exponer las
métricas internas y respaldar el propio plano de control.

Si solo necesitas una instalación de desarrollo, sigue el
[`README.md`](../README.md) y [`CONTRIBUTING.md`](../CONTRIBUTING.md) y salta
directamente a [§9 Comprobación rápida](#9-comprobación-rápida).

Para los flujos de trabajo del operador del día a día una vez Kubeport está en
marcha, consulta [`docs/operator-guide.md`](operator-guide.md).

---

## 1. Topología

Kubeport se ejecuta como una app normal de Frappe dentro de un bench Frappe /
ERPNext. Se admiten dos formas de despliegue, distinguidas por cómo el bench
alcanza el servidor de API de Kubernetes objetivo.

| Topología | Ubicación del bench | Modos de autenticación | Cuándo elegirla |
|---|---|---|---|
| **In-cluster** | Un pod dentro del *mismo* clúster de Kubernetes que Kubeport gestiona | `In-Cluster` (token de SA automontado), o `Kubeconfig` / `Bearer Token` para *otros* clústeres | El clúster es tu objetivo principal de despliegue; quieres la mayor localidad de red y la menor superficie de credenciales. |
| **Out-of-cluster** | Un host de bench (VM, contenedor) fuera de Kubernetes | `Kubeconfig`, `Bearer Token` | Gestionas uno o más clústeres desde un bench central de operaciones, no puedes ejecutar el bench dentro del clúster de cargas de trabajo, o el bench se comparte con cargas de ERPNext no relacionadas con Kubeport. |

Las dos topologías no son excluyentes. Un bench in-cluster puede registrar
clústeres *adicionales* mediante filas con kubeconfig o bearer-token; el token
de la cuenta de servicio automontado solo lo usan las filas `Kubernetes
Cluster` cuyo `auth_method = In-Cluster`.

Compromisos:

- **Latencia y fiabilidad** — un bench in-cluster alcanza `kubernetes.default.svc` por la red del clúster; los benches out-of-cluster van por la ruta que resuelva la URL del kubeconfig / bearer-token (normalmente el LB / node port).
- **Radio de explosión de credenciales** — la autenticación in-cluster no tiene kubeconfig en reposo. La autenticación out-of-cluster almacena el YAML del kubeconfig o el bearer token como campos encriptados de Frappe (consulta `kubeport/utils/k8s_client.py:_client_from_bearer_token`).
- **Acoplamiento de ciclo de vida** — un bench in-cluster que gestiona su propio clúster no puede recuperarse trivialmente de una caída del plano de control que se lo lleve por delante. Consulta [§8 Copia de seguridad del plano de control](#8-copia-de-seguridad-del-plano-de-control).

---

## 2. Imagen del bench

Kubeport conduce Helm a través de la CLI `helm` como subproceso (consulta
`kubeport/utils/helm.py`). La CLI debe estar presente en el `PATH` del bench,
tanto para el proceso web (que valida / previsualiza) como para el worker RQ
`long` (que ejecuta despliegues, actualizaciones, desinstalaciones). También
debe estar presente en cualquier contenedor que ejecute el scheduler.

Añade `helm` a tu imagen de Frappe bench. El cambio mínimo a una construcción
Docker `frappe/erpnext` estándar es una copia multi-etapa:

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

Ancla `HELM_VERSION` explícitamente. Kubeport solo invoca el subconjunto
estable de Helm 3.x (`helm repo`, `helm search`, `helm upgrade --install`,
`helm uninstall`, `helm get manifest`, `helm template`, `helm history`,
`helm rollback`). El host del bench (o pod) debe tener alcance de red de
salida a registries OCI — por defecto las releases de Frappe/ERPNext también
instalan un chart hermano de MariaDB de Bitnami descargado de
`oci://registry-1.docker.io/bitnamicharts/mariadb`. Los operadores que
prefieran cablear una base de datos externa pueden marcar **Use External
Database** en cada fila Helm Release para optar por no usarla (consulta la
guía del operador §3.5).

### Matriz de dependencias Python

| Componente | Pin | Por qué |
|---|---|---|
| Python | `>= 3.14` (consulta [`pyproject.toml`](../pyproject.toml)) | Línea base de Frappe v16. |
| `kubernetes` | `>= 34.1.0` | Coincide con la superficie de la API del cliente upstream que Kubeport llama (`CoreV1Api`, `AppsV1Api`, `BatchV1Api`, `StorageV1Api`, `EventsV1Api`). |
| `urllib3` | `>= 2.5.0` | Usada por la vía de bypass TLS para bearer-token en `_client_from_bearer_token`. |
| `PyYAML` | `>= 6.0.2` | Parseo de kubeconfig en `_client_from_kubeconfig`. |

La app de Frappe se instala dentro del bench de la forma estándar:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app kubeport <repository-url> --branch main
bench install-app kubeport
bench --site <site> migrate
```

---

## 3. Límites de recursos

Kubeport corre en tres roles de Frappe: el proceso **web** (peticiones de
formulario y endpoints de API de solo lectura), el worker **RQ `long`** (toda
mutación del clúster) y el **scheduler** (reconciliación cada 5 minutos,
sincronización diaria del catálogo). Carga específica de Kubeport sobre cada
uno:

| Rol | Lo que hace por Kubeport | Línea base recomendada |
|---|---|---|
| Web | Renderizado de formularios, descubrimiento asíncrono mediante `frappe.xcall`, cards del dashboard (`kubeport/api/dashboard.py`). Sin mutaciones del clúster. | 2 vCPU, 1 GiB RAM por réplica. |
| Worker RQ `long` | Todas las llamadas a subprocesos de Helm, todos los `apply`/`delete` de K8s, todos los envíos de Jobs de Frappe Site, subida / limpieza de archivos. El subproceso `helm` es el consumidor dominante de CPU y RAM. | 2 vCPU, 2 GiB RAM. Ejecuta **al menos un** worker; varios workers son seguros — cada operación rota un token por ejecución (`AGENTS.md` §3). |
| Scheduler | Lanza `reconcile_all_releases`, `reconcile_site_backups`, `sync_all_repos` y `enqueue_sync_site_image_catalog` por cron. La reconciliación encola en la cola `long` en lugar de actuar directamente. | 1 vCPU, 512 MiB RAM. |

Kubeport en sí no tiene un exportador de Prometheus — los contadores en el
proceso viven en Redis mediante `frappe.cache()` (consulta
[§7 Monitorización](#7-monitorización)). Dimensiona el Redis del bench según
la guía habitual de Frappe; el namespace de métricas añade claves acotadas
(una por contador, más una lista circular de 1000 muestras por histograma).

El número autoritativo de latencia para tu entorno es el p95 de
`HISTOGRAM_HELM_LATENCY` expuesto en el dashboard del operador
(`kubeport.api.dashboard.helm_p95_latency_card_value`).

---

## 4. Autenticación in-cluster y RBAC

Cuando Kubeport corre como pod dentro del clúster que gestiona, el modo de
autenticación `In-Cluster` lee el token de la cuenta de servicio automontado
en `/var/run/secrets/kubernetes.io/serviceaccount/token` (consulta
`kubeport/utils/k8s_client.py:_client_from_incluster`). Por tanto, la cuenta
de servicio del pod necesita cada verbo de la API que Kubeport efectivamente
llama.

### Aplicar los manifiestos

```bash
kubectl apply -k deploy/rbac/
```

Esto instala una ServiceAccount `kubeport` en el namespace `kubeport-system`,
dos ClusterRoles (`kubeport:cluster-scoped`, `kubeport:namespaced`) y dos
ClusterRoleBindings que enlazan ambos a la SA en todo el clúster. Monta la SA
en el pod del bench (`spec.serviceAccountName: kubeport`); luego, en el Desk,
crea una fila `Kubernetes Cluster` con `Auth Method = In-Cluster` (sin otros
campos — el token montado en el pod se lee en el momento de uso).

Para verificar que los permisos vinculados coinciden con cada call site que
Kubeport ejecuta, lanza el smoke test:

```bash
make rbac-smoke
```

Ejecuta `kubectl auth can-i --as=system:serviceaccount:kubeport-system:kubeport`
para cada par verbo-recurso de la matriz siguiente y termina con código
distinto de cero ante cualquier FAIL. El llamante debe tener permiso de
suplantación (normalmente cluster-admin) para que `--as` surta efecto.

Para apretar por namespace (eliminar el binding cluster-wide de
`kubeport:namespaced`, reemplazarlo por un RoleBinding por namespace de carga
de trabajo), consulta [`deploy/rbac/README.md`](../deploy/rbac/README.md)
§Tightening to per-namespace.

### Matriz verbo-recurso

La matriz completa — verbo, recurso, alcance y el call site auditado de
Kubeport que motiva cada fila — vive en
[`deploy/rbac/README.md`](../deploy/rbac/README.md) §Verb justification, junto
a los manifiestos que la otorgan. Kubeport **no** solicita `pods/portforward`,
`nodes/*`, persistent volumes, recursos personalizados ni ningún subrecurso
`*/scale`, `*/finalizers`, `*/status` más allá de lo que `delete` y `patch`
ya cubren.

Si tu política operativa prohíbe además otorgar CRUD sobre recursos con
alcance de namespace a nivel de todo el clúster, el patrón de apretar por
namespace en [`deploy/rbac/README.md`](../deploy/rbac/README.md) mantiene los
verbos de `kubeport:namespaced` limitados a un conjunto explícito de
namespaces de carga de trabajo (un RoleBinding por cada uno), a costa de un
`kubectl apply` extra por cada nuevo namespace de carga de trabajo.

---

## 5. Autenticación out-of-cluster

Cuando Kubeport se ejecuta fuera del clúster objetivo, registra clústeres
usando una de estas opciones:

- **Kubeconfig** — pega / sube un YAML de kubeconfig. Almacenado en el campo
  Frappe Password de `Kubernetes Cluster.kubeconfig`. El endpoint se
  reescribe en la importación si apunta a `0.0.0.0` / `127.0.0.1` /
  `localhost` (para que siga siendo alcanzable desde un bench contenerizado).
  El `Skip TLS verification` exclusivo para desarrollo está soportado pero
  deshabilitado por defecto.
- **Bearer Token** — proporciona URL del servidor de API, token (encriptado
  en reposo) y certificado CA. La CA es **obligatoria** salvo que `Skip TLS
  verification` esté habilitado — Kubeport rechaza la autenticación
  bearer-token sin CA.

Ambos modos usan el mismo constructor de cliente con alcance
(`get_k8s_api_client`), por lo que los requisitos de RBAC sobre el clúster
remoto coinciden con [§4 anterior](#4-autenticación-in-cluster-y-rbac).

Para recorridos paso a paso de la UI consulta
[`docs/operator-guide.md`](operator-guide.md) §1.

---

## 6. Almacenamiento de releases de Helm

Kubeport se apoya en el almacenamiento de estado de release de la propia CLI
de Helm, que por defecto son `Secrets` de Kubernetes en el namespace de la
release (driver `storage: secret`). La matriz RBAC anterior otorga los verbos
sobre `secrets` en consecuencia. Si has cambiado la instalación de Helm del
clúster al driver `configmap`, no hace falta configuración extra — los verbos
sobre `configmaps` también están otorgados.

Kubeport no mantiene su propio almacenamiento de releases de Helm y no hace
proxy de `helm history` a través de Frappe; la CLI `helm` en vivo es la
fuente de verdad.

---

## 7. Monitorización

Kubeport expone su observabilidad interna mediante los endpoints whitelisted
de `kubeport/api/dashboard.py`. Están diseñados para alimentar Number Cards
de Frappe en el espacio de trabajo del operador; también son adecuados como
objetivos de scrape para cualquier agente de monitorización compatible con
Frappe.

| Endpoint | Devuelve |
|---|---|
| `kubeport.api.dashboard.internal_metrics_summary` | Todos los contadores y el histograma de latencia de helm en una sola llamada. |
| `kubeport.api.dashboard.reconcile_ticks_card_value` | `reconcile_ticks_total` |
| `kubeport.api.dashboard.stale_ops_recovered_card_value` | `stale_ops_recovered_total` |
| `kubeport.api.dashboard.orphan_jobs_swept_card_value` | `orphan_jobs_swept_total` |
| `kubeport.api.dashboard.helm_p95_latency_card_value` | p95 de `helm_subprocess_latency_seconds` sobre la ventana circular de 1000 muestras. |

Los contadores viven en Redis bajo el namespace `kubeport_metrics` mediante
`frappe.cache()`, por lo que son visibles para el proceso web incluso cuando
se incrementan en el worker `long`. No hay formato de exposición Prometheus /
OpenMetrics — envuelve los endpoints JSON en tu autenticación Frappe
existente si quieres scrapearlos externamente.

Cada operación se etiqueta además con un `correlation_id` UUID enhebrado de
web → enqueue → worker (`kubeport/utils/metrics.py:correlation_scope`).
Busca `[correlation_id=<uuid>]` en la salida de `frappe.logger("kubeport.*")`
para seguir una sola operación a través de procesos. Configura el grep /
filtro de tu agregador de logs sobre ese prefijo.

Para el diseño del módulo de métricas consulta
[`docs/architecture.md`](architecture.md) y
[`docs/control-plane-state.md`](control-plane-state.md) §Robustness
Properties (que los contadores cuantifican bajo carga).

---

## 8. Copia de seguridad del plano de control

Kubeport almacena su **estado deseado** en MariaDB mediante DocTypes de
Frappe: `Kubernetes Cluster`, `Helm Repository`, `Helm Chart`, `Helm
Release`, `Service Bundle`, `Kubeport Site Image`, `Frappe Site`, `Frappe
Site Backup`, `Kubernetes Command`, `Kubernetes Command Audit Log`. El
descubrimiento / estado observado **nunca** se persiste, por lo que una
restauración del plano de control solo necesita recuperar las filas de
estado deseado y los secretos de los campos encriptados — el clúster en vivo
suministra todo lo demás cuando la reconciliación vuelve a ejecutarse.

Usa el backup integrado de Frappe para el bench:

```bash
bench --site <kubeport-site> backup --with-files
```

Esto produce un volcado de la base de datos (y un archivo de ficheros
públicos / privados cuando se establece `--with-files`). Guarda el volcado
**y** `sites/<kubeport-site>/site_config.json` — este último contiene el
campo JSON `encryption_key` usado para desencriptar los campos encriptados
del DocType (payloads de kubeconfig, bearer tokens, contraseñas root de base
de datos en `Frappe Site`). Sin él, el bench restaurado no puede
desencriptar esos secretos. Para el propio procedimiento de restauración de
Frappe consulta la documentación upstream de Bench sobre `bench restore`.

Para los sites *gestionados* que Kubeport aprovisiona, consulta
[`docs/operator-guide.md`](operator-guide.md) §Backup and Restore — ese flujo
usa filas `Frappe Site Backup` y PVCs `kubeport-backups` locales al namespace
y es independiente del propio backup del bench de Kubeport.

Tras una restauración:

1. Reinicia el worker (o workers) de la cola `long` y el scheduler para que
   el cron de reconciliación quede registrado.
2. Ejecuta **Test Connection** sobre cada fila `Kubernetes Cluster`.
3. El siguiente tick de reconciliación reconcilia las filas `Helm Release`,
   `Service Bundle` y `Frappe Site` contra el estado real del clúster. Las
   filas en vuelo transicionan automáticamente por su vía de recuperación de
   operación obsoleta (consulta [`docs/fault-model.md`](fault-model.md)).

---

## 9. Comprobación rápida

Una vez el bench esté en marcha, el clúster registrado y haya al menos una
fila `Kubeport Site Image` curada presente (la sincronización diaria del
catálogo las siembra al instalar / migrar), ejecuta el harness de extremo a
extremo:

```bash
make eval
```

Esto conduce el camino dorado de 10 pasos contra un clúster k3d en vivo y
escribe un informe JSON en `eval/results/<utc-timestamp>.json`. Una
ejecución que pasa confirma la conectividad con el clúster, la sincronización
de charts, el deploy de Helm, la creación / migración / backup / restore /
drop de Frappe Site, y el cortocircuito de la reconciliación. Consulta
[`eval/README.md`](../eval/README.md) para la lista completa de requisitos
previos, el esquema por fase y cómo interpretar el informe.

Para los escenarios de inyección de fallos que validan empíricamente las
defensas de robustez, ejecuta `make eval-faults` (consulta
[`eval/README.md`](../eval/README.md) §Fault scenarios).

---

## Véase también

- [`docs/operator-guide.md`](operator-guide.md) — flujos de trabajo del día a día una vez el despliegue está activo.
- [`docs/architecture.md`](architecture.md) — diagramas C4, secuencias en tiempo de ejecución, invariantes de diseño.
- [`docs/control-plane-state.md`](control-plane-state.md) — superficie de capacidades, defensas de robustez, brechas abiertas.
- [`docs/fault-model.md`](fault-model.md) — fallos tolerados y límites superiores de recuperación.
- [`docs/threat-model.md`](threat-model.md) — fronteras de confianza, catálogo STRIDE.
- [`AGENTS.md`](../AGENTS.md) — invariantes y patrones de implementación.
