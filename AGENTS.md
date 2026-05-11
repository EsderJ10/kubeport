# Kubeport — Agent Instructions

Este archivo es la referencia autorizada para agentes de IA que trabajan en el repositorio Kubeport. Define el contexto del proyecto, los invariantes arquitectónicos, los patrones de implementación y las reglas de comportamiento que los agentes deben seguir.

---

## Project Context

Kubeport es una app de Frappe que actúa como un control plane de Kubernetes dentro de la interfaz de Frappe/ERPNext. Gestiona la conectividad del cluster, los Helm releases, los Kubernetes manifests sin procesar (mediante Service Bundles) y el aprovisionamiento de sitios Frappe.

**El invariante arquitectónico principal**: el desired state vive en MariaDB mediante DocTypes de Frappe; el observed state siempre se consulta en vivo desde el cluster. Nunca mezcles estas dos categorías.

## Stack and Tooling

| Attribute | Value |
|---|---|
| Type | App de Frappe para ERPNext/Frappe |
| Python | 3.14+ |
| Build backend | `flit` |
| Install | `bench get-app <url> --branch main` luego `bench install-app kubeport` |
| Dependencies | `kubernetes`, `urllib3`, `PyYAML` |

### Code Style

- **Indentation**: tabs (no espacios)
- **Quote style**: comillas dobles
- **Line length**: 110 caracteres
- **Python formatting**: `ruff format`
- **Python linting**: `ruff`
- **JS/CSS formatting**: `prettier`
- **JavaScript linting**: `eslint`

### Type Annotations

- `export_python_type_annotations = True` está habilitado en `hooks.py`
- Usa `frappe.types.DF` para type hints de campos DocType
- Todos los métodos API whitelisted deben tener type annotations

### Pre-commit

Se espera que Pre-commit esté instalado en un bench checkout:

```bash
cd apps/kubeport && pre-commit install
```

## Repository Rules

- Prefiere `rg` / `rg --files` para búsqueda y descubrimiento de archivos.
- Usa `apply_patch` para ediciones manuales cuando esté disponible.
- Nunca reviertas cambios del usuario a menos que se solicite explícitamente.
- No uses comandos destructivos de git (`git reset --hard`, `git checkout --`, etc.).
- Mantén las ediciones en ASCII a menos que el archivo ya use texto no ASCII.

---

## Architecture

### Layer Map

| Layer | Path | Responsibility |
|---|---|---|
| DocTypes (desired state) | `kubeport/kubeport/doctype/` | Documentos respaldados por MariaDB; uno por tipo de recurso |
| API endpoints | `kubeport/api/` | Consultas whitelisted de solo lectura que soportan formularios |
| K8s / Helm utilities | `kubeport/utils/` | Clientes y utilidades sin estado |
| Background tasks | `kubeport/tasks/` | Todas las operaciones que modifican el cluster |
| Scheduled jobs | `hooks.py` | Reconciliación (cada 5 min), sincronización diaria de repos |
| Tests | `kubeport/tests/`, `doctype/*/test_*.py` | Tests unitarios e integrados |
| Patches | `kubeport/patches/` | Migración de esquema y limpieza |

### DocTypes

