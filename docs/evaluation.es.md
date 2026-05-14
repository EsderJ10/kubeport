# Evaluación

Este capítulo recoge la evaluación empírica de Kubeport frente a los cinco objetivos enunciados en [`docs/thesis.md`](thesis.md) §3. Toda afirmación cuantitativa cita el informe JSON bajo [`eval/results/`](../eval/results/) que la generó; cada informe es regenerable mediante el harness descrito en [`eval/README.md`](../eval/README.md).

La evaluación se estructura en cuatro ejes:

1. **Funcional** — ¿se completa de extremo a extremo el ciclo de vida dorado de Frappe-sobre-Kubernetes de 10 fases?
2. **Fiabilidad bajo inyección de fallos** — ¿recuperan las defensas de robustez documentadas el sistema dentro de los límites documentados?
3. **Línea base comparativa** — ¿qué coste tiene el mismo flujo de trabajo cuando se ejecuta con `kubectl` + `helm` en bruto?
4. **Envolvente de escalado** — ¿cómo escala el bucle de reconciliación con el número de filas de estado deseado persistidas?

Cada eje se corresponde con uno de los cuatro harnesses de `eval/` (`make eval`, `make eval-faults`, `make eval-baseline`, `make eval-scaling`).

---

## 1. Funcional

**Fuente**: [`eval/results/sample.json`](../eval/results/sample.json). Driver: `make eval`. Metodología: [`eval/README.md`](../eval/README.md).

El harness recorre el camino dorado de 10 fases a través de las APIs whitelisted de Kubeport (sin clics en la UI) contra un clúster k3d real, desde una fila `Kubernetes Cluster` recién creada hasta una fila `Frappe Site` eliminada.

| Fase | Estado | Tiempo de pared (s) |
|---|---|---:|
| `setup_cluster_doc`  | passed | 0.5 |
| `setup_helm_repo`    | passed | 0.7 |
| `verify_chart`       | passed | 0.0 |
| `create_release`     | passed | 0.0 |
| `deploy_release`     | passed | 0.0 |
| `create_site`        | passed | 570.9 |
| `migrate_site`       | passed | 176.1 |
| `backup_site`        | passed | 244.2 |
| `restore_site`       | passed | 472.4 |
| `drop_site`          | passed | 245.2 |
| **Total**            | **10/10** | **1709.96** |

Las entradas con tiempo de pared `0.0` reflejan el modo de reutilización: la ejecución de referencia reutiliza el clúster, el repositorio, la release y el chart de la invocación previa, por lo que las fases de configuración correspondientes hacen short-circuit. Las fases 1–5 se ejercitan de extremo a extremo en una ejecución en modo fresh; el bloque `context` de [`sample.json`](../eval/results/sample.json) registra `release_doc_created: false` y `deploy_skipped_already_deployed: true` para esa ejecución.

**Resultado**: cada objetivo de [`docs/thesis.md`](thesis.md) §3 tiene una fase en passing en el informe — O1 (`setup_cluster_doc`), O2 (`setup_helm_repo` → `deploy_release`), O4 (`create_site` → `drop_site`), y el enrutado asíncrono bajo O5 está implícito en cada espera por fase. O3 (apply de Service Bundle) se ejercita mediante la suite de tests unitarios en `kubeport/tests/test_reconciliation.py` en lugar de mediante el camino dorado; no forma parte del ciclo de vida crítico cara al operador.

---

## 2. Fiabilidad bajo inyección de fallos

**Fuente**: [`eval/results/sample-faults.json`](../eval/results/sample-faults.json). Driver: `make eval-faults` (modo fast-forward usado aquí para que la ejecución se complete en ~100 s; `make eval-faults-real` elimina el time-warp y deja que el tick de reconciliación de 5 minutos se dispare de forma natural). Metodología: [`eval/README.md`](../eval/README.md) §Fault scenarios.

