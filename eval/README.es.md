# Arnés de evaluación de Kubeport

Un runner de extremo a extremo reproducible que ejercita el camino dorado de Kubeport
contra un clúster k3d objetivo y emite un informe JSON legible por máquina.
Este arnés es la fuente de los números empíricos citados en
[`docs/thesis.md`](../docs/thesis.md) y
[`docs/control-plane-state.md`](../docs/control-plane-state.md).

## Estructura

| Ruta | Propósito |
|---|---|
| `eval/harness.py` | Driver en el host. Valida el contenedor de desarrollo, envía el script al contenedor, captura el informe JSON. |
| `eval/_inproc.py` | Se ejecuta dentro del contenedor de desarrollo. Habla con Frappe directamente a través del intérprete Python del bench; ejercita el camino dorado de 10 pasos. |
| `eval/results/` | Informes con marca de tiempo más un `sample.json` versionado de una ejecución conocida como buena. |
| `eval/results/sample.json` | Ejecución de referencia para citar en la tesis. |

## Requisitos previos

- Contenedor de desarrollo `tfg_devcontainer-frappe-1` en ejecución, con la
  aplicación `kubeport` instalada en el site del bench (por defecto
  `frappe-k8s.localhost`). Consulta `~/workspace/school/tfg/devcontainer-example`.
- Deben estar en ejecución dentro del contenedor de desarrollo un worker de la cola
  long, un worker de la cola default y el scheduler del bench — el arnés falla rápido
  en caso contrario. Arráncalos con:
  ```bash
  docker exec -d tfg_devcontainer-frappe-1 bash -lc \
    'cd /workspace/development/bench-16 && bench worker --queue long > /tmp/worker-long.log 2>&1'
  docker exec -d tfg_devcontainer-frappe-1 bash -lc \
    'cd /workspace/development/bench-16 && bench worker --queue default > /tmp/worker-default.log 2>&1'
  docker exec -d tfg_devcontainer-frappe-1 bash -lc \
    'cd /workspace/development/bench-16 && bench schedule > /tmp/scheduler.log 2>&1'
  ```
- `k3d` v5+ en el host con al menos un clúster en ejecución
  (`k3d cluster list`). El arnés lee un kubeconfig del host;
  **no** arranca un clúster nuevo — los clústeres preexistentes se
  reutilizan según el invariante de AGENTS.md de que el bench del contenedor
  de desarrollo aloja el propio Kubeport.
- Helm Repository `frappe/erpnext` accesible: `https://helm.erpnext.com`
  (por defecto). Sobreescribe con `--helm-repo-url`.
- Una fila `Kubeport Site Image` curada ya inicializada por
  `kubeport.tasks.site_image_tasks.enqueue_sync_site_image_catalog`
  (el trabajo diario programado en `hooks.py`).

Sin dependencias Python en el host más allá de la librería estándar: el
driver del host solo invoca `docker` y `k3d` mediante shell.

## Ejecución

```bash
# From repo root
make eval                 # default config: k3d-cluster=frappe-cluster, ERPNext chart
make eval-clean           # delete all eval/results/*.json reports

# Or invoke the driver directly with custom parameters:
python3 eval/harness.py \
    --k3d-cluster frappe-cluster \
    --release-name my-eval \
    --namespace my-eval \
    --site-name my-eval.localhost
```

Cada invocación escribe un nuevo fichero `eval/results/<utc-timestamp>.json`.
Los informes existentes nunca se sobreescriben.

## Camino dorado (10 fases)

Las fases se corresponden con el ciclo de vida descrito en
[`docs/control-plane-state.md`](../docs/control-plane-state.md). Cada
fase rota un token de operación bajo el capó — consulta AGENTS.md §3.

