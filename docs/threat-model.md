# Kubeport — Modelo de amenazas

Este documento es el análisis de fronteras de confianza para el plano de control de Kubeport. Complementa [`SECURITY.md`](../SECURITY.md) (que establece la política de divulgación y los supuestos de confianza de alto nivel) con un catálogo STRIDE por frontera, una justificación de la lista de operaciones destructivas permitidas, y un mapeo de cada endpoint incluido en la lista blanca y cada llamada de worker privilegiada a la frontera que cruza.

Para el contexto arquitectónico — contenedores, componentes, secuencias de ejecución — véase [`docs/architecture.md`](architecture.md). Para el catálogo de fallos tolerados y los límites superiores de recuperación, véase [`docs/fault-model.md`](fault-model.md). Las ocho propiedades numeradas de seguridad / disponibilidad / consistencia eventual aplicadas por el código se enumeran en [`docs/architecture.md`](architecture.md) §3.

---

## 1. Diagrama de fronteras de confianza

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

**Índice de fronteras**

| ID | Frontera | Dirección del cruce | Portador |
|---|---|---|---|
| B1 | Navegador del operador → Worker web de Frappe | Entrante | HTTPS autenticado / cookie de sesión de Frappe + CSRF |
| B1g | Visitante no autenticado → Worker web de Frappe | Entrante | HTTPS público, único endpoint para invitados |
| B2 | Procesos de Frappe → MariaDB | Bidireccional, en proceso | ORM de Frappe + API de campos cifrados |
| B3 | Worker web / planificador → Worker largo de RQ | Unidireccional, diferido | RQ respaldado por Redis mediante `frappe.enqueue(..., queue="long", enqueue_after_commit=True)` |
| B4 | Worker largo → CLI de Helm | Unidireccional, invocación en proceso | `subprocess.run(list, ...)` con archivo kubeconfig aislado por llamada |
| B5 | Worker largo / Helm → API de Kubernetes | Saliente, con alcance por clúster | Cliente Python `kubernetes` mediante `get_k8s_api_client(cluster_name)` |
| B6 | API del clúster → pod bench (exec) | Saliente, flujo bidireccional | `stream(core_v1.connect_get_namespaced_pod_exec, ...)` |
| B7 | Worker largo → GHCR público | Saliente, diario | Descarga HTTPS del catálogo curado; digest fijado en la fila |
| B8 | Pod Job del clúster → PVC `kubeport-backups` | Interno al clúster | PVC RWX montado por el Job de backup de bench y un Job de sonda busybox |

---

## 2. Análisis por frontera

### B1 — Navegador del operador → Worker web de Frappe

**Dirección de confianza.** Entrante: el operador solo es de confianza después de que Frappe haya autenticado la sesión y validado el token CSRF. El endpoint se ejecuta con los vínculos de rol de ese operador.

**Datos que cruzan.** Valores de formulario para escrituras de estado deseado (filas de `Kubernetes Cluster` incluyendo kubeconfig, tokens bearer; YAML de valores de `Helm Release`; YAML de manifiesto de `Service Bundle`); solicitudes de lectura para descubrimiento en vivo del clúster; acciones ad-hoc del operador (`Kubernetes Command.execute`).

**Mitigaciones en el código.**

- Sesión de Frappe + token CSRF en cada endpoint `@frappe.whitelist()` (valor predeterminado del framework; `allow_guest=True` es la opción explícita de exclusión).
- Control de roles: cada DocType privilegiado requiere `System Manager`. El bloque de permisos de `Kubernetes Command` está en `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.json:161-170`.
- Argumentos con anotaciones de tipo: `require_type_annotated_api_methods = True` en `kubeport/hooks.py` rechaza las llamadas con tipos poco estrictos antes de la ejecución.
- Validación del lado del servidor en cada escritura de estado deseado, por ejemplo, normalización del endpoint de kubeconfig en `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.py`, lista de tipos permitidos de manifiestos en `kubeport/utils/k8s_resources.py`, validación de coordenadas de GHCR en `kubeport/kubeport/doctype/kubeport_site_image/kubeport_site_image.py`.
- Confirmaciones tipadas para operaciones destructivas: `Kubernetes Command.confirm_destructive` comprobado en `validate` y en `execute` (`kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.py:78-91, 113-114`); `Frappe Site.cancel_site` y `Frappe Site.restore_site` también.