Cada escenario se corresponde uno a uno con una defensa de robustez enumerada en [`docs/control-plane-state.md`](control-plane-state.md) §Robustness Properties. Para cada uno, el harness mide el **MTTR** (tiempo desde la inyección del fallo hasta que la fila alcanza su estado terminal esperado) y verifica el límite superior documentado de 30 min (1800 s).

| Escenario | Invariante defendido | Witness en el código | MTTR (s) | Límite (s) | Pasa |
|---|---|---|---:|---:|:---:|
| `worker_kill_mid_helm_upgrade` | El reconciliador de operaciones rancias recupera filas `Helm Release` abandonadas por el worker | `kubeport/tasks/reconciliation.py:_reconcile_stale_helm_operations` (umbral 30 min) | **2.30** | 1800 | ✓ |
| `job_ttl_expired_before_reconcile` | La reconciliación recurre a la sonda de verdad sobre el terreno del bench cuando el Job de la operación ya no existe antes de que el tick lo lea | `kubeport/tasks/reconciliation.py:_probe_site_state`, `_reconcile_site_migrate` | **6.68** | 1800 | ✓ |
| `pod_exec_timeout_during_site_probe` | La sonda de tres estados devuelve `unknown` ante un fallo transitorio de pod-exec; el reconciliador difiere la fila en lugar de caer por defecto a `Failed` | `kubeport/utils/discovery.py:_exec_list_sites`, `kubeport/tasks/reconciliation.py:_probe_site_state` | **9.63** (1 tick diferido) | 1800 | ✓ |
| `corrupt_archive_size_sidecar` | La sonda sidecar del PVC marca `Failed` cuando el sidecar de tamaño del archivo está truncado/ausente; el borrado de fila encola la limpieza de basura del archivo que retira el archivo del PVC | `kubeport/tasks/reconciliation.py:_probe_backup_archive_on_pvc`, `_reconcile_site_backup`, `kubeport/tasks/site_tasks.py:delete_backup_archive_task` | **22.34** (archivo eliminado) | 1800 | ✓ |

**Resultado**: los cuatro escenarios pasan dentro del límite documentado de 30 minutos. Las ejecuciones fast-forward miden el tiempo de pared del propio trabajo de recuperación una vez que un tick se ha disparado; en tiempo real el límite lo fija el siguiente tick de reconciliación (≤ 5 min después del umbral de operación rancia para el escenario de Helm, inmediato para los escenarios de sonda del bench).

El escenario `corrupt_archive_size_sidecar` también ejercita el comportamiento en cascada: después de que la fila se marca como `Failed`, la tarea de limpieza de basura (`delete_backup_archive_task`) se encola y el archivo corrupto se elimina del PVC — verificado por el paso `cleanup_check` (`log: ABSENT\nSIZE_ABSENT`). Esta es la garantía de cierre del bucle para el ciclo de vida del archivo de copia de seguridad; sin ella, la ruta de fallo filtraría almacenamiento.

### Inyección continua de fallos en CI

La tabla de referencia anterior se regenera localmente con `make eval-faults`. El mismo escenario `worker_kill_mid_helm_upgrade` está también cableado en [`.github/workflows/chaos.yml`](../.github/workflows/chaos.yml), que se ejecuta en cada pull request y push a `main`. El job de CI arranca un bench Frappe v16, un clúster k3d de un nodo y un worker de cola long, empaqueta el chart in-repo [`eval/faults/fixtures/chaos-chart`](../eval/faults/fixtures/chaos-chart) (un chart de un solo `ConfigMap` usado puramente como objetivo de upgrade rápido y sin pull de imagen), y ejecuta el escenario con `--fast-forward` para que la recuperación se observe en segundos en lugar de en 30 min. El job parsea `RESULT_BEGIN`/`RESULT_END` de la salida del harness in-bench y verifica `passed == true`.

---

## 3. Línea base comparativa (Kubeport vs. `kubectl` + `helm` en bruto)

**Fuente**: [`eval/results/sample-baseline.json`](../eval/results/sample-baseline.json) y [`eval/results/comparison.md`](../eval/results/comparison.md). Driver: `make eval-baseline`. Metodología: [`eval/baseline/README.md`](../eval/baseline/README.md).

