# Resumen del código base

Referencia de arquitectura a nivel de módulo para el repositorio de Kubeport. Este documento describe qué hace cada módulo y cómo se relacionan entre sí. Para las capacidades actuales y las brechas abiertas, consulta [Estado del plano de control](control-plane-state.md).

---

## Estructura de alto nivel

```
kubeport/
├── api/              # Whitelisted read-only endpoints
│   ├── __init__.py        # Namespace lookup, kubeconfig parsing/extraction
│   ├── discovery.py       # Live release and site discovery
│   ├── observability.py   # Helm Release pod logs, events, rollout context
│   ├── dashboard.py       # Kubeport workspace dashboard data
│   ├── site_images.py     # DB-backed Kubeport Site Image catalog
│   └── site.py            # Frappe Site job log and backup listing support
├── utils/            # Stateless integration helpers
│   ├── k8s_client.py      # Scoped Kubernetes API client builder
│   ├── helm.py            # Helm CLI wrapper (subprocess, temp kubeconfig)
│   ├── discovery.py       # Read-only cluster and site discovery logic
│   ├── observability.py   # Pod / event / rollout helpers for diagnostics
│   ├── release_health.py  # Shared workload-readiness classifier
│   ├── k8s_resources.py   # Manifest parsing, CRUD, resource allowlist
│   └── constants.py       # Shared constants (resource kinds, labels)
├── tasks/            # Background jobs (all cluster-mutating work)
│   ├── helm_tasks.py             # Repo sync, release deploy/uninstall/rollback
│   ├── site_image_tasks.py       # Shipped site-image catalog sync
│   ├── service_bundle_tasks.py   # Manifest apply/delete
│   ├── site_tasks.py             # Frappe site lifecycle via K8s Jobs
│   ├── kubernetes_command_tasks.py  # Operator-tools execute path
│   └── reconciliation.py         # Scheduled drift detection + orphan-Job sweep
├── kubeport/doctype/  # Frappe DocType definitions and controllers
│   ├── kubernetes_cluster/
│   ├── helm_repository/
│   ├── helm_chart/
│   ├── helm_chart_version/
│   ├── helm_release/
│   ├── service_bundle/
│   ├── frappe_site/
│   ├── frappe_site_backup/
│   ├── kubeport_site_image/
│   ├── kubeport_site_image_app/
│   ├── kubernetes_command/
│   └── kubernetes_command_audit_log/
├── kubeport/workspace/kubeport_operations/  # Desk workspace (auto-installed)
├── kubeport/number_card/                    # Workspace dashboard cards (fixtures)
├── tests/            # Cross-module unit and integration tests
├── patches/          # Schema migration and cleanup
└── hooks.py          # App configuration, scheduled jobs
```

## Modelo de ejecución central

El código base impone una separación estricta entre estado deseado y estado observado:

- **El estado deseado** vive en MariaDB a través de DocTypes de Frappe.
- **El estado observado** proviene de consultas en vivo a Kubernetes y Helm.
- **Las operaciones mutadoras** se enrutan a través de trabajos en segundo plano en la cola `long`.
- **La reconciliación** compara el estado deseado y el observado de forma programada y actualiza los campos de estado.

Esta es la decisión arquitectónica definitoria del repositorio.

---

## DocTypes

### Kubernetes Cluster

Almacena la configuración de conectividad del clúster y sirve como ancla para todo el acceso en vivo a Kubernetes.

- Valida los campos específicos del método de autenticación (contenido del kubeconfig, token de portador + certificado CA, o dentro del clúster).
- Soporta pruebas de conexión contra la API real de Kubernetes.
- Proporciona un bypass de verificación TLS exclusivo para desarrollo para clústeres locales con kubeconfig y con token de portador.
- Requiere un certificado CA para la autenticación por token de portador a menos que el bypass TLS exclusivo para desarrollo esté explícitamente habilitado.
- Normaliza los endpoints de servidor del kubeconfig importado cuando el fichero origen apunta a direcciones exclusivamente locales (`0.0.0.0`, `127.0.0.1`, `localhost`) inalcanzables desde el contenedor de la aplicación.
- El formulario en el lado del cliente renderiza tablas de descubrimiento en vivo mediante llamadas asíncronas a la API.