### B1g — Visitante no autenticado → Worker web de Frappe

**Dirección de confianza.** Entrante, pero el visitante no es de confianza. Exactamente un endpoint tiene `allow_guest=True`: `kubeport.api.dashboard.public_helm_release_count` (`kubeport/api/dashboard.py:76-86`).

**Datos que cruzan.** Saliente: un único entero (`frappe.db.count("Helm Release")`). No se acepta ningún cuerpo de solicitud más allá del enrutamiento estándar de Frappe.

**Mitigaciones en el código.** El endpoint no acepta parámetros, no devuelve datos de filas y solo lee la cardinalidad de un DocType. No hay ruta de escritura expuesta.

### B2 — Procesos de Frappe → MariaDB

**Dirección de confianza.** Interna, en proceso. Cualquiera con acceso de lectura a nivel de base de datos al esquema de Frappe puede leer lo que un System Manager puede leer.

**Datos que cruzan.** Filas de estado deseado; campos de secretos cifrados (el fieldtype `Password` de Frappe está cifrado en reposo con la clave de cifrado a nivel de bench).

**Mitigaciones en el código.**

- El token bearer utiliza el fieldtype `Password`: `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.json:133` (cifrado en reposo).
- Los Secrets de credenciales de base de datos por Job se crean en el clúster, se referencian como propietarios del Job para su recolección de basura, y nunca se persisten en la especificación env estática del pod bench: `kubeport/tasks/site_tasks.py:149` (creación), `:242` (eliminación).
- Todas las consultas utilizan llamadas parametrizadas del ORM de Frappe (`frappe.db.get_value`, `frappe.db.set_value`); no se usa SQL directo en la ruta de mutación del clúster.

**Riesgo residual documentado.** El YAML del kubeconfig se almacena en un campo `Code`, no en `Password`, por lo que está en reposo en texto plano en la fila. Esto se menciona en `SECURITY.md` ("Anyone who can read those rows from the database can act as the cluster"). Se espera que los operadores controlen el acceso a nivel de base de datos en consecuencia.

### B3 — Worker web / planificador → Worker largo de RQ

**Dirección de confianza.** Unidireccional. El flujo de control abandona el hilo de la solicitud y se reanuda dentro del worker largo. Todo lo que cruza debe ser revalidado.

**Datos que cruzan.** Solo argumentos del Job (normalmente un docname más un `operation_token` de 128 bits).

**Mitigaciones en el código.**

- `enqueue_after_commit=True` garantiza que el worker solo vea filas que hayan sido confirmadas: por ejemplo, `kubeport/kubeport/doctype/helm_release/helm_release.py:148-155`.
- El worker vuelve a obtener el documento y vuelve a comprobar el `operation_token` antes de cualquier escritura de estado — véase el fallo `F5` en `docs/fault-model.md`.
- La cola (`long`) aísla la latencia de mutación del clúster de los Jobs de usuario de corta duración.

### B4 — Worker largo → CLI de Helm

**Dirección de confianza.** Invocación en proceso hacia un binario externo al proceso en el host bench.

**Datos que cruzan.** Argumentos de comando, un archivo kubeconfig aislado por llamada escrito en un directorio temporal, variables de entorno (`HELM_CACHE_HOME`, `HELM_CONFIG_HOME`, `HELM_DATA_HOME`, `KUBECONFIG`), YAML de valores en stdin cuando aplica.

**Mitigaciones en el código.**