| # | Fase | Qué hace | Estado terminal esperado |
|---|---|---|---|
| 1 | `setup_cluster_doc` | Inserta (o reutiliza) una fila `Kubernetes Cluster` a partir del kubeconfig proporcionado | la fila existe |
| 2 | `setup_helm_repo` | Inserta (o reutiliza) una fila `Helm Repository`, dispara `sync_charts`, espera a `last_synced` | `last_synced` poblado |
| 3 | `verify_chart` | Confirma que la fila `Helm Chart` solicitada existe tras la sincronización | la fila existe |
| 4 | `create_release` | Inserta una nueva fila `Helm Release` con el chart + clúster | fila insertada |
| 5 | `deploy_release` | Llama a `deploy_release` y espera hasta que la fila alcance `Deployed` | `status == "Deployed"` |
| 6 | `create_site` | Inserta una fila `Frappe Site`, llama a `create_site`, espera a `Active` | `status == "Active"` |
| 7 | `migrate_site` | Llama a `migrate_site`, espera a `Active` | `status == "Active"` |
| 8 | `backup_site` | Llama a `backup_site`, espera a que la fila `Frappe Site Backup` alcance `Available` | `status == "Available"` |
| 9 | `restore_site` | Llama a `restore_site` contra esa copia de seguridad, espera a `Active` | `status == "Active"` |
| 10 | `drop_site` | Llama a `delete_site`, espera hasta que la reconciliación elimine la fila | fila ausente |

Una fase pasa a `passed` cuando se alcanzó el estado terminal dentro del
timeout configurado. Si una fase falla, todas las fases posteriores se marcan
como `skipped` y el informe registra el detalle del fallo original.

## Esquema del informe

Nivel superior:

```json
{
  "schema_version": 1,
  "generated_at": "2026-05-10T18:00:00Z",
  "started_at":   "2026-05-10T17:50:00Z",
  "finished_at":  "2026-05-10T18:00:00Z",
  "duration_seconds": 600.0,
  "host_wall_seconds": 605.2,
  "context":   { ... non-secret arguments ... },
  "invocation":{ ... resolved invocation parameters ... },
  "phases":    [ { ... per-phase object ... } ],
  "summary": {
    "passed": 10,
    "failed": 0,
    "skipped": 0,
    "total":   10
  }
}
```

Objeto por fase:

```json
{
  "phase": "deploy_release",
  "started_at":  "2026-05-10T17:53:00Z",
  "finished_at": "2026-05-10T17:55:30Z",
  "duration_seconds": 150.0,
  "timeout_seconds":  900,
  "status": "passed",
  "detail": ""
}
```

`status` es uno de `passed`, `failed`, `skipped`, `pending`. Los secretos
(contenido del kubeconfig, contraseñas admin/db, valores YAML en bruto) se eliminan
de `context` antes de la serialización.

## Idempotencia

- Cada ejecución usa valores predeterminados con marca de tiempo para `--release-name`,
  `--namespace` y `--site-name` de modo que ejecuciones concurrentes o repetidas no
  colisionen.
- Las filas de Cluster y Helm Repository se reutilizan si ya existe una fila con
  el mismo nombre — ambas son portadoras de identidad idempotentes.
- La fase 10 (`drop_site`) limpia la fila `Frappe Site` específica de la ejecución.
  Las Helm Releases y el namespace específico de la ejecución se dejan para que
  el operador los inspeccione; desinstala desde el formulario de Helm Release cuando termines.

## Modo reutilización vs. modo limpio

El arnés está dirigido por identidad: las fases que encuentran una fila existente
con el nombre solicitado son no-ops. Esto hace posibles dos formas de ejecución:

- **Modo limpio de extremo a extremo** — pasa valores distintos para `--cluster-doc-name`,
  `--release-name` y `--namespace`. Las fases 1, 4 y 5 hacen trabajo real;
  la ejecución ejercita el ciclo de vida completo que describe el plan TODO-04.
- **Modo reutilización** — apunta a un `Kubernetes Cluster` y `Helm Release`
  existentes (por ejemplo, el `demo-bench` del contenedor de desarrollo). Las fases 1-5
  se cortocircuitan; la ejecución mide solo el ciclo de vida por site
  (fases 6-10).