### Helm Repository

Almacena la configuración del repositorio de Helm y los metadatos de sincronización.

- Registra automáticamente los repositorios con Helm al crearlos.
- Sincroniza los charts en trabajos en segundo plano con tokens de sincronización por ejecución para evitar que workers obsoletos sobreescriban sincronizaciones más recientes.
- Reconstruye el inventario completo de charts y versiones a partir del índice actual del repositorio y poda las filas de charts obsoletos que han desaparecido del origen.
- Realiza el seguimiento del estado de sincronización y la marca temporal de la última sincronización.
- Soporta `include_patterns` opcionales (globs separados por comas) para filtrar qué charts se sincronizan.

### Helm Chart

Almacena los metadatos de los charts respaldados por el repositorio y el historial de versiones.

- Nombrado automáticamente como `{repository}/{chart_name}`.
- Mantiene una tabla hija de registros `Helm Chart Version`.
- Cachea el contenido predeterminado de `values.yaml`.
- Limpia los valores predeterminados cacheados cuando la última versión del chart cambia durante la sincronización.
- Construye referencias a charts para los comandos de la CLI de Helm.

### Helm Release

Almacena el estado deseado para el despliegue de una carga de trabajo gestionada por Helm.

- La identidad está acotada a `cluster/namespace/release_name`, coincidiendo con el alcance real de la release de Helm.
- Valida el contenido de los valores YAML.
- Rechaza combinaciones inseguras de StorageClass `local-path` junto con modo de acceso `ReadWriteMany`.
- Prepara valores iniciales desplegables para charts de ERPNext / Frappe inyectando la StorageClass predeterminada del worker del clúster, con los modos de acceso `local-path` ajustados a `ReadWriteOnce`.
- Puede enlazarse con una `Kubeport Site Image` para charts oficiales de bench de ERPNext / Frappe. La imagen del catálogo seleccionada es estado deseado y se renderiza en los valores de Helm durante el despliegue como `image.repository`, `image.tag` consciente del digest, e `image.pullPolicy=IfNotPresent`.
- Bloquea overrides manuales conflictivos de `values.image.*` mientras haya una Site Image seleccionada, de modo que la intención de imagen permanezca inequívoca. El hash de la especificación deseada incluye la fila de imagen seleccionada y el digest para la detección de cambios pendientes.
- Empaqueta por defecto un MariaDB de Bitnami hermano por release para los charts de Frappe. La instalación / actualización encola `_ensure_bundled_mariadb_release` (fija `oci://registry-1.docker.io/bitnamicharts/mariadb@25.1.1`) y renderiza `dbHost: <release-name>-mariadb` en los valores del padre; la desinstalación ejecuta `_uninstall_bundled_mariadb_release`. La casilla `use_external_database` lo desactiva (no se instala hermano; el operador posee `dbHost`).
- Renderiza ingress estructurado (`ingress_enabled`, `ingress_hostname`, `ingress_class_name`, `ingress_cluster_issuer`) en `ingress.*` para los charts de Frappe. El TLS de cert-manager es opt-in mediante `ingress_cluster_issuer`. `render_ingress_values` invoca a `_has_advanced_ingress_override` para decidir si el YAML en bruto del usuario es la vía de escape (multi-host, tipos de path personalizados, anotaciones personalizadas más allá de `cert-manager.io/cluster-issuer`, o claves no incluidas en `{enabled, className, hosts, annotations, tls}`) o un bloque simple que será reemplazado por el renderizado estructurado. Los cuatro campos del formulario participan en el hash de la especificación, de modo que alternar el ingress invierte `pending_changes`.
- Encola el despliegue (`helm upgrade --install`), el rollback y la desinstalación a través de trabajos en segundo plano.
- Realiza el seguimiento del estado del ciclo de vida de la release (`Draft`, `In Progress`, `Deployed`, `Degraded`, `Failed`, `Uninstalling`).
- Realiza el seguimiento del hash de la especificación deseada, el hash de la especificación aplicada por última vez, la versión del chart aplicada por última vez, el tipo de operación y la hora de inicio de la operación. `pending_changes` se establece cuando el estado deseado guardado difiere de la última aplicación exitosa.
- Usa tokens de operación por ejecución más re-comprobaciones de estado, de modo que los workers obsoletos de despliegue / desinstalación no puedan sobreescribir una operación más reciente.
- Bloquea la desinstalación normal mientras filas enlazadas de `Frappe Site` puedan seguir siendo dueñas de estado en el lado del bench; la desinstalación forzada requiere confirmación escrita.
- Expone el historial en vivo de Helm y encola el rollback como operación de cola larga. Un rollback exitoso actualiza los valores / versión del chart deseados guardados para coincidir con la revisión en vivo seleccionada.
- Permite la eliminación directa de filas solo desde `Draft`; las filas `Failed` pueden seguir siendo dueñas de recursos del clúster y deben limpiarse mediante desinstalación.
- Clasifica el estado de la release con la política compartida de preparación de cargas de trabajo en `utils/release_health.py`.