- La invocación de subprocesos es siempre una lista, nunca `shell=True`: `kubeport/utils/helm.py:516, 525`. El docstring del módulo en `kubeport/utils/helm.py:9` reitera la regla.
- Tiempos de espera por llamada: `_HELM_WORKER_TIMEOUT_SECONDS = 600` (mutaciones), `_HELM_READ_TIMEOUT_SECONDS = 30` (lecturas). Definidos en `kubeport/utils/helm.py:31`.
- El kubeconfig por llamada se escribe en una ruta temporal, solo el proceso del worker largo puede leerlo, y el directorio se elimina tras la llamada.
- El binario de Helm está en el `PATH` del host bench y es de confianza; la integridad de la cadena de suministro de ese binario es responsabilidad del operador (mencionado en `SECURITY.md` "Hardening recommendations").

### B5 — Worker largo / Helm → API de Kubernetes

**Dirección de confianza.** Saliente. El clúster confía en cualquier credencial que presente el kubeconfig / token bearer. Kubeport debe mantener esa credencial con un alcance limitado.

**Datos que cruzan.** Material de autenticación (kubeconfig, token bearer, token de SA en clúster); solicitudes CRUD contra los tipos de recursos de la lista de tipos permitidos de Service Bundle, más los recursos del ciclo de vida del Job de Frappe Site; lecturas de lista de pods / logs de pods / eventos para observabilidad.

**Mitigaciones en el código.**

- Cliente con alcance por clúster: `get_k8s_api_client(cluster_name)` en `kubeport/utils/k8s_client.py`. No se comparte estado global.
- La autenticación con token bearer sin certificado CA se rechaza de forma predeterminada; la omisión TLS solo para desarrollo tiene el nombre `Skip TLS Verification (Development Only)` y está protegida por una casilla de verificación separada: véase `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.json` y el bloque de validación del controlador.
- Lista de tipos de recursos permitidos para aplicar Service Bundle: `kubeport/utils/k8s_resources.py` (17 tipos integrados; los CRDs quedan explícitamente fuera del alcance).
- `_request_timeout` por llamada en cada llamada de cliente en tiempo de reconciliación y de observabilidad (véase el fallo `F7` en `docs/fault-model.md`).
- La aplicación de Service Bundle usa server-side apply (no suplanta a un usuario; usa la credencial del clúster registrado).

### B6 — API del clúster → pod bench (exec)

**Dirección de confianza.** La API del clúster reenvía una solicitud exec de Kubeport al contenedor principal del pod bench. El pod bench se trata como la fuente de verdad para la existencia del sitio (véase `docs/control-plane-state.md`).

**Datos que cruzan.** Un comando de shell fijo (`bench list-apps`, `find` sobre el directorio de sitios) — nunca suministrado por el operador; el flujo de respuesta de stdout/stderr.

**Mitigaciones en el código.**

- Los comandos se construyen a partir de literales de cadena en el código (`kubeport/utils/discovery.py:309-322`, `kubeport/tasks/reconciliation.py:1641` y siguientes), nunca a partir de la entrada del operador. No hay vector de inyección de shell que llegue al canal exec.
- `_request_timeout` limita el flujo (`_POD_EXEC_TIMEOUT_SECONDS`).
- La sonda de tres estados protege las escrituras de estado deseado: un fallo transitorio de exec devuelve `unknown` y la fila se deja en vuelo en lugar de marcarse incorrectamente como `Failed` (fallo `F3`).

**Riesgo residual documentado.** La ejecución de código dentro del pod bench compromete la fuente de verdad. `docs/fault-model.md` §"Faults explicitly out of scope" lo menciona explícitamente.

### B7 — Worker largo → GHCR público

**Dirección de confianza.** Solo saliente. Kubeport nunca publica en GHCR; el flujo de trabajo de publicación de imágenes de sitio se ejecuta en un push de etiqueta y está fuera de proceso del bench.

**Datos que cruzan.** Descargas HTTPS de los deltas de `kubeport/site_images/catalog.json` curado durante la sincronización diaria del catálogo de imágenes de sitio.

**Mitigaciones en el código.**

