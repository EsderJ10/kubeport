# Plan B — Drilldown de Observabilidad de Helm Release

## Proposito

El formulario de Helm Release ya muestra readiness por recurso (manifest renderizado evaluado
contra el cluster vivo, ocho tipos de workload integrados). Cuando un release queda `Degraded`,
el operador puede ver **que** recurso falla, pero no **por que**. El siguiente clic, "show me the
logs", saca del producto: `kubectl logs`, `kubectl describe`, `kubectl rollout history`.

Plan B cierra ese hueco del "por que?". Tras este ciclo, cada fila no lista en el drilldown de
readiness estara a un clic de los datos que explican el problema: logs del pod (con tail), eventos
de Kubernetes para el recurso y la cronologia del rollout para `Deployment` / `StatefulSet` /
`DaemonSet`.

El objetivo del ciclo es que **los operadores se queden en Kubeport para diagnosticar**. No hay
nuevos DocTypes, ni nuevas rutas que muten el cluster. Solo observabilidad de solo lectura que
extiende el walker de readiness ya existente.

Fuera de alcance para este ciclo (pospuesto): metricas de pod (CPU/memory), streaming de logs en
tiempo real via WebSocket, timeline completo de eventos del cluster (solo se consultan eventos
acotados a un recurso concreto), busqueda de logs / grep a traves de pods y exec-into-pod desde
la UI.

## Encaje arquitectonico

Segun `AGENTS.md` y `CLAUDE.md`:

- Discovery es solo lectura y nunca se persiste en MariaDB. Los datos de logs / eventos / rollout
  son estado observado, obtenido bajo demanda.
- Los endpoints de API viven en `kubeport/api/` y se exponen con whitelist y anotaciones de tipo.
- El render del formulario usa `frappe.xcall` para datos externos (no `doc.onload` para lecturas
  contra el cluster vivo).
- Los timeouts de subprocess son conservadores (30s por defecto, alineado con
  `kubeport/utils/helm.py`).

Este ciclo **no** anade jobs en background: cada lectura es una llamada sincrona a la API de K8s
desde el hilo web. Eso es aceptable porque los payloads estan acotados (longitud del tail, numero
de eventos, numero de revisiones) y el operador esta esperando de forma interactiva en un panel
que acaba de abrir.

## Alcance

### Nuevo modulo utilitario

**`kubeport/utils/observability.py`**: helpers sin estado, solo API de K8s:

- `get_pod_logs(cluster, namespace, pod, container=None, tail_lines=200, previous=False) -> str`
  - Llama a `CoreV1Api.read_namespaced_pod_log` con `_request_timeout=20.0`.
  - Limita `tail_lines` a 2000 del lado del servidor. Trunca el payload a 256 KB antes de
    devolverlo para mantener el formulario responsivo.
  - `previous=True` obtiene los logs del contenedor anterior (tras un restart). Se expone como
    un toggle "Previous container" en la UI para contenedores en crash-loop.
- `list_resource_events(cluster, namespace, kind, name, limit=20) -> list[dict]`
  - Llama a `CoreV1Api.list_namespaced_event` con `field_selector="involvedObject.name=<name>,
    involvedObject.kind=<kind>"`.
  - Devuelve los eventos mas recientes hasta `limit`, normalizados a `{type, reason, message,
    count, first_seen, last_seen, source}`.
  - Defensivo: K8s 1.25+ usa `events.k8s.io/v1`; hace fallback a `core/v1` si la API v1 de
    eventos devuelve vacio (algunos clusters gestionados van atrasados).
- `get_rollout_history(cluster, namespace, kind, name, limit=10) -> list[dict]`
  - Para `Deployment`: lista `ControllerRevision`s + los `ReplicaSet`s correspondientes, y
    devuelve una fila por revision con `revision`, `created_at`, `change_cause` (desde la
    anotacion `kubernetes.io/change-cause`), `current` (bool), `image_summary` (strings de
    imagenes de contenedor unidos).
  - Para `StatefulSet` / `DaemonSet`: la misma vista basada en `ControllerRevision`.
  - Para otros tipos: devuelve lista vacia (no soportado de forma elegante; la UI oculta el panel).
- `list_pods_for_resource(cluster, namespace, kind, name) -> list[dict]`
  - Resuelve los pods pertenecientes a un `Deployment` / `StatefulSet` / `DaemonSet` / `Pod`
    standalone recorriendo la cadena de owner-reference (Deployment → ReplicaSet → Pod;
    StatefulSet/DaemonSet → Pod directamente). La usa el drilldown para poblar el selector de pod
    cuando el operador hace clic en "Logs" sobre una fila de workload.
  - Devuelve `[{name, container_names, phase, restart_count, container_statuses}]`.

Todas las funciones lanzan `RuntimeError` ante fallos de conectividad con el cluster, con una
cadena que el formulario pueda renderizar tal cual. Ninguna muta estado.

### Nuevos endpoints de API (`kubeport/api/observability.py`)

Todos llevan `@frappe.whitelist()` y `frappe.only_for("System Manager")` (o el rol que gatee el
controlador de releases existente: hay que igualarlo). Todos reciben `release_docname` y resuelven
cluster / namespace desde la fila, sin confiar nunca en que el cliente nombre el cluster.

- `get_release_resource_logs(release_docname, kind, name, container=None, tail_lines=200,
  previous=False) -> dict` — envuelve `get_pod_logs`. Para tipos de workload (`Deployment`,
  `StatefulSet`, `DaemonSet`), el endpoint resuelve antes los pods del workload y devuelve un
  dict `{pods: [...], selected_pod, logs_by_pod: {<pod_name>: <log_text>}, errors_by_pod: {...}}`
  con logs de un pod seleccionado y probado por ownership por peticion, para que workloads grandes
  no requieran una request web por cada pod.