### Service Bundle

Almacena el estado deseado para manifiestos de Kubernetes en bruto.

- Valida el contenido del manifiesto contra la lista permitida de tipos de recursos soportados.
- Encola la aplicación (server-side apply) y la eliminación a través de trabajos en segundo plano.
- Usa tokens de operación por ejecución y un estado `Deleting` distintivo para garantizar la seguridad ante concurrencia.
- Realiza el seguimiento del estado del ciclo de vida del bundle.

### Frappe Site

Almacena el estado deseado para un site de Frappe que se creará sobre un bench en ejecución.

- Enlaza con una `Helm Release` (el bench objetivo).
- Almacena el nombre del site, la contraseña de administrador, las credenciales de base de datos y las aplicaciones a instalar.
- Solo `mariadb` es un `db_type` soportado; el campo está fijado a ese único valor.
- Los trabajos en segundo plano descubren un pod de bench en ejecución, extraen dinámicamente su imagen de contenedor y el montaje del PVC de sites, y envían un Job de Kubernetes que ejecuta el comando `bench` apropiado. Un único constructor `_build_op_job_manifest` se reutiliza entre creación, eliminación y migración; solo difieren el comando y las variables de entorno por operación.
- `_preflight_db_topology` se ejecuta de forma síncrona en **Create Site** para rechazar el clic cuando el cableado de la BD obviamente no va a funcionar. Para releases con MariaDB empaquetado requiere que `<release-name>-mariadb` exista en el namespace; para releases con BD externa requiere que el operador haya proporcionado credenciales root en la fila. Un pre-flight verde es necesario pero no suficiente — el worker sigue ejecutando la cadena completa de resolución — pero un pre-flight rojo expone el fallo en el hilo de la interfaz en lugar de dejar que el Job se envíe y falle segundos después.
- La reconciliación verifica la existencia del site mediante descubrimiento basado en exec (comprueba `site_config.json`) en lugar de confiar en los códigos de salida del Job — evita falsos negativos cuando `--install-app` dispara advertencias no fatales.
- Usa los campos `operation_token` por ejecución (control de concurrencia) y `operation_job_name` / `operation_job_token` (identidad de reconciliación) a lo largo de las operaciones del ciclo de vida del site.

### Frappe Site Backup

Almacena metadatos para archivos de copia de seguridad creados a partir de un Frappe Site.

- Realiza el seguimiento del estado del ciclo de vida de la copia de seguridad (`Pending`, `In Progress`, `Available`, `Restoring`, `Failed`).
- Persiste únicamente metadatos: marca temporal, tamaño, backend / ruta de almacenamiento, identidad del Job de operación y metadatos del site origen.
- Los archivos viven en PVCs `kubeport-backups` locales al namespace y no se almacenan en MariaDB.
- Los registros de copia de seguridad son independientes, de modo que los metadatos de recuperación pueden sobrevivir a la fila original de `Frappe Site`.

