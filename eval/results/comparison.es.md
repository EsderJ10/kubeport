# Kubeport vs `kubectl + helm` en bruto — comparación del camino dorado

Medición lado a lado del mismo flujo de trabajo de Frappe sobre Kubernetes
de 10 fases ejecutado dos veces contra el mismo clúster objetivo, mismo chart,
misma imagen: una vez a través de los DocTypes de Kubeport
([`eval/results/sample.json`](sample.json)) y otra con
`kubectl` / `helm` / `kubectl exec ... -- bench` en bruto
([`eval/results/sample-baseline.json`](sample-baseline.json)).
Metodología y reglas de conteo:
[`eval/baseline/README.md`](../baseline/README.md).

| Fase | Kubeport (s) | Línea base (s) | Comandos de línea base | Pasos manuales de línea base |
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

Los números del lado Kubeport proceden de la ejecución de referencia en modo
reutilización del 2026-05-10 ([`sample.json`](sample.json)); los números de
la línea base proceden de la ejecución equivalente en modo reutilización sobre
el mismo clúster + namespace + release
([`sample-baseline.json`](sample-baseline.json)). La interacción del operador
con Kubeport es de ~1 save de DocType o 1 click de botón por
fase, con la validación, la resolución de pods, el manejo de archivos y
el ensamblado de flags capturados en código y no en las manos del operador
— el lado Kubeport no tiene `commands` ni `manual_steps` que
reportar.

## Cómo leer la tabla

**El tiempo de reloj por fase no es el titular.** La línea base termina
el ciclo de vida por site (fases 6–10) en 254s frente a los 1709s de Kubeport.
Esa brecha es estructural, no de productividad del operador: Kubeport enruta
cada acción por site a través de un Job de Kubernetes y espera al
tick de reconciliación de 5 minutos para observar el resultado, mientras que la
línea base ejecuta `kubectl exec ... -- bench …` de forma síncrona y lee
el código de salida directamente. Kubeport paga este coste a propósito — el
modelo async + reconciliación es lo que sobrevive a las caídas de worker y al
vencimiento del TTL de los Jobs (consulta `docs/control-plane-state.md` §Robustness Properties)
— pero la columna de tiempo de reloj es una diferencia contable, no una
diferencia de tiempo del operador.

**El titular es la sobrecarga del operador.** La columna de la línea base totaliza
**24 comandos de shell y 13 intervenciones manuales** para un único
ciclo de vida de site sobre un bench ya desplegado; Kubeport elide ambos en
flujos de trabajo basados en formularios. Cada incremento de `manual_steps` es auditable en
[`eval/baseline/run.sh`](../baseline/run.sh) — cada llamada a `bump_manual`
tiene un comentario en línea que nombra la intervención del operador que
representa (por ejemplo, componer un `values.yaml`, resolver el pod del bench
por etiqueta, elegir un directorio destino en el host para el conjunto de
archivos de copia de seguridad, emparejar el conjunto de archivos con los
flags correctos de `bench restore`). El recuento de 24 comandos es mecánico:
cada invocación envuelta de `kubectl` / `helm` incrementa un contador por
fase en su único lugar de llamada.

## Reproducción

```bash
# Kubeport side (reuse mode, see eval/README.md):
make eval

# Baseline side (reuse mode, see eval/baseline/README.md):
make eval-baseline
```

Ambas ejecuciones apuntan al clúster k3d demo `pacopepe` con la
Helm Release `demo-bench` de ERPNext en el namespace `demo`. Cada ejecución
produce un nuevo informe con marca de tiempo; esta comparación se obtiene
de los `sample.json` / `sample-baseline.json` versionados.
