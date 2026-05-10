# Contribuir a Kubeport

Kubeport es una aplicación Frappe. Las contribuciones siguen las convenciones estándar de aplicaciones Frappe más algunos invariantes específicos de este proyecto (consulta [`AGENTS.md`](AGENTS.md) para la lista completa).

---

## Entorno de desarrollo

Kubeport se desarrolla dentro de un Frappe Bench. El flujo de trabajo soportado es un contenedor de desarrollo, que proporciona Frappe v16, MariaDB, Redis, Helm y `kubectl` preinstalados.

Una configuración manual mínima tiene el siguiente aspecto:

```bash
# Dentro de un checkout existente de Frappe Bench
bench get-app kubeport <repository-url> --branch main
bench install-app kubeport
bench --site <site> migrate
```

Dependencias de ejecución requeridas en el host del bench:

- Python ≥ 3.14
- Helm 3 en `PATH`
- Un clúster de Kubernetes accesible (kubeconfig, token de portador o cuenta de servicio dentro del clúster) para cualquier funcionalidad que ejercite E/S real con el clúster

---

## Ramas y pull requests

- La rama predeterminada es `main`. Todos los cambios llegan mediante pull request contra `main`.
- Las ramas de funcionalidad deben usar un prefijo temático: `feat/...`, `fix/...`, `chore/...`, `refactor/...`, `docs/...`.
- Mantén los PRs revisables: un único cambio cohesionado por PR, con `CHANGELOG.md` actualizado en el mismo commit cuando el cambio sea arquitectónicamente significativo.

### Mensajes de commit

Los mensajes de commit siguen el estilo existente del proyecto: un resumen convencional en una sola línea, sin cuerpo, sin tráiler `Co-Authored-By`. Ejemplos del `git log`:

```
fix: integrate main and resolve CI lint and reconciliation test failures
refactor: remove obsolete migration patch tests
ci: enforce test job gating now that bench-in-CI is green
```

### Entradas del CHANGELOG

`CHANGELOG.md` es un **registro de decisiones de arquitectura**, no un fichero de notas de versión. Añade una entrada cuando el PR registre una decisión de diseño que valga la pena preservar (el enfoque elegido, las alternativas descartadas, el motivo). Las correcciones de bugs y refactorizaciones que no cambian la arquitectura no requieren entrada. El formato está documentado al principio de `CHANGELOG.md`.

---

## Estilo de código

| Aspecto | Regla | Herramienta |
|---|---|---|
| Sangría Python | tabuladores | `ruff format` |
| Comillas Python | dobles | `ruff format` |
| Longitud de línea Python | 110 caracteres | `ruff` |
| Versión objetivo Python | 3.14 | `pyproject.toml` (`target-version = "py314"`) |
| Formato JS / CSS | valores predeterminados de prettier | `prettier` |
| Linting JS | `.eslintrc` del repositorio | `eslint` |
| Anotaciones de tipo | obligatorias en todos los métodos `@frappe.whitelist()` | `require_type_annotated_api_methods = True` en `hooks.py` |
| Tipos de campos DocType | `frappe.types.DF` | `export_python_type_annotations = True` en `hooks.py` |

Pre-commit ejecuta los formateadores y linters automáticamente. Instálalo una vez por checkout:

```bash
cd apps/kubeport
pre-commit install
```

La versión de ruff en pre-commit está anclada en `.pre-commit-config.yaml`. El job de lint de CI (`.github/workflows/ci.yml`) usa la misma versión. Si actualizas una, actualiza la otra (y `docker-compose.lint.yml` — ver más abajo).

### Lint en contenedor (sin instalación en el host)

Si no quieres ruff en el host, usa la pila compose incluida — ancla la misma imagen y versión que CI:

```bash
make fmt         # formatear en local
make lint        # reportar hallazgos
make fix         # corregir automáticamente + formatear
make lint-check  # ejecución exacta en modo CI (format --check + check)
```

`make` delega en `docker compose -f docker-compose.lint.yml`. Los ficheros escritos por el contenedor pertenecen al UID de tu host. Si actualizas ruff, actualízalo en `.github/workflows/ci.yml`, `.pre-commit-config.yaml` **y** `docker-compose.lint.yml`.

Un flujo de trabajo complementario (`.github/workflows/lint-autofix.yml`) ejecuta `ruff check --fix` + `ruff format` en cada PR y hace commit del resultado de vuelta a la rama del PR, de modo que la deriva corregible mecánicamente nunca bloquea la comprobación `Lint`.

---

## Tests

Ejecuta la suite completa de Kubeport desde un bench en funcionamiento:

```bash
bench --site <site> run-tests --app kubeport
```

