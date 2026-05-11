# CLAUDE.md

Referencia rápida para sesiones de Claude Code. Para el conjunto completo de reglas, invariantes de diseño y patrones de implementación, consulta [`AGENTS.md`](AGENTS.md).

## Proyecto

Kubeport es una aplicación Frappe que actúa como plano de control de Kubernetes. **Estado deseado → MariaDB (DocTypes). Estado observado → consultas en vivo al clúster. Nunca mezclarlos.**

## Comandos

```bash
# Formateo y linting (instalación en el host)
ruff format                  # Formateo Python (tabuladores, comillas dobles, 110 caracteres)
ruff                         # Linting Python
prettier                     # Formateo JS/CSS
eslint                       # Linting JavaScript

# Lint en contenedor (sin instalación en el host — usa docker-compose.lint.yml)
make fmt                     # formatear en local
make lint                    # reportar hallazgos
make fix                     # corregir automáticamente + formatear
make lint-check              # ejecución exacta en modo CI

# Pre-commit (obligatorio)
cd apps/kubeport && pre-commit install

# Tests (requiere bench Frappe en funcionamiento + site)
bench --site <site> run-tests --app kubeport --doctype <DocType>
```

## Estilo de código

- **Sangría**: tabuladores
- **Comillas**: comillas dobles
- **Anotaciones de tipo**: obligatorias en todos los métodos de API públicos (`frappe.types.DF` para campos de DocType)

## Arquitectura de un vistazo

| Capa | Ruta | Notas |
|---|---|---|
| DocTypes | `kubeport/kubeport/doctype/` | Estado deseado en MariaDB |
| API | `kubeport/api/` | Endpoints públicos de solo lectura |
| Utilidades | `kubeport/utils/` | Helpers sin estado para K8s y Helm |
| Tareas | `kubeport/tasks/` | Todo el trabajo que muta el clúster (trabajos en segundo plano) |
| Programado | `hooks.py` | `*/5 * * * *` → reconciliación, diario → sincronización de repositorios |

## Reglas críticas (ver AGENTS.md para más detalles)

1. **Todas las mutaciones del clúster van a través de trabajos en segundo plano** — nunca desde el hilo web.
2. **El descubrimiento es de solo lectura** — nunca persistir el estado descubierto en MariaDB.
3. **Usar tokens de operación/sincronización** — los workers en segundo plano deben volver a comprobar los tokens antes de actuar.
4. **Actualizaciones de campos dirigidas en las tareas** — usar `db_set` / `frappe.db.set_value`, no `doc.reload()`.
5. **Renderizado asíncrono de formularios** — usar `frappe.xcall` + renderizado en el cliente para datos externos, no `doc.onload`.

## Documentación

| Fichero | Propósito |
|---|---|
| `README.md` | Punto de entrada: resumen de capacidades, esquema de arquitectura, instalación, mapa de documentos |
| `AGENTS.md` | Invariantes y patrones de implementación autoritativos |
| `CONTRIBUTING.md` | Configuración del entorno de desarrollo, flujo de lint/test, convenciones de PR |
| `SECURITY.md` | Política de divulgación y notas sobre fronteras de confianza |
| `docs/threat-model.md` | Diagrama de fronteras de confianza, catálogo STRIDE, mapeo endpoint × frontera |
| `docs/thesis.md` | Encuadre del proyecto: problema, estado del arte, objetivos, resultados, trabajo futuro |
| `docs/architecture.md` | Diagramas C4, secuencias en tiempo de ejecución, invariantes de diseño |
| `docs/operator-guide.md` | Flujos de trabajo del operador de extremo a extremo + procedimiento de verificación |
| `docs/deploy.md` | Guía de despliegue fuera del entorno de desarrollo: topología, imagen de bench, RBAC, dimensionamiento, monitorización, copia de seguridad del plano de control |
| `deploy/rbac/README.md` | Manifiestos RBAC dentro del clúster con Kustomize + validador `make rbac-smoke` |
| `docs/control-plane-state.md` | Capacidades, defensas de robustez, brechas abiertas |
| `docs/codebase-summary.md` | Referencia de arquitectura a nivel de módulo |
| `docs/evaluation.md` | Capítulo de evaluación empírica (funcional, fiabilidad, línea base, escalado) |
| `docs/references.bib` | Bibliografía BibTeX para las citas del §2 y §9 de `docs/thesis.md` |
| `docs/history/` | Documentos de planificación archivados (ver su README) |
| `eval/README.md` | Conjunto de evaluación de extremo a extremo (`make eval`) |
| `CHANGELOG.md` | Registro de decisiones de arquitectura |

Actualiza la documentación en el mismo commit cuando los cambios afecten al descubrimiento, las tareas o la semántica del estado.