### Kubeport Site Image

Almacena imágenes públicas de runtime de Frappe / ERPNext en GHCR disponibles para selección desde Helm Release — tanto imágenes curadas como registradas por el usuario.

- Las filas del catálogo son metadatos de producto en MariaDB. Las filas curadas se siembran desde `kubeport/site_images/catalog.json`; las filas registradas por el usuario se crean en la interfaz.
- Realiza el seguimiento del repositorio, etiqueta de release, digest, mayor de Frappe, versión de ERPNext, revisión origen, hash de apps.json, estado, selección por defecto y un flag `is_curated` (`1` para filas curadas pertenecientes a la sincronización del catálogo, `0` para filas registradas por el usuario). El campo integrado `owner` de Frappe registra quién creó cada fila.
- Las filas curadas activas deben incluir un digest de imagen publicado; las filas de usuario pueden crearse sin digest, pero entonces despliegan por etiqueta.
- Incluye filas hijas para las aplicaciones empaquetadas; la cuadrícula es editable en filas de usuario y de solo lectura en filas curadas.
- La sincronización se ejecuta como trabajo en segundo plano en la cola larga en instalación / migrate y en el scheduler diario. La sincronización solo escribe filas con `is_curated=1` y omite las colisiones de repository:tag con filas de usuario (registradas como advertencia), de modo que los datos del usuario nunca se sobreescriben.
- `is_default` está reservado para filas curadas. La eliminación se bloquea cuando una Helm Release enlaza con la fila, y las filas curadas no pueden eliminarse (usa `Deprecated`).

### Kubernetes Command

Un doctype deliberado de herramientas del operador para operaciones ad-hoc Get / List / Delete contra una lista permitida fija de tipos con namespace.

- El acceso de lectura cubre 8 tipos: `Pod`, `Job`, `Secret`, `ConfigMap`, `Service`, `Deployment`, `StatefulSet`, `PersistentVolumeClaim`.
- La eliminación está restringida solo a `Pod`, `Job`, `ConfigMap` — los cambios destructivos sobre el cuarteto peligroso (Secret, PVC, Deployment, StatefulSet) deben pasar por sus controladores dedicados (`Helm Release`, `Service Bundle`, `Frappe Site`).
- Solo System Manager. La eliminación requiere `confirm_destructive` aplicado tanto en validate (guardado del formulario) como en execute (worker en segundo plano).
- La ejecución se encola en la cola `long` mediante `kubeport.tasks.kubernetes_command_tasks`.

### Kubernetes Command Audit Log

Fila de auditoría de solo adición escrita en cada ejecución de `Kubernetes Command` (éxito o fallo).

- System Manager tiene acceso de lectura; el doctype nunca se escribe desde la interfaz.
- Desacoplado de la fila origen — el rastro de auditoría sobrevive a la eliminación de la fila `Kubernetes Command`.

---

## Capa API

### `kubeport.api.__init__`

Endpoints de apoyo al formulario para la interacción con el clúster:

- `get_cluster_namespaces(cluster_name)` — listado en vivo de namespaces para los desplegables del formulario
- `parse_kubeconfig_contexts(kubeconfig_content)` — parsea el kubeconfig subido y devuelve metadatos de contexto con información de normalización
- `extract_kubeconfig_context(kubeconfig_content, context_name)` — extrae un kubeconfig mínimo y autocontenido para un único contexto

Incluye normalización de endpoints del kubeconfig: detecta direcciones del servidor de API exclusivamente locales (`0.0.0.0`, `127.0.0.1`, `localhost`) y las reemplaza con la IP del gateway predeterminado del contenedor para configuraciones de desarrollo en contenedores.

### `kubeport.api.discovery`

Endpoint de descubrimiento en vivo del clúster:

- `get_cluster_discovery(cluster_name)` — devuelve un payload `{ benches, sites, errors }`
- Descubre releases de Helm a nivel de todo el clúster, luego ejecuta el descubrimiento de sites contra las releases identificadas como benches de Frappe (actualmente solo el chart `erpnext`)
- Los fallos a nivel de release se aíslan y reportan como errores parciales — una única release que falla no bloquea el descubrimiento para otras releases