- Las filas curadas deben ser repositorios `ghcr.io/owner/image` con un digest `sha256:` fijado: validación en `kubeport/kubeport/doctype/kubeport_site_image/kubeport_site_image.py`.
- El saneamiento del catálogo se ejecuta en una tarea en segundo plano (`kubeport/tasks/site_image_tasks.py`) y es idempotente: la reimportación nunca amplía el conjunto curado sin el correspondiente commit de manifiesto.
- El flujo de trabajo de publicación de imágenes de sitio no hace auto-commit — sube un artefacto y el operador confirma el bump a través del flujo de revisión normal (véase [`AGENTS.md`](../AGENTS.md) §"Scheduled Jobs").

### B8 — Pod Job del clúster → PVC `kubeport-backups`

**Dirección de confianza.** Interna al clúster. Los Jobs de backup del lado bench escriben archivos; un Job de sonda busybox de corta duración lee el auxiliar `<archivo>.size`.

**Datos que cruzan.** Archivos tar de backup y sus archivos auxiliares `.size`; un Job de limpieza `archive-delete` por archivo.

**Mitigaciones en el código.**

- El PVC es local al namespace; el acceso requiere estar programado en ese namespace.
- El Job de sonda monta el PVC de solo lectura y solo ejecuta la lectura del auxiliar de tamaño (`kubeport/tasks/reconciliation.py:1245`).
- El ciclo de vida del archivo es independiente de la fila fuente `Frappe Site` (Invariante 7 en `AGENTS.md`); la eliminación del sitio fuente no huerfana el archivo.

---

## 3. Catálogo STRIDE

Una fila por par (frontera, amenaza) con una mitigación no trivial. Las amenazas que se corresponden con "fuera de alcance" (por ejemplo, denegación de servicio mediante acciones legítimas de System Manager, según `SECURITY.md`) no se duplican aquí.

