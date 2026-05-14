# Arnés de comparación con la línea base

Una reejecución lado a lado del mismo camino dorado de 10 fases ejecutado por
[`eval/harness.py`](../harness.py), pero dirigido enteramente por
`kubectl`, `helm` y `kubectl exec ... -- bench` en bruto — sin Kubeport en
el bucle. El JSON de salida tiene el mismo esquema que el informe del arnés
principal, más los campos por fase `commands_issued` y `manual_steps`.
Esta es la base empírica para la afirmación sobre el estado del arte en
[`docs/thesis.md`](../../docs/thesis.md) §2.

## Estructura

| Ruta | Propósito |
|---|---|
| `eval/baseline/run.sh` | Script dentro del contenedor. Ejecuta cada fase con herramientas CLI en bruto; contabiliza tiempo de reloj, número de comandos y pasos manuales; emite JSON entre los marcadores `RESULT_BEGIN` / `RESULT_END`. |
| `eval/baseline/host_driver.py` | Envoltorio en el host. Obtiene el kubeconfig (reescribiendo `0.0.0.0` al gateway del bridge del contenedor de desarrollo para que kubectl dentro del contenedor pueda alcanzar el puerto k3d publicado del host), copia el script al contenedor, lo ejecuta, captura el JSON, escribe `eval/results/baseline-<utc>.json`. |
| `eval/results/sample-baseline.json` | Ejecución de referencia para citar en la tesis. |
| `eval/results/comparison.md` | Tabla lado a lado que resume línea base frente a Kubeport (`eval/results/sample-baseline.json` frente a `eval/results/sample.json`). |

## Metodología

Mismo clúster objetivo, mismo chart, misma imagen, mismo contenedor de desarrollo,
mismo ciclo de vida por site que usa la muestra en modo reutilización del arnés
principal — la única diferencia es si el operador teclea comandos CLI o
inserta/actualiza filas de DocType. La línea base no arranca un despliegue limpio
de ERPNext porque eclipsaría los números por site que se están caracterizando;
la comparación trata sobre el ciclo de vida por site (fases 6–10) más la
sobrecarga del operador que Kubeport elide en las fases 1–5.

`run.sh` se ejecuta contra la Helm Release `demo-bench` existente en el clúster
k3d `pacopepe` (la misma fixture que `eval/results/sample.json`),
creando un site nuevo `eval-baseline-<timestamp>.localhost` de modo que las
reejecuciones no colisionen.

## Conteo de `commands_issued` y `manual_steps`

`commands_issued` cuenta las invocaciones distintas de shell `kubectl` / `helm`
que dispara una fase. Las funciones envoltorio en `run.sh`
(`k`, `h`) incrementan un contador por fase una vez por llamada de modo que el recuento sea
mecánico. `kubectl wait` cuenta como un comando aunque haga polling.

`manual_steps` cuenta las decisiones o intervenciones del operador que la
fase necesita y que Kubeport oculta tras un DocType. Cada conteo está
documentado en línea en `run.sh` junto al lugar de llamada, de modo que el conteo es
auditable. Concretamente:

| Fase | Paso manual | Lo que hace Kubeport en su lugar |
|---|---|---|
| 1 `setup_cluster_doc` | Elegir el contexto correcto del kubeconfig | Almacenado en la fila `Kubernetes Cluster` |
| 4 `create_release` | Escribir a mano un `values.yaml` | El DocType valida + fusiona los valores predeterminados del chart |
| 5 `deploy_release` | Sondear manualmente la preparación del pod | El bucle de reconciliación hace polling y escribe `helm_status_detail` |
| 6 `create_site` | Resolver el pod del bench por etiqueta; elegir contraseña de admin; proporcionar credenciales de root de DB | Resolución de pod + inyección de Secret por Job en `tasks/site_tasks.py` |
| 8 `backup_site` | Elegir un directorio destino en el host; aprender los nombres de los nuevos ficheros desde el directorio de backups del bench | El script envoltorio hace diff antes/después y canaliza al PVC `kubeport-backups` |
| 9 `restore_site` | Emparejar el conjunto de archivos con los flags de bench restore | `_bench_restore_command` extrae el tarball y ensambla los flags |
| ... | ... (resolve_pod contabiliza un paso manual extra en cada fase que lo usa: 6, 7, 8, 9, 10) | El DocType cachea el selector de pod |

## Trampas del mapeo de fases

Dos fases se colapsan frente a la línea base de CLI en bruto:

- `setup_cluster_doc` no tiene análogo CLI real — es un único
  `kubectl config use-context` (o el contexto actual implícito). El
  único paso manual que contamos es que el operador decida a qué contexto
  apuntar.
- `create_release` es un puro insert de DocType en Kubeport sin
  E/S de clúster en absoluto; el análogo en la línea base es escribir a mano un
  `values.yaml`. Cero comandos, un paso manual.

Ambas se mantienen en la tabla para alinear las filas con el esquema del arnés
principal; sus duraciones bajas son honestas, no rellenadas.

## Sobre qué *no* trata la comparación

El tiempo de reloj por fase puede salir **más rápido** para la línea base en
algunas fases — `kubectl wait --for=condition=Ready` es más directo
que esperar al tick de reconciliación de 5 minutos de Kubeport para observar
el mismo hecho. La afirmación de la tesis es sobre la productividad del operador
(comandos + pasos manuales), no sobre la velocidad cruda de un único disparo.
`eval/results/comparison.md` señala esto para que un revisor no malinterprete
la tabla.

## Ejecución

```bash
# From repo root, with the dev container up and the k3d cluster
# (default: pacopepe) running with the demo-bench release deployed.
make eval-baseline

# Or invoke the host driver directly with custom parameters:
python3 eval/baseline/host_driver.py \
    --k3d-cluster pacopepe \
    --release-name demo-bench \
    --namespace demo \
    --db-root-password changeit
```

Cada invocación escribe un nuevo `eval/results/baseline-<utc>.json`.
Los informes existentes nunca se sobreescriben.

## Esquema del informe

Nivel superior — misma forma que `eval/results/sample.json`:

```json
{
  "schema_version": 1,
  "generated_at":   "2026-05-10T18:00:00Z",
  "started_at":     "2026-05-10T17:50:00Z",
  "finished_at":    "2026-05-10T18:00:00Z",
  "duration_seconds": 600.0,
  "host_wall_seconds": 605.2,
  "context":   { ... non-secret arguments ... },
  "invocation":{ ... resolved invocation parameters ... },
  "phases":    [ { ... per-phase object ... } ],
  "summary": {
    "passed": 10,
    "failed": 0,
    "skipped": 0,
    "total":   10,
    "commands_issued_total": 32,
    "manual_steps_total":    13
  }
}
```

Objeto por fase — misma forma que en el arnés principal, más los dos nuevos
campos:

```json
{
  "phase": "create_site",
  "started_at":  "2026-05-10T17:53:00Z",
  "finished_at": "2026-05-10T17:55:30Z",
  "duration_seconds": 150.0,
  "timeout_seconds":  900,
  "status": "passed",
  "detail": "",
  "commands_issued": 2,
  "manual_steps":    3
}
```

## Referencias cruzadas

- Arnés principal: [`eval/README.md`](../README.md).
- Máquinas de estado por DocType que el operador orquesta a mano:
  [`docs/control-plane-state.md`](../../docs/control-plane-state.md).
- Encuadre del estado del arte que esta ejecución informa:
  [`docs/thesis.md`](../../docs/thesis.md) §2.