La línea base ejecuta el mismo flujo de trabajo de 10 fases sobre el mismo clúster, namespace, release y chart, solo con comandos de shell en bruto. Se añaden dos columnas que no tienen análogo en Kubeport: `commands_issued` (recuento de invocaciones distintas de `kubectl` / `helm`) y `manual_steps` (recuento de intervenciones del operador — véase `eval/baseline/run.sh`, cada llamada a `bump_manual` lleva una justificación inline).

| Fase | Kubeport (s) | Línea base (s) | Comandos línea base | Pasos manuales línea base |
|---|---:|---:|---:|---:|
| `setup_cluster_doc` | 0.5 | 0.2 | 1 | 1 |
| `setup_helm_repo`   | 0.7 | 1.4 | 2 | 0 |
| `verify_chart`      | 0.0 | 12.7 | 1 | 0 |
| `create_release`    | 0.0 | 0.2 | 0 | 1 |
| `deploy_release`    | 0.0 | 1.5 | 2 | 1 |
| `create_site`       | 570.9 | 174.4 | 2 | 3 |
| `migrate_site`      | 176.1 | 43.3 | 2 | 1 |
| `backup_site`       | 244.2 | 9.1 | 7 | 3 |
| `restore_site`      | 472.4 | 21.7 | 5 | 2 |
| `drop_site`         | 245.2 | 5.5 | 2 | 1 |
| **Totales**         | **1710.0** | **273.9** | **24** | **13** |

**El tiempo de pared por fase no es el titular.** La línea base termina el ciclo de vida por site en ~254 s mientras que Kubeport tarda ~1709 s. Ese factor 6,7× es estructural, no es productividad del operador: Kubeport encamina cada acción por site a través de un Job de Kubernetes y espera al tick de reconciliación de 5 minutos para observar el resultado, mientras que la línea base ejecuta `kubectl exec ... -- bench …` de forma síncrona y lee el código de salida directamente. Kubeport paga este coste a propósito — el modelo asíncrono + reconciliación es exactamente lo que sobrevive a las caídas de worker y a la expiración del TTL de Job (§2 arriba).

**El titular es la sobrecarga del operador.** Los totales de la columna de línea base son **24 comandos de shell distintos y 13 intervenciones del operador** para un único ciclo de vida de site sobre un bench ya desplegado. Cada paso manual se justifica inline en [`eval/baseline/run.sh`](../eval/baseline/run.sh) (componer un `values.yaml`, resolver el pod del bench por etiqueta, elegir un directorio destino en el host para el conjunto de archivos de copia de seguridad, emparejar el conjunto de archivos con las flags correctas de `bench restore`, …). Kubeport colapsa cada fase en un único guardado de DocType o clic de botón, eliminando tanto el recuento de comandos de shell como el de pasos manuales.

Esta es la versión empírica del argumento SOTA en [`docs/thesis.md`](thesis.md) §2: la brecha que Kubeport cierra no es "otra UI de Kubernetes" sino el delta de sobrecarga del operador entre un shell con UI sobre `kubectl` y un plano de control integrado.

---

## 4. Envolvente de escalado

**Fuente**: [`eval/results/sample-scaling.json`](../eval/results/sample-scaling.json) y [`eval/results/scaling-tick-latency.png`](../eval/results/scaling-tick-latency.png). Driver: `make eval-scaling`. Metodología: [`eval/README.md`](../eval/README.md) §Scaling.

El harness inserta en bloque N filas sintéticas `Helm Release` / `Service Bundle` / `Frappe Site` en sus estados terminales `Deployed` / `Active`, ejecuta el punto de entrada de reconciliación `kubeport.tasks.reconciliation.reconcile_all_releases` contra un clúster mock, y mide la latencia del tick. Cada N se repite 5 veces.