| Frontera | STRIDE | Amenaza | Mitigación | Testigo |
|---|---|---|---|---|
| B1 | Suplantación | Sesión falsificada que se hace pasar por un operador. | Sesión de Frappe + CSRF (valor predeterminado del framework). | `kubeport/hooks.py` (no hay exclusiones de CSRF registradas para las rutas de Kubeport). |
| B1 | Manipulación | El operador envía un manifiesto con tipos no permitidos (p. ej., CRD) para eludir la lista de tipos permitidos. | Validación de `Service Bundle` del lado del servidor contra la lista de tipos de recursos permitidos; rechazado antes del enqueue. | `kubeport/utils/k8s_resources.py`, `kubeport/kubeport/doctype/service_bundle/service_bundle.py:44-60`. |
| B1 | Repudio | El operador elimina un `Pod`/`Job`/`ConfigMap` y niega la acción. | Fila de `Kubernetes Command Audit Log` de solo anexado escrita por cada ejecución, desacoplada de la fila fuente para sobrevivir a su eliminación. | `kubeport/tasks/kubernetes_command_tasks.py:110-143`. |
| B1 | Divulgación de información | El navegador exfiltra credenciales de `Kubernetes Cluster`. | El token bearer usa el fieldtype `Password` cifrado; el campo no se devuelve a los formularios en texto plano tras el primer guardado (comportamiento predeterminado de Frappe para `Password`). | `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.json:133`. |
| B1 | Denegación de servicio | El operador hace clic en Deploy / Cancel rápidamente para corromper el estado en vuelo. | `operation_token` por ejecución rotado en cada enqueue; el worker lo vuelve a comprobar antes de las escrituras (fallo `F5`). | `kubeport/kubeport/doctype/helm_release/helm_release.py:141`. |
| B1 | Elevación de privilegios | Un usuario que no es System Manager desencadena una operación ad-hoc destructiva. | Comprobación de rol a nivel de DocType en `Kubernetes Command`, más campo tipado `confirm_destructive` bloqueado en `validate` y `execute`. | `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.json:161-170`, `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.py:78-91`. |
| B1g | Divulgación de información | El endpoint de invitado se amplía para exponer datos de filas. | El endpoint solo devuelve `frappe.db.count("Helm Release")`; no acepta parámetros. | `kubeport/api/dashboard.py:76-86`. |
| B2 | Divulgación de información | Se exfiltra una copia de seguridad de la base de datos; se filtran tokens bearer. | Los tokens bearer están cifrados en reposo mediante el campo `Password` de Frappe (la clave de cifrado es a nivel de bench). Se espera que los operadores cifren las copias de seguridad de la base de datos (`SECURITY.md` "Hardening recommendations"). | `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.json:133`. |
| B2 | Manipulación | Valores falsificados en una fila de estado deseado engañan a la reconciliación. | Todas las escrituras desde la ruta del worker pasan por `db_set` con recomprobación del token; las escrituras desde la ruta del controlador pasan por el pipeline de validación de Frappe. | `kubeport/tasks/site_tasks.py:1357`, `kubeport/tasks/reconciliation.py:1820`. |
| B3 | Suplantación | Job recogido por un worker antes de que se confirme la fila fuente. | `enqueue_after_commit=True` en cada enqueue que muta el clúster. | `kubeport/kubeport/doctype/helm_release/helm_release.py:148-155`, reflejado en `service_bundle.py:78`, `frappe_site.py:149` (y sitios de enqueue hermanos). |
| B3 | Manipulación | El worker actúa sobre un estado rotado. | Recomprobación de `operation_token` previa a la escritura (fallo `F5`). | `kubeport/tasks/site_tasks.py:1357`. |
| B4 | Manipulación / EoP | Cadena controlada por el operador inyectada en un comando de shell. | Solo `subprocess.run(list, ...)`; el docstring del módulo prohíbe `shell=True`. | `kubeport/utils/helm.py:516, 525` y regla del módulo en `:9`. |
| B4 | Denegación de servicio | La CLI de Helm se cuelga contra un clúster inalcanzable. | Tiempos de espera estrictos (`_HELM_WORKER_TIMEOUT_SECONDS = 600`, `_HELM_READ_TIMEOUT_SECONDS = 30`). Una fila atascada se recupera mediante el fallo `F1`. | `kubeport/utils/helm.py:31`. |
| B4 | Divulgación de información | El kubeconfig por llamada se filtra a otros procesos del host bench. | Escrito en un archivo temporal por llamada, eliminado al salir el subproceso. | `kubeport/utils/helm.py` (wrapper de helm, bloque `_with_kubeconfig`). |
| B5 | Suplantación | Falsificación del servidor de la API del clúster. | `ca_certificate` obligatorio a menos que se habilite explícitamente la omisión TLS solo para desarrollo. | `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.py` (bloque de validación). |
| B5 | Manipulación | El alcance de la credencial del clúster se extiende entre clústeres registrados. | `get_k8s_api_client(cluster_name)` devuelve un cliente con alcance recién construido por llamada; no hay caché global compartido entre clústeres. | `kubeport/utils/k8s_client.py`. |
| B5 | Denegación de servicio | El servidor de la API paraliza las lecturas. | `_request_timeout` en cada lectura (15 s para la lista de Jobs del barrido de huérfanos, 10 s para el descubrimiento de clases de almacenamiento, configurable para el listado de pods por formulario). | `kubeport/tasks/reconciliation.py:1577`, `kubeport/tasks/site_tasks.py:382`, `kubeport/utils/observability.py:63`. |
| B5 | Elevación de privilegios | La aplicación de Service Bundle muta un tipo fuera de la lista de tipos permitidos. | La lista de tipos permitidos se aplica antes de la operación apply; los CRDs y los CRs arbitrarios se rechazan. | `kubeport/utils/k8s_resources.py`. |
| B6 | Manipulación | Una cadena suministrada por el operador llega al comando exec. | El comando está codificado en el código fuente; no hay entrada del operador que fluya al shell. | `kubeport/utils/discovery.py:309-322`, `kubeport/tasks/reconciliation.py:1641` y siguientes. |
| B6 | Repudio | El resultado del pod-exec se trata silenciosamente como una escritura. | Sonda de tres estados — un fallo transitorio de exec devuelve `unknown` y el siguiente tick reintenta; las escrituras de estado final solo ocurren ante una respuesta definitiva. | `kubeport/tasks/reconciliation.py:31, 1192`. |
| B6 | Denegación de servicio | Un flujo exec colgado bloquea el worker. | `_request_timeout` (`_POD_EXEC_TIMEOUT_SECONDS`). | `kubeport/utils/discovery.py:333`, `kubeport/tasks/reconciliation.py:1666` (exec_kwargs). |
| B7 | Manipulación | El catálogo curado es reemplazado por una referencia de imagen falsificada. | Las filas curadas tienen el digest fijado (`sha256:`); el flujo de trabajo `update_site_catalog` no hace auto-commit, por lo que un bump curado pasa por la revisión de código normal. | `kubeport/kubeport/doctype/kubeport_site_image/kubeport_site_image.py` (validación de digest), `.github/workflows/publish-site-image.yml` (flujo solo de artefacto). |
| B8 | Manipulación | Un archivo truncado se marca erróneamente como `Available`. | La reconciliación no confía en los códigos de salida del Job; la sonda auxiliar `<archivo>.size` del lado del PVC también debe completarse con éxito (fallo `F9`). | `kubeport/tasks/reconciliation.py:1245`. |