- `get_release_resource_events(release_docname, kind, name, limit=20) -> dict` — envuelve
  `list_resource_events` y devuelve `{rows: [...], error: ""}`. Los fallos de lookup devuelven
  `{rows: [], error: "<message>"}` para degradacion elegante a nivel de panel.
- `get_release_resource_rollout(release_docname, kind, name, limit=10) -> dict` — envuelve
  `get_rollout_history` y devuelve `{rows: [...], error: ""}` con el mismo wrapper de fallo.

### UI (`helm_release.js`)

Cada fila no lista de la tabla existente de drilldown de health gana tres acciones, renderizadas
como pequenos botones inline:

- **Logs** — abre un panel lateral con un selector de pod (cuando la fila es un workload), un
  selector de tail-lines (50 / 200 / 500 / 2000), un toggle "Previous container" y un `<pre>`
  para el texto del log. El boton de refresh vuelve a consultar via xcall.
- **Events** — abre un panel lateral con una lista ordenable de eventos acotados a este recurso.
  Mas recientes primero. Cada fila muestra type/reason/age y un tooltip con el mensaje completo.
- **Rollout** — solo se muestra para `Deployment` / `StatefulSet` / `DaemonSet`. Abre un panel
  lateral con la lista de revisiones, la revision actual marcada y las imagenes por revision.
  **No hay accion de rollback en este ciclo.** Helm rollback ya existe a nivel de release; el
  rollback de workload a nivel de superficie se pospone a proposito.

Los paneles laterales son hermanos del panel de readiness existente, comparten el mismo canal en
tiempo real para auto-refresh con `helm_release_status_update` y se cierran limpiamente al
recargar el formulario.

### Fila de readiness existente mejorada

`utils/release_health.py` — solo una mejora menor. El campo `ResourceHealth.message` ya incluye
el primer evento fallido para filas no listas (el `_attach_warning_events` existente). Anade un
unico entero `pod_count` al resultado por workload para que el formulario sepa si debe mostrar el
selector de pod sin hacer un round-trip.

## Cuando la implementacion este completa

El ciclo termina cuando se cumplen **todas** estas condiciones:

1. **El camino del "por que?" es un clic.** Desde un Helm Release `Degraded` con un
   `Deployment` fallando, el operador puede: hacer clic en la fila de readiness → ver los
   botones "Logs", "Events", "Rollout" → hacer clic en cualquiera → obtener los datos en
   menos de 5 segundos y sin errores en consola. Verificado manualmente en un cluster real
   contra al menos un deployment en crash-loop.
2. **Los cuatro tipos estan cubiertos.** Logs funcionan para `Deployment`, `StatefulSet`,
   `DaemonSet` y recursos `Pod` standalone. Events funciona para los ocho `_HEALTH_KINDS`.
   Rollout funciona para `Deployment` / `StatefulSet` / `DaemonSet` y se oculta (no aparece
   deshabilitado) para los otros tipos.
3. **Payloads acotados.** Las respuestas de logs se limitan a 256 KB y `tail_lines` ≤ 2000 del
   lado del servidor. Las listas de eventos se limitan a 50. Las listas de rollout se limitan a
   20. Tests unitarios directos para cada limite.
4. **Degradacion elegante.** Cuando el cluster es inalcanzable / un pod ya no existe / la API de
   eventos esta vacia / un workload no tiene pods, los paneles renderizan un mensaje claro y el
   resto del formulario sigue siendo utilizable. Tests directos para cada modo de fallo.
5. **Sin persistencia.** No hay nuevo DocType, ni nueva columna en `Helm Release`. Confirmado por
   inspeccion del diff: nada bajo `kubeport/kubeport/doctype/helm_release/helm_release.json`
   cambia mas alla, como mucho, de una referencia a asset JS.
6. **Tres suites en verde.** `kubeport/tests/test_observability.py` cubre el modulo utilitario.
   `kubeport/tests/test_api_observability.py` cubre los endpoints whitelistados (resolucion de la
   fila de release, gating por rol, rechazo de entrada malformada). `test_release_health` y
   `test_helm_tasks` existentes siguen en verde.
7. **Docs actualizados en el mismo ciclo.** `README.md` (linea de la guia del operador sobre el
   drilldown de logs/events), `docs/codebase-summary.md` (nuevo modulo),
   `docs/control-plane-state.md` (mover "no full in-app drilldown for pod logs, Kubernetes event
   history, rollout timelines" fuera de Open Gaps), `CHANGELOG.md` (entrada estilo ADR).
8. **Sin nuevos marcadores TODO/FIXME/skip** en ningun archivo tocado por el ciclo.

## Archivos criticos

- Nuevo: `kubeport/utils/observability.py`, `kubeport/api/observability.py`,
  `kubeport/tests/test_observability.py`, `kubeport/tests/test_api_observability.py`.
- Modificar: `kubeport/kubeport/doctype/helm_release/helm_release.js` (botones de accion del
  drilldown + paneles laterales), `kubeport/utils/release_health.py` (anadir `pod_count` al
  resultado por workload), `kubeport/hooks.py` solo si hace falta una nueva ruta de asset JS.

## Follow-ups conocidos (pospuestos de forma explicita)

- Streaming de logs en tiempo real (tail respaldado por WebSocket).
- Metricas de CPU/memoria desde metrics-server / Prometheus.
- Observabilidad por Frappe Site (la misma superficie de drilldown, pero anclada en los pods de
  la fila de Site).
- Timeline de eventos a escala de cluster (eventos no acotados a un recurso unico).
- Boton de undo rollout a nivel de workload (separado del rollback de Helm).
- Busqueda grep / regex sobre todos los pods de un release.