| N (filas por tipo) | Tick medio (s) | Mediana (s) | Mín (s) | Máx (s) |
|---:|---:|---:|---:|---:|
| 1    | 0.0839 | 0.081  | 0.0381 | 0.1423 |
| 10   | 0.0914 | 0.0881 | 0.0818 | 0.1023 |
| 100  | 1.2373 | 1.102  | 0.8972 | 1.9686 |
| 1000 | 10.4612 | 10.6415 | 9.6329 | 10.8962 |

Un ajuste por ley potencial sobre `(log N, log tick_seconds)` da **pendiente ≈ 0.745**, intercepto ≈ −3.194 (campo `regression` de [`sample-scaling.json`](../eval/results/sample-scaling.json)). El harness anota esto como **`shape: sublinear`** — la latencia del tick crece más despacio que el recuento de filas.

La latencia del subproceso de Helm, muestreada a lo largo de la ejecución del harness, aparece en el mismo informe: media 0.332 s, p95 0.63 s, máx 0.663 s (n=2 en la ejecución de referencia en modo reuse; en ejecuciones en modo fresh el tamaño de la muestra es mayor pero los percentiles se mantienen en el mismo orden de magnitud).

**Resultado**: una única instancia de Kubeport reconcilia 1000 filas en ~10,5 s de tiempo de pared por tick — cómodamente por debajo de la cadencia programada de 5 minutos (300 s, ≈ 28× de margen) y del umbral de operación rancia de 30 minutos (1800 s, ≈ 170× de margen). La forma sublineal es la consecuencia esperada de que las lecturas de BD en bloque dominen el trabajo por fila para las rutas de reconciliación in-database.

El PNG en [`eval/results/scaling-tick-latency.png`](../eval/results/scaling-tick-latency.png) representa los mismos datos en ejes log-log para la figura de la tesis.

---

## 5. Resumen frente a objetivos

| # | Objetivo | Evidencia |
|---|---|---|
| O1 | Conectividad con clústeres (3 modos de autenticación) | §1 — `setup_cluster_doc` pasa contra un clúster k3d real ([`sample.json`](../eval/results/sample.json)). |
| O2 | Ciclo de vida de release de Helm | §1 — `setup_helm_repo`, `verify_chart`, `create_release`, `deploy_release` pasan todos ([`sample.json`](../eval/results/sample.json)). |
| O3 | Despliegue de manifiestos en bruto (Service Bundle) | Cubierto por `kubeport/tests/test_reconciliation.py` (casos de transición de estado de apply / delete de Service Bundle). No forma parte del harness del camino dorado. |
| O4 | Ciclo de vida de site de Frappe | §1 — `create_site`, `migrate_site`, `backup_site`, `restore_site`, `drop_site` pasan todos ([`sample.json`](../eval/results/sample.json)). |
| O5 | Invariantes de ingeniería de plataformas | §2 — las cuatro defensas de robustez se recuperan dentro del límite de 30 min ([`sample-faults.json`](../eval/results/sample-faults.json)). §4 — el tick de reconciliación escala de forma sublineal hasta 1000 filas, ≈ 28× de margen bajo la cadencia programada ([`sample-scaling.json`](../eval/results/sample-scaling.json)). |

La sobrecarga del operador frente a la línea base de herramientas en bruto (§3) es el complemento empírico: 24 comandos de shell y 13 intervenciones manuales por ciclo de vida de site eliminados, a cambio de un coste en tiempo de pared de ~6,7× pagado por el modelo asíncrono + reconciliación que exige O5.

---

## Reproducción

Los cuatro informes se regeneran desde el host desde un contenedor de desarrollo limpio:

```bash
make eval            # → eval/results/<utc-timestamp>.json   (functional)
make eval-faults     # → eval/results/faults-<utc-timestamp>.json (reliability)
make eval-baseline   # → eval/results/baseline-<utc-timestamp>.json (baseline)
make eval-scaling    # → eval/results/scaling-<utc-timestamp>.json + scaling-tick-latency.png
```

Cada invocación escribe un nuevo informe con marca de tiempo; los informes existentes nunca se sobrescriben. Los ficheros `eval/results/sample*.json` versionados en el repositorio son las ejecuciones de referencia citadas arriba.