---

## 4. Mapeo de endpoints × frontera

Cada punto de entrada incluido en la lista blanca y cada llamada de worker privilegiada se mapea a la frontera que cruza, de modo que la tabla de fronteras anterior sea exhaustiva sobre el código base de Kubeport.

### 4.1 Endpoints en lista blanca (B1, más B1g donde se indica)

| Endpoint | Archivo:línea | Cruza |
|---|---|---|
| `get_cluster_namespaces` | `kubeport/api/__init__.py:27` | B1 → B5 (lectura en vivo) |
| `parse_kubeconfig_contexts` | `kubeport/api/__init__.py:65` | B1 (solo análisis, sin contacto con el clúster) |
| `extract_kubeconfig_context` | `kubeport/api/__init__.py:132` | B1 (solo análisis) |
| `get_cluster_discovery` | `kubeport/api/discovery.py:19` | B1 → B5 |
| `adopt_helm_release` | `kubeport/api/discovery.py:94` | B1 → B2 (escribe una fila de estado deseado a partir de una release descubierta en vivo) |
| `get_release_resource_logs` | `kubeport/api/observability.py:28` | B1 → B5 |
| `get_release_resource_events` | `kubeport/api/observability.py:96` | B1 → B5 |
| `get_release_resource_rollout` | `kubeport/api/observability.py:129` | B1 → B5 |
| `get_site_job_logs` | `kubeport/api/site.py:14` | B1 → B5 |
| `list_site_backups` | `kubeport/api/site.py:101` | B1 → B2 |
| `get_site_backup_job_logs` | `kubeport/api/site.py:122` | B1 → B5 |
| `list_site_images` | `kubeport/api/site_images.py:13` | B1 → B2 |
| `count_stale_operations` | `kubeport/api/dashboard.py:32` | B1 → B2 |
| `stale_operations_card_value` | `kubeport/api/dashboard.py:64` | B1 → B2 |
| `public_helm_release_count` | `kubeport/api/dashboard.py:76` | **B1g** → B2 (solo conteo) |
| `Kubernetes Cluster.test_connection` | `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.py:66` | B1 → B5 |
| `Helm Repository.sync_charts` | `kubeport/kubeport/doctype/helm_repository/helm_repository.py:59` | B1 → B3 → B4 |
| `Helm Chart.fetch_default_values` | `kubeport/kubeport/doctype/helm_chart/helm_chart.py:42` | B1 → B4 (solo lectura `helm show values`) |
| `Helm Release.deploy_release` | `kubeport/kubeport/doctype/helm_release/helm_release.py:125` | B1 → B3 → B4/B5 |
| `Helm Release.uninstall_release` | `kubeport/kubeport/doctype/helm_release/helm_release.py:162` | B1 → B3 → B4/B5 |
| `Helm Release.rollback_release` | `kubeport/kubeport/doctype/helm_release/helm_release.py:203` | B1 → B3 → B4/B5 |
| `Helm Release.get_release_health` | `kubeport/kubeport/doctype/helm_release/helm_release.py:241` | B1 → B5 |
| `Helm Release.load_defaults` | `kubeport/kubeport/doctype/helm_release/helm_release.py:267` | B1 → B4 (solo lectura) |
| `Helm Release.get_release_history` | `kubeport/kubeport/doctype/helm_release/helm_release.py:304` | B1 → B4 (solo lectura) |
| `Service Bundle.apply_bundle` | `kubeport/kubeport/doctype/service_bundle/service_bundle.py:44` | B1 → B3 → B5 |
| `Service Bundle.delete_bundle` | `kubeport/kubeport/doctype/service_bundle/service_bundle.py:62` | B1 → B3 → B5 |
| `Frappe Site.create_site` | `kubeport/kubeport/doctype/frappe_site/frappe_site.py:124` | B1 → B3 → B5 |
| `Frappe Site.delete_site` | `kubeport/kubeport/doctype/frappe_site/frappe_site.py:163` | B1 → B3 → B5 |
| `Frappe Site.migrate_site` | `kubeport/kubeport/doctype/frappe_site/frappe_site.py:210` | B1 → B3 → B5 |
| `Frappe Site.backup_site` | `kubeport/kubeport/doctype/frappe_site/frappe_site.py:244` | B1 → B3 → B5 → B8 |
| `Frappe Site.restore_site` | `kubeport/kubeport/doctype/frappe_site/frappe_site.py:292` | B1 → B3 → B5 ← B8 |
| `Frappe Site.cancel_site` | `kubeport/kubeport/doctype/frappe_site/frappe_site.py:368` | B1 → B3 → B5 |
| `Kubernetes Command.execute` | `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.py:98` | B1 → B5 (lectura en línea) o B1 → B3 → B5 (eliminación por enqueue) |