| DocType | Purpose | Key Behavior |
|---|---|---|
| `Kubernetes Cluster` | Credenciales y conectividad del cluster | Multi-auth (kubeconfig, bearer token, in-cluster). Bypass TLS solo en desarrollo. Normalización del endpoint kubeconfig. Renderizado de discovery en vivo del lado del cliente. |
| `Helm Repository` | Configuración de repos Helm | Sincronización de charts en background con tokens por ejecución. Reconstrucción completa del inventario chart/version. Limpieza de charts obsoletos. |
| `Helm Chart` | Metadatos sincronizados de charts | Historial de versiones y valores por defecto en caché. Autonombrado como `{repository}/{chart_name}`. |
| `Helm Chart Version` | Registro individual de versión de chart | Tabla hija de `Helm Chart`. |
| `Helm Release` | Desired state de un Helm release | Identidad delimitada por `cluster/namespace/release_name`. Validación de valores YAML. Deploy/uninstall en background. |
| `Service Bundle` | Desired state de Kubernetes manifests sin procesar | Valida contra la allowlist de resource kinds soportados. Tokens de operación por ejecución. Apply/delete en background. |
| `Frappe Site` | Desired state de un sitio Frappe en un bench | Enlaza a un Helm Release (el bench). Envía Kubernetes Jobs para create/delete/migrate/backup/restore. Reconciliación basada en ground-truth. |
| `Frappe Site Backup` | Metadatos de archivos de backup de sitios | Filas independientes para backups almacenados en PVC. Los archivos viven en PVCs `kubeport-backups` locales al namespace y pueden sobrevivir al sitio original. |
| `Kubeport Site Image` | Imágenes runtime de Frappe curadas o registradas por el usuario | El repositorio debe ser un GHCR público (`ghcr.io/owner/image`). El digest, si existe, debe ser una referencia `sha256:`. Las filas curadas activas deben registrar el digest publicado. `is_default` está reservado para filas curadas activas. Las filas curadas no pueden eliminarse (solo marcarse como Deprecated). Las filas registradas por el usuario solo pueden eliminarse si ningún `Helm Release` las referencia. Las filas curadas se generan desde `kubeport/site_images/catalog.json` mediante la sincronización diaria. |
| `Kubeport Site Image App` | Hijo de `Kubeport Site Image` | Registra cada app Frappe/ERPNext/custom incluida en una Site Image con su URL de origen y ref. Bloqueado para edición salvo que el padre sea registrado por el usuario. |
| `Kubernetes Command` | Doctype de herramientas de operador para Get / List / Delete ad-hoc | Allowlist fija de resource kinds (lectura: 8 tipos; delete: solo `Pod`/`Job`/`ConfigMap`). Solo System Manager. Delete requiere `confirm_destructive` tipado y validado. Ejecución encolada en la cola `long`. |
| `Kubernetes Command Audit Log` | Log de ejecución append-only | Una fila por ejecución de `Kubernetes Command` (éxito o fallo). Desacoplado de la fila origen para que el historial sobreviva a su eliminación. Solo lectura para System Manager; nunca escrito desde la UI. |

### Scheduled Jobs

Declarados en `hooks.py`:

- `*/5 * * * *` → `kubeport.tasks.reconciliation.reconcile_all_releases` (detección de drift para Helm Releases, Service Bundles y Frappe Sites; también ejecuta el barrido de Jobs huérfanos)
- `*/5 * * * *` → `kubeport.tasks.reconciliation.reconcile_site_backups` (sondeo de Kubernetes Jobs de backup y comprobación de finalización en PVC)
- Diario → `kubeport.tasks.helm_tasks.sync_all_repos` (actualización del catálogo de charts)
- Diario → `kubeport.tasks.site_image_tasks.enqueue_sync_site_image_catalog` (reimporta `kubeport/site_images/catalog.json` en el doctype `Kubeport Site Image`, marcando filas curadas y reconciliando drift; también ejecuta `after_install` y `after_migrate`)

El catálogo se actualiza mediante CI: un push de tag `v*` a `.github/workflows/publish-site-image.yml` ejecuta `scripts/update_site_catalog.py` tras verificar el digest, reescribiendo `image_tag`, `image_digest`, `source_revision` y `apps_json_hash` de la fila curada correspondiente. El workflow no hace commit ni abre PR — sube el archivo reescrito como `site-image-catalog-<tag>` y muestra el diff en el resumen del run; el operador hace el commit mediante el flujo normal de revisión.

---

## Design Invariants

Estas reglas no son negociables. Cada cambio de código debe respetarlas.

### 1. Desired State vs. Observed State

- **DO**: Guarda la intención en campos DocType. Consulta el estado en vivo desde Kubernetes/Helm en tiempo de lectura.
- **DO**: Guarda metadatos de backups en MariaDB, pero mantén los archivos de backup fuera de MariaDB en el backend de almacenamiento configurado.
- **DO NOT**: Persistas estado descubierto del cluster en MariaDB. El discovery es solo lectura.