El `results/sample.json` versionado se produjo en **modo reutilización**
contra el doc de clúster `demo-k3d` del contenedor de desarrollo y la release
ERPNext `demo-bench`: se crea un site nuevo `eval-<timestamp>.localhost`,
se migra, se respalda, se restaura y se elimina. Esta es la forma realista
de una medición ejecutada en CI, ya que redesplegar ERPNext desde cero
en cada ejecución eclipsaría el ciclo de vida por site que se está caracterizando.

## Presupuesto de tiempo

Una ejecución limpia en modo reutilización apunta a <30 min de tiempo de reloj en el
contenedor de desarrollo (medidos 1710s = 28,5 min en la ejecución de referencia).
La mayor parte de ese tiempo son los cuatro Jobs de K8s (`bench new-site`, `migrate`,
`backup`, `restore`, `drop-site`) más el intervalo cron de reconciliación de 5 minutos.
Los timeouts por fase se establecen generosamente por encima de las medianas
esperadas para absorber descargas de imágenes y retrasos en la planificación de pods;
ajústalos para uso en CI. Una ejecución limpia de extremo a extremo añade otros
~5-10 min para el despliegue Helm de ERPNext.

## Escenarios de fallo (TODO-05)

El arnés de inyección de fallos vive bajo `eval/faults/` y está dirigido por
`eval/faults/run.py`. Cada escenario valida empíricamente una
defensa de robustez listada en `docs/control-plane-state.md` §Robustness
Properties. Los informes aterrizan en `eval/results/faults-<utc-timestamp>.json`
y los targets `make eval-faults` / `make eval-faults-real` cubren las
formas de invocación predeterminadas.

| Escenario | Invariante defendido | Testigo | Estado |
|---|---|---|---|
| `worker_kill_mid_helm_upgrade` | El reconciliador de operaciones obsoletas recupera filas `Helm Release` varadas por el worker en menos de `STALE_OPERATION_THRESHOLD_MINUTES` (30 min) | [`kubeport/tasks/reconciliation.py:_reconcile_stale_helm_operations`](../kubeport/tasks/reconciliation.py) | Implementado |
| `job_ttl_expired_before_reconcile` | La reconciliación recurre a la sonda del bench como fuente de verdad cuando el Job de operación ya no existe antes de que el tick lo lea | [`kubeport/tasks/reconciliation.py:_probe_site_state`](../kubeport/tasks/reconciliation.py) | Implementado |
| `pod_exec_timeout_during_site_probe` | La sonda de tres estados devuelve `unknown` ante un fallo transitorio de pod-exec; la fila permanece In Progress durante el tick y se recupera en el siguiente tick | [`kubeport/utils/discovery.py:_exec_list_sites`](../kubeport/utils/discovery.py) y [`kubeport/tasks/reconciliation.py:_probe_site_state`](../kubeport/tasks/reconciliation.py) | Implementado |
| `corrupt_archive_size_sidecar` | La sonda sidecar de PVC marca la copia de seguridad como `Failed` cuando el sidecar `<archive>.size` está truncado; el borrado de la fila encola la limpieza de la papelera de archivos que elimina el archivo del PVC | [`kubeport/tasks/reconciliation.py:_probe_backup_archive_on_pvc`](../kubeport/tasks/reconciliation.py) y [`kubeport/tasks/site_tasks.py:delete_backup_archive_task`](../kubeport/tasks/site_tasks.py) | Implementado |

### Ejecución de los escenarios implementados

`worker_kill_mid_helm_upgrade` requiere:

- Una fila `Helm Release` existente en estado `Deployed` (o `Degraded`) — el arnés ejecuta un upgrade no-op contra ella. Por defecto: `demo-k3d/demo/demo-bench` (la misma release que usa `make eval`).
- El contenedor de desarrollo, el worker de la cola long y el scheduler del bench en ejecución según la sección Requisitos previos de arriba.