### 4.2 Llamadas de worker privilegiadas (B3 → B4/B5/B6)

| Función del worker | Archivo:línea | Cruza |
|---|---|---|
| `add_and_sync_repo` | `kubeport/tasks/helm_tasks.py:42` | B4 |
| `sync_repo_charts` | `kubeport/tasks/helm_tasks.py:89` | B4 |
| `sync_all_repos` (programado, diario) | `kubeport/tasks/helm_tasks.py:140` | B4 |
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
| `run_kubernetes_command` | `kubeport/tasks/kubernetes_command_tasks.py:44` | B5 (solo Delete; las lecturas se ejecutan en línea en B1) |
| `sync_site_image_catalog` (programado, diario) | `kubeport/tasks/site_image_tasks.py:20` | B7 → B2 |
| `reconcile_all_releases` (programado, `*/5`) | `kubeport/tasks/reconciliation.py` (registrado en `kubeport/hooks.py`) | B5, B6, B2 |
| `reconcile_site_backups` (programado, `*/5`) | `kubeport/tasks/reconciliation.py` (registrado en `kubeport/hooks.py`) | B5, B6, B8, B2 |

---

## 5. Justificación de la lista de tipos permitidos para Delete en Kubernetes Command

`Kubernetes Command` es el único DocType que expone operaciones ad-hoc de clúster al operador. Las acciones de lectura (`Get`, `List`) aceptan la lista completa de ocho tipos para lectura:

```
Pod, Job, ConfigMap, Secret, PersistentVolumeClaim,
Service, Deployment, StatefulSet
```

Las acciones de eliminación solo aceptan tres tipos — `Pod`, `Job`, `ConfigMap` — definidos en `_DELETABLE_KINDS` en `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.py:36`. Las exclusiones son deliberadas. Para cada tipo excluido, esta sección indica el motivo fundamental y el controlador que debería ser propietario de la ruta destructiva en su lugar.

