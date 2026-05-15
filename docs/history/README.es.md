# Documentos historicos de planificacion

Este directorio guarda documentos que capturaron planificacion, exploracion de diseno o
detalle procedimental en un momento concreto de la vida del proyecto. Se conservan por
**trazabilidad**: registran como se consideraron las decisiones antes de tomarlas, pero ya no
son la referencia autorizada del comportamiento actual.

Para el estado actual del sistema, lee en su lugar:

- [`docs/control-plane-state.md`](../control-plane-state.md) — capacidades actuales, defensas de
  robustez y huecos abiertos.
- [`docs/operator-guide.md`](../operator-guide.md) — como usar el sistema de extremo a extremo,
  incluyendo el procedimiento de smoke test que reemplaza al historico `frappe-site-smoke.md`
  de aqui.
- [`docs/architecture.md`](../architecture.md) — diagramas C4 e invariantes de diseno.
- [`CHANGELOG.md`](../../CHANGELOG.md) — registro de decisiones de arquitectura.

## Indice

| Archivo | Que capturaba | Estado |
|---|---|---|
| `plan-a-site-backup-restore.md` | Exploracion inicial del diseno de backup / restore | Implementado; sustituido por el codigo actual en `kubeport/tasks/site_tasks.py` y la seccion de backups de la guia del operador. |
| `plan-b-observability-drilldown.md` | Diseno inicial de los paneles de observabilidad dentro del formulario | Implementado; sustituido por `kubeport/api/observability.py` y el comportamiento del formulario de Helm Release. |
| `plan-f-application-abstraction.md` | Exploracion de una abstraccion de "application" de mayor nivel sobre Helm releases | No implementado; se conserva para trabajo futuro. |
| `frappe-site-smoke.md` | Procedimiento manual de smoke pre-lanzamiento | Sustituido por la seccion 10 de `docs/operator-guide.md`. |