`job_ttl_expired_before_reconcile` y `pod_exec_timeout_during_site_probe` requieren ambos:

- Una fila `Frappe Site` existente en estado `Active` cuyo site subyacente
  esté **realmente funcional** en el bench (`bench list-apps` termina con 0
  dentro del pod del bench). Por defecto: `demo-k3d/demo/demo-bench/erp.cluster.local`.
  Si tu bench tiene un site distinto conocido como bueno, sobreescribe con
  `--site-doc-name <docname>`.
- El contrato de la sonda se apoya en la release/clúster existente al que apunta la
  fila — no se necesita configuración adicional de clúster más allá de un bench sano.

`pod_exec_timeout_during_site_probe` además aplica un monkey-patch a
`kubeport.utils.discovery._exec_list_sites` durante un tick de reconciliación para
lanzar un `urllib3.exceptions.ReadTimeoutError` sintético; el parche se
restaura en un bloque `finally` antes del segundo tick para que el contenedor
de desarrollo quede en el mismo estado en que se encontró.

`corrupt_archive_size_sidecar` requiere que el PVC `kubeport-backups` esté
presente en el namespace objetivo (creado automáticamente por Kubeport en la
primera copia de seguridad) y el worker de la cola long del bench para que el
borrado de la fila posterior a Failed encole el Job de limpieza del archivo.
El escenario envía dos Jobs `busybox` de vida corta que montan el PVC: uno para
truncar `<archive>.size` a cero bytes, otro para verificar que el archivo está
ausente tras la ejecución de la limpieza de la papelera. Ambos Jobs llevan la
etiqueta estándar `app.kubernetes.io/managed-by=kubeport` y un
`ttlSecondsAfterFinished` de 60s para autolimpiarse.

```bash
# Fast path: for both scenarios, backdate the staleness clock or
# directly invoke the per-doctype reconciler so recovery is observed
# in seconds; the report records fast_forward_used=true per scenario.
make eval-faults

# Realistic path: wait for the natural 5-min cron tick (and, for
# scenario 1, the 30-min staleness window). Wall-clock typically
# 30-35 min for scenario 1; ~5 min for scenario 2.
make eval-faults-real
```

Tras cada escenario el arnés comprueba si el worker de la cola long sigue
arriba y lo reinicia si es necesario (el escenario 1 lo mata
deliberadamente). Pasa `--no-restart-worker` a `eval/faults/run.py` para
omitir el reinicio (útil al investigar). El contenedor de desarrollo
queda en el mismo estado en que se encontró.

### Esquema del informe

El nivel superior se corresponde con la forma del informe del camino dorado (`schema_version`,
`generated_at`, `started_at`, `finished_at`, `duration_seconds`,
`context`, `summary`). Cada entrada en `scenarios[]` lleva:

```json
{
  "scenario": "worker_kill_mid_helm_upgrade",
  "defended_invariant": "Stale operation reconciler recovers worker-stranded Helm Release rows",
  "witness": { "reconciler": "<file:symbol>", "threshold_minutes": 30 },
  "injected_at":   "2026-05-10T18:00:00Z",
  "recovered_at":  "2026-05-10T18:00:12Z",
  "mttr_seconds":  12.0,
  "expected_mttr_bound_seconds": 1800,
  "fast_forward_used": true,
  "observed":  { "final_status": "Deployed", "operation_token_rotated": true },
  "passed":    true,
  "detail":    "Stale-op reconciler recovered the stranded row to 'Deployed' in 12.0s (fast-forwarded).",
  "steps": [ ... ordered timeline of every observation and mutation ... ]
}
```

La ejecución de referencia está versionada en `eval/results/sample-faults.json`
para citar en la tesis.

## Referencias cruzadas

- Invariantes que el arnés ejercita: AGENTS.md §Design Invariants.
- Máquinas de estado por DocType: `docs/control-plane-state.md`.
- Propiedades de robustez emparejadas con escenarios:
  `docs/control-plane-state.md` §Robustness Properties.