### `kubeport.api.site`

Soporte del formulario de Frappe Site:

- `get_site_job_logs(site_docname)` — obtiene stdout del pod del Job de operación actual para mostrarlo en el formulario; usa el campo `operation_job_name` para localizar el pod
- `list_site_backups(site_docname)` — devuelve las filas de copia de seguridad para el formulario del site, las más recientes primero
- `get_site_backup_job_logs(backup_docname)` — obtiene stdout de los Jobs de copia de seguridad / restauración
- Devuelve logs vacíos de forma elegante cuando el pod del Job aún no está disponible o ha sido limpiado por TTL

### `kubeport.api.site_images`

Endpoint de solo lectura del catálogo de imágenes de site:

- `list_site_images(include_deprecated=False)` — devuelve las filas activas del catálogo y los metadatos de las aplicaciones incluidas para el renderizado del formulario de Helm Release

### `kubeport.api.dashboard`

Respalda el dashboard del workspace de Kubeport Operations. Contadores de solo lectura y resúmenes de operaciones obsoletas; sin E/S al clúster — todo se obtiene de DocTypes de estado deseado.

### `kubeport.api.observability`

Endpoints de solo lectura para el drilldown de Helm Release:

- Resuelve la identidad del clúster desde la fila `Helm Release` tras las comprobaciones de System Manager y de lectura del documento.
- Valida el recurso solicitado contra el manifiesto en vivo de Helm antes de leer logs, eventos o contexto de rollout.
- Devuelve la lista de pods con propiedad demostrada y logs limitados para un pod seleccionado, además de payloads de eventos y rollout acotados a la release sin persistir el estado observado.
- Usa envoltorios explícitos `{rows, error}` para los fallos de búsqueda de eventos y rollout, de modo que el formulario pueda renderizar degradación a nivel de panel.

---

## Capa de utilidades

### `k8s_client.py`

Construye clientes API de Kubernetes con alcance a partir de documentos de clúster. Fundamental para todos los flujos que se dirigen a K8s.

- Soporta tres modos de autenticación: kubeconfig, token de portador, dentro del clúster.
- Evita mutar el estado global del cliente de Kubernetes — cada llamada produce un cliente aislado.
- Gestiona el material CA del token de portador y la configuración de bypass TLS.

### `helm.py`

Wrapper sin estado alrededor del binario de Helm 3.

- Cada función escribe un fichero kubeconfig temporal, ejecuta `helm` vía `subprocess.run` (como una lista, nunca con `shell=True`), y limpia en un bloque `finally`.
- Parseo de salida JSON cuando Helm lo soporta; YAML en bruto para `helm show values`.
- La autenticación dentro del clúster produce `None` para la ruta del kubeconfig, permitiendo que Helm autodetecte las credenciales del pod.
- Los workers concurrentes nunca comparten estado del kubeconfig gracias al aislamiento de ficheros temporales por llamada.

### `release_health.py`

Salud observada de solo lectura para Helm Releases.

- Lee `helm get manifest` para enumerar los recursos propiedad de Helm y comprueba la preparación para los tipos de recursos integrados (`Deployment`, `StatefulSet`, `DaemonSet`, `Pod`, `Job`, `PersistentVolumeClaim`, `Service`, `Ingress`).
- Convierte los recursos ausentes o con error de API en filas no listas, de modo que el formulario pueda mostrar resultados parciales en lugar de hacer fallar toda la lectura de salud.
- Proporciona el clasificador compartido usado por los workers de despliegue y la reconciliación: `deployed` más todas las cargas de trabajo listas es `Deployed`; `deployed` más no listas / error de probe es `Degraded`; los estados de Helm pending o non-deployed son `Failed`.
- Mantiene las filas observadas por recurso como efímeras; solo los metadatos de ciclo de vida / estado (`status`, `helm_revision`, `helm_status_detail` breve, hashes de especificación y marcadores de operación) se persisten en la fila de Helm Release.

### `observability.py`