### 2. Async-First Execution

- **DO**: Enruta todas las operaciones que modifican el cluster (Helm install/upgrade/uninstall, apply/delete de Service Bundle, envío de Kubernetes Jobs de Frappe Site) mediante background jobs en la cola `long`.
- **DO NOT**: Hagas llamadas K8s/Helm que modifiquen el cluster desde el web thread.

### 3. Concurrency Safety

- **DO**: Usa tokens de operación/sync por ejecución. Revisa estado y token antes de actuar en workers. Usa actualizaciones dirigidas con `frappe.db.set_value` / `db_set`.
- **DO NOT**: Uses `doc.reload()` en workers cuando la concurrencia importa. Permite acciones duplicadas mientras un documento está `In Progress` o `Uninstalling` sin comprobaciones de token.

### 4. Discovery Is Read-Only

- **DO**: Devuelve resultados parciales cuando releases o pods individuales fallan. Distingue claramente categorías de error (sin pods, solo pods de infraestructura, pods de workload no ejecutándose, fallo en exec).
- **DO NOT**: Persistas benches, sitios o estado de pods descubiertos en la base de datos. El discovery es efímero.

### 5. Form Rendering

- **DO**: Prefiere llamadas async (`frappe.xcall` / `frappe.call`) más renderizado del lado del cliente para datos externos.
- **DO NOT**: Uses características del framework que carguen datos externos de Kubernetes durante `doc.onload` o la obtención del documento.

### 6. Cluster Access Scoping

- **DO**: Mantén todo acceso a Kubernetes limitado al documento del cluster objetivo. Construye clientes API con ámbito mediante `get_k8s_api_client(cluster_name)`.
- **DO NOT**: Uses configuración global del cliente Kubernetes ni compartas estado entre solicitudes.

### 7. Backup Archive Independence

- **DO**: Trata el ciclo de vida del archivo de backup como independiente del PVC del bench del sitio origen y de la fila `Frappe Site`.
- **DO NOT**: Borres o requieras la fila `Frappe Site` origen para restaurar desde un `Frappe Site Backup` en estado `Available` cuyo metadata de cluster/namespace/site coincida con el sitio destino.

---

## Implementation Patterns

### Adding a New Background Task

1. Crea la función de tarea en el módulo correspondiente dentro de `kubeport/tasks/`.
2. Acepta un identificador de documento y un parámetro `operation_token`.
3. Revisa el estado actual del documento y el token antes de ejecutar (protección contra jobs obsoletos).
4. Envuelve la operación del cluster en try/except. En fallo: actualiza estado, registra el error, publica un evento realtime.
5. En éxito: actualiza campos de estado con `db_set` o `frappe.db.set_value`, publica un evento realtime.
6. La tarea se encola desde el controlador DocType usando `frappe.enqueue(..., queue="long", enqueue_after_commit=True)`.

### Adding a New API Endpoint

1. Colócalo en `kubeport/api/` en el módulo adecuado.
2. Decóralo con `@frappe.whitelist()`.
3. Añade type annotations completas (requeridas por `require_type_annotated_api_methods = True`).
4. Manténlo de solo lectura — sin mutaciones del cluster desde endpoints API.
5. Maneja fallos con elegancia — devuelve resultados vacíos o payloads de error estructurados en lugar de lanzar excepciones.

### Adding a New DocType Field

1. Actualiza la definición JSON del DocType mediante el editor de DocTypes o manualmente.
2. Añade el campo al controlador Python si requiere validación o lógica de negocio.
3. Usa `frappe.types.DF` para type hints en la clase del controlador.
4. Si el campo afecta discovery, reconciliación o comportamiento de background tasks, actualiza la documentación relevante en el mismo cambio.

### Discovery Pod Selection

La selección de pods para discovery de sitios sigue esta prioridad:
1. Selector de etiquetas: `app.kubernetes.io/instance=<release_name>` (preferido)
2. Alternativa: escaneo del namespace buscando coincidencias por etiqueta `release` o prefijo del nombre del pod
3. Filtro: solo pods de workload Frappe (gunicorn, scheduler, workers — no mariadb, valkey)
4. Ranking: puntuación por fase Running, contenedores listos, etiqueta de componente