| Tipo | Excluido de Delete porque… | El operador debería usar |
|---|---|---|
| `Secret` | Los pods de una carga de trabajo activa pueden tener el Secret proyectado como env/volumen; eliminarlo no reinicia los Pods que lo consumen, dejándolos con credenciales obsoletas y un modo de fallo confuso en el próximo reinicio. El ciclo de vida del Secret debería ser propiedad del controlador que lo creó (Helm, el Job de Frappe Site), no de una eliminación fuera de banda por el operador. | Actualización / desinstalación de `Helm Release`; flujo de cancelación de `Frappe Site` (que elimina el Secret de credenciales por Job mediante referencia de propietario). |
| `PersistentVolumeClaim` | Eliminar un PVC destruye datos sin posibilidad de deshacer en banda. El PVC bench de Frappe Site y el PVC `kubeport-backups` contienen estado irremplazable; el PVC de backup también está diseñado para sobrevivir a la fila fuente `Frappe Site` (Invariante 7). Permitir la eliminación de PVC mediante una fila del registro de auditoría anularía la garantía de durabilidad de que los metadatos de backup sobreviven a la eliminación de la fila fuente. | Flujo de eliminación de `Frappe Site Backup` (que usa un Job `archive-delete` para eliminar archivos específicos sin perturbar el PVC); herramientas de administrador del clúster para la eliminación del PVC. |
| `Deployment` | Eliminar un Deployment termina masivamente su conjunto de réplicas y deja los Pods de la carga de trabajo huérfanos, causando una interrupción que ninguna fila de Kubeport refleja. La fila de estado deseado seguiría afirmando `Deployed`, rompiendo el invariante de que la fila predice la forma del clúster. | Actualización / desinstalación de `Helm Release` — la única ruta que actualiza la fila de estado deseado en la misma transacción. |
| `StatefulSet` | El mismo perfil de interrupción que `Deployment`, con el coste adicional de que los PVCs gestionados por StatefulSet pueden retenerse o eliminarse dependiendo de `persistentVolumeClaimRetentionPolicy` — un riesgo para eliminaciones ad-hoc. El ciclo de vida gestionado por Helm maneja esto de forma consistente. | Actualización / desinstalación de `Helm Release`. |
| `Service` | Eliminar un Service rompe silenciosamente todos sus consumidores (tráfico intra-clúster, backends de Ingress, enrutamiento del sitio de Frappe) sin que aparezca en ninguna fila de Kubeport. | `Helm Release` o `Service Bundle` (que vuelve a aplicar mediante server-side apply y actualiza la fila). |

Los tipos que **sí** están permitidos para Delete comparten una propiedad: el recurso es reiniciable o recuperable por sí mismo.

- `Pod` — el controlador padre (Deployment, StatefulSet, Job) lo vuelve a crear.
- `Job` — ya está en estado terminal en el momento de la eliminación en funcionamiento normal; la recreación es la ruta de recuperación normal del operador para la reconciliación atascada, y el barrido de Jobs huérfanos (fallo `F6`) ya elimina los Jobs no referenciados por ninguna fila.
- `ConfigMap` — recuperable desde el origen (la fila de estado deseado que lo definió puede volver a aplicarlo). Sin pérdida de datos.

La doble comprobación (campo tipado `confirm_destructive` aplicado tanto en `validate` como en `execute`, fila de auditoría escrita en cada ejecución) garantiza que cada Delete quede registrado por la fila fuente y por la fila de auditoría, y que la fila de auditoría sobreviva a la fuente.

---

## 6. Referencias cruzadas

- [`SECURITY.md`](../SECURITY.md) — política de divulgación, dentro y fuera del alcance, recomendaciones de hardening.
- [`docs/architecture.md`](architecture.md) §3 — ocho propiedades numeradas de seguridad / disponibilidad / consistencia eventual aplicadas por el código.
- [`docs/fault-model.md`](fault-model.md) — fallos tolerados, defensas, límites superiores de recuperación.
- [`docs/control-plane-state.md`](control-plane-state.md) §Robustness Properties — inventario en prosa de las mismas defensas en términos de características del producto.
- [`AGENTS.md`](../AGENTS.md) — invariantes no negociables para colaboradores que mantienen estas propiedades verdaderas.