Ejecuta los tests de un único DocType:

```bash
bench --site <site> run-tests --app kubeport --doctype "Helm Release"
```

Cuando el entorno bench no esté disponible, prefiere tests unitarios focalizados y comprobaciones de sintaxis cerca del módulo modificado en lugar de una ejecución completa del bench.

### Convenciones de tests

- Usa `IntegrationTestCase` por defecto. Usa `UnitTestCase` solo para lógica pura y aislada.
- Cuando añadas o modifiques una API pública, añade un test cerca del módulo modificado.
- Para cambios que afecten al aprovisionamiento de Frappe Site, ejecuta el procedimiento manual de verificación documentado en el §10 de [`docs/operator-guide.md`](docs/operator-guide.md) y registra el resultado en la descripción del PR antes de fusionar.

### CI

Cada PR ejecuta `.github/workflows/ci.yml`:

- Job `lint`: `ruff format --check` + `ruff check` sobre todo el repositorio.
- Job `test`: arranca un bench Frappe v16 contra contenedores de servicio MariaDB 10.6 y Redis 7, instala `kubeport` y ejecuta `bench --site test_site run-tests --app kubeport`.

Ambos jobs deben pasar antes de fusionar.

---

## Invariantes arquitectónicos que debes respetar

Estos son innegociables. La lista completa está en [`AGENTS.md`](AGENTS.md). La versión corta:

1. **El estado deseado vive en MariaDB. El estado observado se consulta en vivo.** Las rutas de código de descubrimiento nunca deben escribir en MariaDB.
2. **Todas las mutaciones del clúster se ejecutan a través de `frappe.enqueue(..., queue="long")`.** Sin llamadas a `helm` ni a `kubernetes-client` que muten el clúster desde el hilo de la petición web.
3. **Los workers en segundo plano vuelven a comprobar el `operation_token` (o `sync_token`) del documento antes de cualquier escritura de estado.** Un worker obsoleto nunca debe sobreescribir una operación más reciente.
4. **Las escrituras de estado en los workers son dirigidas (`db_set` / `frappe.db.set_value`).** Nunca usar `doc.reload()` en un worker — compite con las actualizaciones concurrentes del formulario.
5. **Los datos externos del clúster los obtiene el formulario de forma asíncrona (`frappe.xcall`), no mediante `doc.onload`.**
6. **Los clientes de la API de K8s tienen alcance por clúster mediante `get_k8s_api_client(cluster_name)`.** Sin estado de cliente global compartido entre peticiones.
7. **Los archivos de copia de seguridad son independientes del site origen.** Una fila de backup en estado `Available` debe poder restaurarse incluso después de que la fila `Frappe Site` origen haya sido eliminada.

Un cambio que difumine cualquiera de estos es un cambio de diseño, no una corrección de bug — abre una discusión en el PR antes de fusionarlo.

---

## Documentación

Cuando un cambio afecte al descubrimiento, al comportamiento de tareas en segundo plano o a la semántica del estado deseado, actualiza el documento correspondiente **en el mismo commit**:

| Documento | Actualizar cuando |
|---|---|
| [`README.md`](README.md) | La superficie de funcionalidades visible para el usuario cambia (capacidades añadidas o eliminadas). |
| [`docs/architecture.md`](docs/architecture.md) | Se introduce una nueva capa, contenedor, secuencia o invariante. |
| [`docs/operator-guide.md`](docs/operator-guide.md) | Un flujo de trabajo visible para el operador cambia (nuevos campos, nuevos botones, nuevos estados del ciclo de vida). |
| [`docs/control-plane-state.md`](docs/control-plane-state.md) | La superficie de capacidades o las defensas de robustez cambian. |
| [`docs/codebase-summary.md`](docs/codebase-summary.md) | La responsabilidad o los límites de un módulo cambian. |
| [`AGENTS.md`](AGENTS.md) | Un invariante cambia — poco frecuente. |
| [`CHANGELOG.md`](CHANGELOG.md) | Cualquier decisión arquitectónicamente significativa. |

Para PRs exclusivamente de documentación, el job de CI `lint` sigue ejecutándose (el markdown no se comprueba hoy en día, pero los ficheros Python sí).

---

## Reportar bugs y proponer funcionalidades

Abre un issue con:

- Una reproducción clara (comandos, operaciones de DocType, comportamiento observado frente al esperado).
- El contexto del clúster relevante (versión de Kubernetes, versión del chart, modo de autenticación).
- Para copias de seguridad, sites o releases de Helm: el estado de la fila, `operation_token` y `operation_job_name` si aplica.

Para problemas de seguridad, sigue [`SECURITY.md`](SECURITY.md) — no abras un issue público.