### Supported Resource Kinds (Service Bundle)

Service Bundle solo soporta una allowlist fija de resource kinds incorporados definida en `kubeport/utils/k8s_resources.py`. Los CRDs y recursos personalizados arbitrarios están fuera de alcance.

Allowlist actual: `Pod`, `Service`, `Deployment`, `ConfigMap`, `Secret`, `Namespace`, `Ingress`, `PersistentVolumeClaim`, `StatefulSet`, `DaemonSet`, `Job`, `CronJob`, `ServiceAccount`, `ClusterRole`, `ClusterRoleBinding`, `Role`, `RoleBinding`.

---

## Testing

### Running Tests

```bash
bench --site <site> run-tests --app kubeport --doctype <DocType>
```

### Test Conventions

- Las clases de test usan `IntegrationTestCase` por defecto. Usa `UnitTestCase` solo para lógica aislada y pura.
- Para comprobaciones locales de sintaxis cuando el entorno Bench no está disponible, prefiere validaciones específicas en lugar de ejecuciones completas.
- Al añadir un nuevo API o comportamiento de formulario, actualiza o añade tests cerca del módulo modificado.

### Current Coverage

Cobertura más fuerte: comportamiento de discovery, timeouts de API, transiciones de estado de reconciliación, validación de manifests, protecciones de concurrencia en Helm workers, patches de limpieza/migración.

Cobertura más débil: envío de Kubernetes Jobs de Frappe Site end-to-end, comportamiento de sincronización de `Helm Repository`, flujos de metadatos de `Helm Chart`, tests de integración amplios entre DocTypes.

---

## Documentation

- Mantén `README.md` alineado con las funcionalidades realmente entregadas, no con planes aspiracionales.
- Usa `docs/control-plane-state.md` para capacidades actuales, huecos abiertos y notas de robustez.
- Usa `docs/codebase-summary.md` para resúmenes de arquitectura a nivel de módulo.
- Usa `docs/architecture.md` §3 para las ocho propiedades numeradas de seguridad / liveness / eventual consistency que gobiernan el código, y `docs/fault-model.md` para el catálogo de fallos tolerados, defensas y límites de recuperación. Cualquier cambio que añada una defensa nueva, debilite una existente o modifique un límite de recuperación debe actualizar ambos archivos en el mismo commit.
- Cuando un cambio afecte discovery, comportamiento de background tasks o semántica del desired state, actualiza la documentación en el mismo commit.

---

## Common Pitfalls

Estos son errores específicos que deben evitarse:

| Pitfall | Why It's Wrong | Correct Approach |
|---|---|---|
| Persisting discovered sites/benches to MariaDB | Viola el invariante de discovery de solo lectura | Devuelve datos de discovery como API responses efímeras |
| Calling Helm CLI from the web thread | Bloquea la solicitud; puede agotar el tiempo | Encola en la cola `long` mediante `frappe.enqueue` |
| Using `doc.reload()` in a background worker | Crea race conditions con actualizaciones concurrentes | Usa `frappe.db.get_value` para lecturas dirigidas, `db_set` para escrituras |
| Assuming `helm status == deployed` means sites exist | Los pods de workload pueden estar Pending o CrashLooping | Comprueba la fase real del pod y la existencia del sitio por separado |
| Trusting K8s Job exit codes as ground truth | `bench new-site` puede fallar pese a haber tenido éxito | Verifica la existencia del sitio mediante exec antes de marcar como Failed |
| Mixing cluster/runtime failures with code bugs | Confunde el análisis y la depuración | Documenta si el fallo es por código o por el entorno/cluster |
| Hardcoding chart image tags or PVC names | Rompe al actualizar versiones del chart | Extrae dinámicamente desde un pod de referencia en vivo |
| Using `shell=True` in subprocess calls | Riesgo de seguridad, vector de inyección | Pasa siempre el comando como lista de strings |