Helpers de Kubernetes de solo lectura para diagnósticos de Helm Release.

- Resuelve los pods para los recursos `Deployment`, `StatefulSet`, `DaemonSet` y `Pod` independiente, usando referencias de propietario y selectores del controlador.
- Obtiene los logs de un pod seleccionado por petición con límites de tail en el lado del servidor y un tope de tamaño de respuesta.
- Normaliza eventos de Kubernetes acotados y contexto de rollout de cargas de trabajo para el renderizado del formulario.

### `discovery.py`

Lógica de descubrimiento de solo lectura del clúster y de sites. Contiene la mayor parte de la implementación del hito de robustez.

- Normalización de releases: separa nombre de chart y versión, identifica los charts de bench de Frappe.
- El descubrimiento anota si una release de Helm ya está siendo seguida y expone la adopción explícita; no crea filas durante el descubrimiento de solo lectura.
- Selección de pods: búsqueda primero por etiqueta con fallback a escaneo de namespace. El filtrado exclusivo de cargas de trabajo excluye pods de infraestructura (mariadb, valkey). Los pods candidatos se rankean por fase, preparación y etiqueta de componente.
- Listado de sites: exec en un pod seleccionado y enumera los directorios que contienen `site_config.json`.

### `k8s_resources.py`

Parseo, validación y CRUD de manifiestos para los recursos de Service Bundle.

- Valida el contenido del manifiesto gestionado contra una lista permitida fija de tipos de recursos integrados.
- Soporta server-side apply (`application/apply-patch+yaml`) con fallback a `create_from_dict` en 404.
- Soporta la eliminación con éxito silencioso en 404 (recurso ya inexistente).
- Proporciona `check_resources_exist` para las comprobaciones de salud de reconciliación.

---

## Capa de tareas

### `helm_tasks.py`

Operaciones de repositorio y release de Helm:

- `add_and_sync_repo` — registra el repositorio con Helm, sincroniza el inventario de charts
- `sync_repo_charts` — re-registra el repositorio, refresca el índice, sincroniza los charts con comprobación de token por ejecución
- `sync_all_repos` — punto de entrada del scheduler diario, encola `sync_repo_charts` para cada repositorio
- `install_or_upgrade_release` — `helm upgrade --install` idempotente con guardas de trabajos obsoletos por token de operación, clasificación de salud compartida y reescritura de la especificación aplicada por última vez. Invoca primero `_ensure_bundled_mariadb_release` cuando el chart coincide con `is_frappe_site_chart()` y `use_external_database` no está marcado.
- `rollback_release` — `helm rollback` con guarda de trabajos obsoletos; un rollback exitoso actualiza los valores / versión del chart deseados guardados a la revisión en vivo seleccionada
- `uninstall_release` — `helm uninstall` con guarda de trabajos obsoletos; el "release not found" de Helm se trata como limpieza exitosa y devuelve la fila a `Draft`, limpiando los metadatos de la última aplicación. Invoca `_uninstall_bundled_mariadb_release` cuando el padre tenía MariaDB empaquetado.
- `_ensure_bundled_mariadb_release` / `_uninstall_bundled_mariadb_release` — instala / desinstala la release hermana de MariaDB de Bitnami `<parent>-mariadb` (chart `oci://registry-1.docker.io/bitnamicharts/mariadb` fijado a `_BUNDLED_MARIADB_CHART_VERSION = "25.1.1"`). Kubeport no inventa un fragmento de valores para los usuarios — el helper renderiza un YAML de valores mínimo que fija el nombre del secret hermano de Bitnami, de modo que el chart padre pueda resolver `<release>-mariadb` y `mariadb-root-password` por la convención de Bitnami.

La sincronización de charts reconstruye el inventario completo de versiones a partir de `helm search repo --versions`, agrupa por nombre de chart, deduplica versiones, poda los charts que han desaparecido del origen, y limpia los valores predeterminados cacheados cuando cambia la última versión.

### `service_bundle_tasks.py`

Ciclo de vida de manifiestos en bruto:

- `apply_service_bundle` — itera los objetos del manifiesto e invoca `apply_resource` para cada uno
- `delete_service_bundle` — itera los objetos del manifiesto e invoca `delete_resource` para cada uno
- Ambos usan tokens de operación por ejecución y publican eventos en tiempo real.

### `site_tasks.py`

Operaciones del ciclo de vida del site de Frappe a través de Jobs de Kubernetes:

- `create_site_task` — descubre un pod de bench en vivo, extrae su imagen y montaje del PVC de sites de forma dinámica, construye un manifiesto de Job ejecutando `bench new-site` y lo envía.
- `delete_site_task` — construye un Job ejecutando `bench drop-site --no-backup --force` con credenciales root de la BD inyectadas.
- `migrate_site_task` — construye un Job ejecutando `bench migrate`; no se requiere Secret de credenciales (bench lee desde `site_config.json`).
- `backup_site_task` — asegura un PVC RWX `kubeport-backups` local al namespace, lo monta en un Job de bench clonado, ejecuta `bench backup --with-files` y escribe un archivo tar más marcadores de metadatos.
- `restore_site_task` — monta el mismo PVC de copia de seguridad, extrae el archivo seleccionado y ejecuta `bench restore --force` con archivos de ficheros públicos / privados cuando estén presentes. Los fallos en el envío de la restauración o en la reconciliación dejan la fila de copia de seguridad como `Available` con detalle del fallo, mientras que el site objetivo pasa a `Failed`.
- Las operaciones de site comparten un único constructor `_build_op_job_manifest`; solo difieren la lista de comandos, las variables de entorno por operación y el montaje del PVC de copia de seguridad.
- Los nombres de Job se derivan del nombre del site y del token de operación para garantizar unicidad y trazabilidad.
- Las credenciales fluyen a través de Secrets de Kubernetes por Job (nunca como valores de entorno en texto plano), referenciados como propietarios del Job para garbage collection automática.
- Guarda de concurrencia de dos tokens por operación: `operation_token` (rotado en cada nueva acción) y `operation_job_token` (instantánea capturada cuando se envía el Job, comprobada por la reconciliación antes de cualquier escritura de estado).

### `kubernetes_command_tasks.py`

Ruta de ejecución de las herramientas del operador usada por `Kubernetes Command`:

- `execute_kubernetes_command(command_name, operation_token)` — re-comprueba el estado del documento y el flag `confirm_destructive` antes de ejecutar la operación Get / List / Delete solicitada contra los tipos de la lista permitida.
- Escribe una fila `Kubernetes Command Audit Log` por cada ejecución (éxito o fallo), incluyendo el clúster, namespace, tipo, nombre, operación, estado de salida y operador.

### `reconciliation.py`

Detección de deriva programada que se ejecuta cada 5 minutos:

- **Helm Releases**: consulta `helm status` para todas las releases `Deployed` / `Degraded`, luego aplica el clasificador compartido de manifiesto / preparación. Puede recuperar filas `Degraded` a `Deployed`, marcar cargas de trabajo no listas como `Degraded` y marcar los estados de Helm pending / non-deployed como `Failed`. También recupera operaciones obsoletas en estado `In Progress` / `Uninstalling` tras 30 minutos.
- **Service Bundles**: comprueba la existencia de recursos mediante `check_resources_exist`. Marca `Degraded` cuando faltan recursos.
- **Frappe Sites y Backups**: hace polling de `BatchV1Api.read_namespaced_job()` para todas las filas de site en vuelo (`In Progress`, `Deleting`, `Migrating`) y filas de copia de seguridad (`In Progress`, `Restoring`). Verifica la verdad sobre el terreno mediante sonda de bench basada en exec antes de marcar transiciones de site / restauración. Los mensajes de detalle de fallo son específicos de la operación ("bench new-site failed", "bench drop-site failed", "bench migrate failed", "bench backup failed", "bench restore failed"). Recurre a la sonda de bench en la expiración del TTL del Job cuando es posible. El barrido de Jobs huérfanos se ejecuta en cada tick.
- **Copias de seguridad programadas + retención**: tras la pasada de finalización en vuelo, `_run_scheduled_backups` encola una copia de seguridad por cada site `Active` cuyo cron `backup_schedule` esté vencido (calculado a partir de `backup_schedule_last_run`, avanzando el marcador antes de encolar para que los ticks concurrentes observen que el hueco ya está ocupado). `_prune_backup_retention` luego envía a la papelera las filas `Available` que superen los topes `backup_retention_count` y `backup_retention_days` por site, compartiendo la ruta de limpieza de archivo existente `FrappeSiteBackup.on_trash`.

