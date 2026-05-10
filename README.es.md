# Kubeport

Una aplicación Frappe que convierte un bench de Frappe / ERPNext en un plano de control de Kubernetes.

Los operadores se conectan a clústeres, registran repositorios de Helm, declaran releases de Helm, aplican manifiestos de Kubernetes en bruto y orquestan el ciclo de vida de sites de Frappe (`bench new-site`, `migrate`, `backup`, `restore`, `drop-site`) — todo desde la misma interfaz Desk que ya utilizan, con los invariantes operacionales normalmente asociados a las herramientas de ingeniería de plataformas: descubrimiento de solo lectura, ejecución en trabajos en segundo plano, verificación contra el estado real y un bucle de reconciliación de deriva cada 5 minutos.

> Invariante definitorio: **el estado deseado vive en MariaDB a través de DocTypes de Frappe; el estado observado siempre se consulta en vivo desde el clúster.** El descubrimiento nunca persiste en la base de datos. Toda mutación del clúster se ejecuta fuera del hilo de la petición.

---

## Contexto del proyecto

Kubeport es el artefacto backend de un proyecto final (TFG, 2026). Se entrega junto con dos repositorios hermanos:

- **Landing page** — [`1DAW-victorjim551/lp-KubePort`](https://github.com/1DAW-victorjim551/lp-KubePort) (desplegada en [`1daw-victorjim551.github.io/lp-KubePort`](https://1daw-victorjim551.github.io/lp-KubePort/)), desarrollada por Víctor Jiménez.
- **Paraguas del proyecto** — [`EsderJ10/tfg`](https://github.com/EsderJ10/tfg): contenedor de desarrollo, notas de diseño, seguimiento de tareas.

Para el encuadre académico (problema, estado del arte, objetivos, resultados) consulta [`docs/thesis.md`](docs/thesis.md).

---

## Mapa de documentación

Empieza por el documento que se ajuste a lo que quieres hacer.

| Objetivo | Leer |
|---|---|
| Entender qué es Kubeport y por qué se construyó | [`docs/thesis.md`](docs/thesis.md) |
| Desplegar Kubeport para uso no relacionado con el desarrollo (topología, RBAC, dimensionamiento, monitorización, copia de seguridad del plano de control) | [`docs/deploy.md`](docs/deploy.md) |
| Aplicar los manifiestos RBAC dentro del clúster | [`deploy/rbac/README.md`](deploy/rbac/README.md) |
| Usar Kubeport de extremo a extremo (flujos de trabajo del operador) | [`docs/operator-guide.md`](docs/operator-guide.md) |
| Entender la arquitectura (diagramas C4, secuencias, invariantes) | [`docs/architecture.md`](docs/architecture.md) |
| Ver la superficie de capacidades actual, defensas de robustez y brechas abiertas | [`docs/control-plane-state.md`](docs/control-plane-state.md) |
| Ver los fallos tolerados, defensas y límites superiores de recuperación | [`docs/fault-model.md`](docs/fault-model.md) |
| Encontrar un módulo / DocType / API específico | [`docs/codebase-summary.md`](docs/codebase-summary.md) |
| Leer la evaluación empírica (funcional, fiabilidad, línea base, escalado) | [`docs/evaluation.md`](docs/evaluation.md) |
| Buscar una cita de la tesis en BibTeX | [`docs/references.bib`](docs/references.bib) |
| Ejecutar el conjunto de evaluación de extremo a extremo | [`eval/README.md`](eval/README.md) |
| Contribuir (configuración, lint, tests, convenciones de PR) | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| Historial de decisiones de arquitectura | [`CHANGELOG.md`](CHANGELOG.md) |
| Reportar un problema de seguridad | [`SECURITY.md`](SECURITY.md) |
| Ver el análisis de fronteras de confianza (STRIDE por frontera, mapeo de endpoints) | [`docs/threat-model.md`](docs/threat-model.md) |
| Invariantes y patrones detallados para contribuidores humanos o de IA | [`AGENTS.md`](AGENTS.md) |

---

## Capacidades

- **Conectividad con clústeres** — autenticación mediante kubeconfig, token de portador o cuenta de servicio dentro del clúster. Importación de kubeconfig desde el navegador con normalización automática del endpoint para entornos de desarrollo en contenedores.
- **Catálogo de charts de Helm** — registra repositorios y sincroniza el inventario de charts y versiones en segundo plano; `values.yaml` predeterminado en caché; actualización diaria.
- **Catálogo de imágenes de site** — imágenes públicas de Frappe / ERPNext en GHCR. Las filas curadas están ancladas por digest y se actualizan automáticamente mediante `.github/workflows/publish-site-image.yml` en cada etiqueta `v*`. Los operadores también pueden registrar sus propias imágenes.
- **Gestión de releases de Helm** — declara el estado deseado con alcance a `clúster/namespace/nombre_release`; despliegue, actualización, rollback y desinstalación idempotentes a través de trabajos en segundo plano; desglose en vivo de la preparación de cargas de trabajo con logs de pods, eventos y contexto de rollout.
- **Despliegue de manifiestos en bruto** — `Service Bundle` valida los manifiestos contra una lista permitida de 17 tipos de recursos integrados y los aplica / elimina mediante apply en el lado del servidor.
- **Ciclo de vida de sites de Frappe** — `Frappe Site` orquesta `bench new-site`, `bench migrate`, `bench backup`, `bench restore`, `bench drop-site` a través de Jobs de Kubernetes clonados desde un pod de workload bench en vivo. `Frappe Site Backup` es un DocType independiente para que los metadatos de copia de seguridad puedan sobrevivir a la fila del site origen.
- **Descubrimiento en vivo** — enumeración de solo lectura de releases de Helm y sites de Frappe en cualquier clúster registrado. El descubrimiento nunca persiste en MariaDB.
- **Reconciliación** — barrido programado cada 5 minutos que compara el estado deseado y el observado, recupera la deriva transitoria, finaliza las filas en vuelo mediante sondas de estado real y barre Jobs huérfanos.
- **Herramientas del operador** — `Kubernetes Command` para operaciones ad-hoc de Get / List / Delete contra una lista permitida; `Kubernetes Command Audit Log` para un historial de ejecuciones de solo adición.

Para el inventario completo de robustez consulta [`docs/control-plane-state.md`](docs/control-plane-state.md).

---

## Arquitectura de un vistazo

```
┌───────────────────────────────────────────────────────┐
│                    Frappe Desk                        │
│        (Formularios, Eventos en Tiempo Real,          │
│             Descubrimiento en Cliente)                │
├──────────────┬───────────────────┬────────────────────┤
│   Capa API   │   Capa DocType    │   Capa de Tareas   │
│  (consultas  │  (estado deseado  │(trabajos en segundo│
│ solo lectura)│    en MariaDB)    │ plano, estructuras |
│              |                   |      cluster)      |
├──────────────┴───────────────────┴────────────────────┤
│                  Capa de Utilidades                   │
│ (cliente K8s, wrapper CLI Helm, descubrimiento, obs.) │
├───────────────────────────────────────────────────────┤
│           Clúster Kubernetes (estado en vivo)         │
└───────────────────────────────────────────────────────┘
```

| Capa | Ruta | Responsabilidad |
|---|---|---|
| DocTypes | `kubeport/kubeport/doctype/` | Documentos de estado deseado respaldados por MariaDB |
| API | `kubeport/api/` | Endpoints públicos de solo lectura para formularios |
| Utilidades | `kubeport/utils/` | Helpers de integración sin estado para K8s y Helm |
| Tareas | `kubeport/tasks/` | Trabajos en segundo plano para todo el trabajo que muta el clúster |
| Tests | `kubeport/tests/`, `doctype/*/test_*.py` | Tests unitarios y de integración |
| Parches | `kubeport/patches/` | Migración de esquema y limpieza |

Para diagramas C4 y secuencias en tiempo de ejecución consulta [`docs/architecture.md`](docs/architecture.md).

---

## Instalación

### Requisitos previos

- Un [Frappe Bench](https://frappeframework.com/docs/user/en/bench) en funcionamiento (Frappe 16, MariaDB, Redis).
- Python 3.14+.
- [Helm 3](https://helm.sh/docs/intro/install/) en el `PATH` del host del bench.
- Acceso de red desde el host del bench (o pod) a un clúster de Kubernetes (kubeconfig, token de portador o cuenta de servicio dentro del clúster).

### Instalar

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app kubeport <repository-url> --branch main
bench install-app kubeport
bench --site <site> migrate
```

El primer `bench install-app` encola la sincronización del catálogo curado de imágenes de site en la cola `long`. El bucle de reconciliación de 5 minutos se registra automáticamente mediante `hooks.py`.

Para despliegues fuera del entorno de desarrollo (topología dentro o fuera del clúster, fragmento de empaquetado Helm de la imagen de bench, RBAC dentro del clúster, límites de recursos, monitorización y copia de seguridad del plano de control), sigue [`docs/deploy.md`](docs/deploy.md).

Para el flujo de trabajo con contenedor de desarrollo compatible consulta [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Primeros pasos

1. Abre el Desk y navega al espacio de trabajo **Kubeport Operations**.
2. **Conecta un clúster** → crea una fila `Kubernetes Cluster` en tu modo de autenticación preferido y ejecuta **Test Connection**.
3. **Registra un repositorio Helm** → crea una fila `Helm Repository`; el catálogo de charts se sincroniza en segundo plano.
4. **Despliega una release** → crea una fila `Helm Release`, elige un chart, edita los valores (opcionalmente elige una imagen de site para charts de ERPNext / Frappe) y haz clic en **Deploy**.
5. **Crea un site de Frappe** → desde una `Helm Release` de un bench ERPNext, crea una fila `Frappe Site` y haz clic en **Create Site**.

Las instrucciones paso a paso para cada flujo de trabajo están en [`docs/operator-guide.md`](docs/operator-guide.md), incluyendo copia de seguridad / restauración, cancelación, desinstalación forzada y el procedimiento manual de verificación para validación previa a la publicación.

---

## Desarrollo

### Estilo de código

| Aspecto | Regla | Herramienta |
|---|---|---|
| Formato Python | tabuladores, comillas dobles, líneas de 110 caracteres | `ruff format` |
| Linting Python | configuración en `pyproject.toml` del repositorio | `ruff` |
| Formato JS / CSS | valores predeterminados del repositorio | `prettier` |
| Linting JS | `.eslintrc` del repositorio | `eslint` |
| Anotaciones de tipo | obligatorias en todos los métodos `@frappe.whitelist()` | aplicado mediante `hooks.py` |

### Pre-commit

```bash
cd apps/kubeport
pre-commit install
```

### Tests

```bash
bench --site <site> run-tests --app kubeport
bench --site <site> run-tests --app kubeport --doctype "Helm Release"
```

CI ejecuta la misma suite más `ruff format --check` y `ruff check` en cada PR (`.github/workflows/ci.yml`). Para más detalles consulta [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Licencia

[MIT](LICENSE).