---

## Frontend / Comportamiento del formulario

Los scripts JavaScript de formulario en las carpetas de DocType siguen un patrón asíncrono por defecto:

- `Kubernetes Cluster` renderiza tablas de descubrimiento en vivo en el formulario mediante `frappe.xcall`.
- `Helm Release` y `Service Bundle` escuchan eventos en tiempo real de actualización de estado y refrescan los indicadores. La salud de Helm Release se carga de forma asíncrona como `{rows, error}` y las filas no listas abren un panel de observabilidad persistente dentro del formulario que expone logs de pods (con un selector de pod cuando hay más de un pod respaldando el recurso), eventos de Kubernetes acotados y contexto de rollout sin bloquear la carga del documento. El panel de observabilidad protege los callbacks asíncronos para que las respuestas obsoletas no puedan sobreescribir la selección activa de recurso, vista o pod.
- `Frappe Site` muestra indicadores de estado, disparadores de ciclo de vida, filas de copia de seguridad, acciones de restauración, y obtiene logs de Job de forma asíncrona.
- Las sugerencias de namespace se obtienen en vivo desde el clúster seleccionado.
- Los formularios nunca intentan persistir estado descubierto externamente durante el fetch del documento.

---

## Cobertura de tests

| Área | Nivel de cobertura | Notas |
|---|---|---|
| Comportamiento del descubrimiento | Fuerte | Selección de pods, búsqueda con fallback, categorías de error |
| Uso de timeouts de la API | Fuerte | Aserciones de timeout en las peticiones |
| Transiciones de estado de la reconciliación | Fuerte | Incluyendo la verificación de verdad sobre el terreno de Frappe Site para las tres operaciones |
| Validación de manifiestos | Fuerte | Lista permitida de tipos de recursos, validación de campos |
| Guardas de concurrencia de workers de Helm | Fuerte | Comprobaciones de token, re-comprobaciones de estado |
| Parches de limpieza / migración | Fuerte | Eliminación de DocTypes heredados, migración de datos |
| Simulación del ciclo de vida de Frappe Site | Fuerte | Escenarios mockeados de extremo a extremo: create→active, cancelación en vuelo, fail→delete→fila eliminada, recuperación de falso negativo en migrate, supersesión concurrente |
| Copia de seguridad / restauración de Frappe Site | Bueno | Guardas de filas de copia de seguridad, construcción de PVC / Job, finalización de reconciliación, confirmación de restauración |
| Hooks y planificación de Helm | Bueno | Declaraciones de eventos del scheduler |
| Sincronización de extremo a extremo de Helm Repository | Débil | Necesita test de integración |
| Flujos de metadatos de Helm Chart | Débil | Necesita test de integración |
| Integración entre DocTypes | Débil | Necesita tests de flujo de trabajo más amplios |

---

## Parches y migración

La capa de parches documenta una transición arquitectónica:

- **Pre-model-sync**: `migrate_kubernetes_manifests_to_service_bundles.py` — migra datos del DocType heredado `Kubernetes Manifest` a `Service Bundle`.
- **Post-model-sync**: `cleanup_legacy_kubeport_doctypes.py` — elimina las tablas de DocType obsoletas. `rename_helm_release_docnames.py` — migra los nombres de documento de Helm Release al esquema de identidad `cluster/namespace/release_name`. `rename_frappe_site_job_fields.py` — renombra `creation_job_name` → `operation_job_name` y `creation_job_token` → `operation_job_token` para reflejar que estos campos siguen la operación actual independientemente de su tipo (create, delete, migrate).
