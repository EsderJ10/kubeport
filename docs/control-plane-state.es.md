# Estado del plano de control

## Resumen ejecutivo

Kubeport opera como un plano de control de Kubernetes real: el estado deseado se almacena en DocTypes de Frappe, mientras que el estado en vivo del clúster se consulta bajo demanda. La implementación puede conectarse a clústeres, descubrir releases en vivo y sites de Frappe, desplegar releases de Helm, aplicar manifiestos en bruto, aprovisionar sites de Frappe mediante Jobs de Kubernetes y reconciliar el estado persistido contra el clúster con una planificación de 5 minutos.

El hito actual es la **robustez** — hacer que el descubrimiento, la ejecución en segundo plano y la reconciliación sean fiables bajo condiciones de clúster imperfectas.

---

## Capacidades

### Conectividad con el clúster

- Tres modos de autenticación: kubeconfig, token de portador, cuenta de servicio dentro del clúster.
- Pruebas de conexión contra la API en vivo de Kubernetes.
- Bypass de verificación TLS exclusivo para desarrollo en clústeres con kubeconfig y token de portador.
- La autenticación por token de portador es estricta por defecto: requiere un certificado CA salvo que se habilite el bypass TLS exclusivo para desarrollo.
- Descubrimiento de namespaces en vivo reutilizado por los formularios que necesitan listas de namespaces del clúster.
- Subida de kubeconfig desde el navegador con análisis de contextos, extracción y normalización del endpoint para configuraciones de desarrollo en contenedores (convierte `0.0.0.0` / `127.0.0.1` / `localhost` a la IP del gateway del contenedor).

### Catálogo de charts de Helm

- Registro de repositorios de Helm y sincronización de charts en segundo plano.
- Reconstrucción completa del inventario de charts / versiones a partir de `helm search repo --versions`.
- Limpieza de charts obsoletos cuando los charts desaparecen aguas arriba.
- Tokens de sincronización por ejecución que impiden que workers obsoletos sobreescriban sincronizaciones más recientes.
- `include_patterns` opcional (globs separados por comas) para filtrar los charts sincronizados.
- `values.yaml` predeterminado por chart en caché, invalidado cuando cambia la versión más reciente.

### Catálogo de imágenes de site

- Kubeport incluye un catálogo `Kubeport Site Image` respaldado por la base de datos y sembrado desde `kubeport/site_images/catalog.json`.
- Las filas del catálogo son metadatos deseados / del producto, no estado observado del clúster: repositorio, tag de la release, digest, versión mayor de Frappe, versión de ERPNext, hash de apps, revisión de la fuente, estado por defecto / deprecado y apps incluidas (solo para visualización).
- Cada fila lleva una bandera `is_curated`. Las filas curadas (`1`) son propiedad de la sincronización diaria del catálogo; las filas registradas por el usuario (`0`) persisten de forma independiente. La sincronización solo escribe filas curadas y omite las entradas que colisionan con un repositorio:tag registrado por el usuario (registrando una advertencia), de modo que las imágenes registradas por el usuario nunca se sobreescriben.
- Los valores por defecto (`is_default=1`) están reservados para filas curadas; las filas de usuario pueden seleccionarse en Helm Release pero no pueden marcarse como por defecto.
- Las filas curadas activas deben registrar el digest de GHCR publicado. La fila `0.0.1-frappe16` incluida está deprecada hasta que exista una imagen pública real en GHCR y se registre su digest.
- El campo nativo de Frappe `owner` registra quién creó cada fila; no se mantiene un campo paralelo "origen / usuario".
- La eliminación se bloquea cuando un `Kubeport Site Image` está enlazado a una `Helm Release`, y las filas curadas no pueden eliminarse en absoluto (márcalas como `Deprecated` en su lugar).
- La sincronización del catálogo se ejecuta a través de una tarea en la cola `long` en la instalación / migración y mediante el planificador diario.
- Las imágenes públicas de GHCR son el ámbito de registro de la v1; no se expone gestión de `imagePullSecret` en la interfaz. Frappe v16 es la versión mayor soportada en la v1. La inyección de valores Helm asume charts de estilo ERPNext (`image.repository`, `image.tag`, `image.pullPolicy`).
- Kubeport no ejecuta builds de Docker ni almacena PATs de GHCR. La imagen de site la construye y publica GitHub Actions usando `GITHUB_TOKEN`, ajustes de SBOM / procedencia y atestaciones de registro.

### Gestión de releases de Helm

- Estado deseado de la release con alcance a `cluster/namespace/release_name`.
- El campo opcional `site_image` enlaza una release de bench ERPNext / Frappe con una imagen de runtime. Los workers de despliegue renderizan la fila del catálogo seleccionada en los valores de Helm como `image.repository`, `image.tag` e `image.pullPolicy=IfNotPresent`; cuando hay `image_digest` registrado, `image.tag` se renderiza como `tag@sha256:...` para que Kubernetes obtenga el digest exacto de la imagen publicada a través de la plantilla `repository:tag` existente del chart.
- El YAML manual sigue disponible, pero las entradas conflictivas `values.image.repository`, `values.image.tag` o `values.image.pullPolicy` se rechazan mientras `site_image` esté establecido.
- Para charts de estilo ERPNext / Frappe, Kubeport prepara valores iniciales desplegables descubriendo la StorageClass por defecto del clúster (la anotada con `storageclass.kubernetes.io/is-default-class: "true"`) cuando los valores del usuario no especifican `persistence.worker.storageClass`. Las storage classes proporcionadas por el usuario siempre prevalecen. Cuando la clase descubierta es `local-path` (u otro aprovisionador RWO-only conocido), Kubeport renderiza `accessModes: ["ReadWriteOnce"]` cuando el modo RWX por defecto del chart no se planificaría sobre ese backend. El descubrimiento es de solo lectura — el resultado no se persiste en MariaDB. Si no existe la anotación de clase por defecto y el usuario no ha establecido la clave, la ruta de carga / despliegue falla pronto con un mensaje accionable en lugar de propagar el error de plantilla `required` del chart de ERPNext.
- **MariaDB integrada por defecto para charts de Frappe.** Los charts de Frappe / ERPNext se publican con `dbHost` vacío, por lo que una release recién desplegada no puede crear sites hasta que el operador conecte una MariaDB. Kubeport cierra esa brecha: cuando el chart coincide con `is_frappe_site_chart()`, el worker de instalación / upgrade también instala el chart MariaDB de Bitnami aguas arriba (anclado: `oci://registry-1.docker.io/bitnamicharts/mariadb` en la versión `25.1.1`) como release hermana llamada `<release-name>-mariadb` en el mismo namespace, y renderiza `dbHost: <release-name>-mariadb` en los valores de la release padre para que el chart resuelva al Service hermano. Desinstalar la padre desinstala la hermana. La casilla **Use External Database** del formulario Helm Release permite optar por no usar este flujo — cuando se marca, no se instala una release hermana y el operador debe proporcionar `dbHost` por su cuenta a través del YAML `Values` en bruto y proporcionar credenciales root en cada `Frappe Site`. El flujo integrado es también la base de `Frappe Site._preflight_db_topology`, una búsqueda síncrona de Service / Secret que se niega a encolar un Job `bench new-site` cuando el camino feliz de MariaDB integrada está obviamente roto (no hay Service `<release-name>-mariadb` en el namespace).
- **Campos de ingress estructurados para charts de Frappe.** El formulario Helm Release expone cuatro campos — `ingress_enabled`, `ingress_hostname`, `ingress_class_name`, `ingress_cluster_issuer` — que renderizan los valores `ingress.*` del chart cuando están habilitados. TLS es opt-in mediante cert-manager: cuando se establece `ingress_cluster_issuer`, Kubeport emite la anotación `cert-manager.io/cluster-issuer` y un bloque `tls` que referencia un secreto por release `<release-name>-tls`. El renderizado está condicionado a `is_frappe_site_chart()`. Los campos estructurados son autoritativos para casos simples, pero el YAML en bruto del usuario prevalece cuando el bloque `ingress:` es **avanzado**, clasificado por `_has_advanced_ingress_override()`: más de un host, cualquier ruta distinta de `/ ImplementationSpecific`, anotaciones más allá de `cert-manager.io/cluster-issuer`, o cualquier clave de nivel superior fuera de `{enabled, className, hosts, annotations, tls}`. Los bloques `ingress` vacíos o por defecto del chart caen en el renderizado estructurado. Los cuatro campos del formulario participan en `calculate_release_spec_hash`, de modo que alternar ingress sin tocar `values` aún cambia `pending_changes`.
- **Sugerencias de descubrimiento de ingress de solo lectura.** Mientras se edita una Helm Release, el formulario rellena sugerencias no persistidas para `ingress_class_name` (la `IngressClass` por defecto del clúster o la única detectada) y `ingress_cluster_issuer` (un `ClusterIssuer` listo de cert-manager). Cuando un controlador de ingress de tipo LoadBalancer tiene una IP externa y no hay hostname establecido, el formulario propone `<release-name>.<ip>.nip.io` para clústeres locales / de desarrollo sin una zona DNS real. Las sugerencias se obtienen en vivo, nunca se persisten, y los campos correspondientes permanecen editables cuando no se detecta nada.
- Los hashes de cambios pendientes incluyen la Site Image seleccionada y el digest, por lo que los cambios de digest se tratan como cambios desplegables del estado deseado.
- Despliegue idempotente mediante `helm upgrade --install` en trabajos en segundo plano.
- Desinstalación mediante `helm uninstall` en trabajos en segundo plano.
- Las filas de release siguen el hash de la spec deseada frente al hash de la última spec aplicada para que los operadores puedan ver los cambios pendientes guardados antes de la próxima instalación / upgrade.
- Los tokens de operación por ejecución y las re-comprobaciones de estado impiden que workers obsoletos de despliegue / desinstalación escriban después de que una operación más reciente tome el relevo.
- Las operaciones de Helm en vuelo registran el tipo de operación y el momento de inicio. La reconciliación recupera filas obsoletas en estado `In Progress` / `Uninstalling` después de 30 minutos comprobando el estado y la preparación en vivo de Helm.
- La eliminación directa de filas solo se permite desde `Draft`; las filas `Failed` se tratan como potencialmente propietarias de recursos y deben desinstalarse primero. Si Helm informa de que la release ya no existe durante la desinstalación, Kubeport trata la limpieza como exitosa y devuelve la fila a `Draft`.
- La desinstalación es consciente de dependencias: las filas `Frappe Site` enlazadas en estados activos / en vuelo bloquean la desinstalación normal, con una ruta tipada de desinstalación forzada para invalidación explícita del operador.
- La salud post-despliegue y de reconciliación combina el estado de runtime de Helm con un recorrido de preparación de los manifiestos renderizados (`Deployment`, `StatefulSet`, `DaemonSet`, `Pod`, `Job`, `PersistentVolumeClaim`, `Service`, `Ingress`). `deployed` más todos los recursos comprobados listos se convierte en `Deployed`; `deployed` más recursos no listos o fallo en la sonda de preparación se convierte en `Degraded`; los estados de Helm pendientes / no desplegados se convierten en `Failed`.
- El formulario Helm Release expone el desglose de preparación de cargas de trabajo como estado observado de solo lectura. Las filas por recurso no se persisten.
- Cada fila de preparación no lista abre un panel de observabilidad dentro del formulario con tres sub-vistas: logs de pods (lista de pods con alcance al recurso, con un pod seleccionado obtenido por petición, acotado por el número de líneas de tail y el tamaño de respuesta), eventos de Kubernetes con alcance y contexto de rollout de Deployment / StatefulSet / DaemonSet. El panel es de solo lectura, efímero, restringido a System Manager más acceso de lectura al documento, e ignora respuestas asíncronas obsoletas cuando los operadores cambian de recurso, vista o pod.
- El formulario Helm Release expone el historial en vivo de la release y encola el rollback como operación en segundo plano. Un rollback exitoso actualiza los valores deseados / la versión del chart a la revisión en vivo seleccionada.
- Ciclo de vida de estados: `Draft` → `In Progress` → `Deployed` / `Degraded` / `Failed` → `Uninstalling` → `Draft`.
- Los eventos en tiempo real disparan la recarga del formulario en los cambios de estado.

### Service Bundle (manifiestos en bruto)

- Manifiestos de Kubernetes en bruto validados contra una lista permitida fija de tipos de recursos (17 tipos integrados).
- Server-side apply (idempotente) y eliminación en trabajos en segundo plano.
- Tokens de operación por ejecución y estado `Deleting` para seguridad ante concurrencia.
- La reconciliación comprueba la existencia de los recursos y marca los recursos ausentes como `Degraded`.

### Aprovisionamiento de Frappe Site

- El DocType `Frappe Site` enlaza con una Helm Release (el bench objetivo).
- Los trabajos en segundo plano ejecutan las operaciones del ciclo de vida del site (`bench new-site`, `bench drop-site`, `bench migrate`, `bench backup`, `bench restore`) enviando un Job de Kubernetes cuya plantilla de pod se clona desde un pod de workload bench en vivo (imagen, montaje del PVC de sites, env, campos a nivel de pod). Un único builder `_build_op_job_manifest` se reutiliza en todas las operaciones; solo difieren el comando bench y el env por operación.
- Las operaciones del ciclo de vida del site se ejecutan a través de un único orquestador `_run_site_op`: el comportamiento por operación se proporciona mediante una pequeña dataclass `SiteOpConfig` más callables planos (`build_command`, `build_env`, `build_creds_secret`). Esta es la única fuente de verdad para el pipeline de apply, la re-comprobación del token tras el apply y la limpieza de excepciones.
- La reconciliación verifica la existencia real del site en el bench (mediante descubrimiento basado en exec comprobando `site_config.json` y `bench list-apps`) en lugar de fiarse de los códigos de salida del Job — evita falsos negativos. La sonda es de tres estados (`exists` / `missing` / `unknown`); los fallos transitorios de bench-exec devuelven `unknown` y difieren la transición de estado al siguiente tick en lugar de marcar el site como `Failed`.
- **Estados del ciclo de vida**: `Draft → In Progress → Active | Failed`, más `Active → Migrating → Active | Failed` y `Active|Failed → Deleting → [doc eliminado] | Failed`. El backup reutiliza el estado `In Progress` — `Active → In Progress → Active | Failed` mientras un Job de backup se ejecuta contra el site — y un reintento tras una creación fallida reutiliza la flecha original como `Failed → In Progress`. La cancelación rota el token de operación, marca la fila como `Failed` y hace un mejor esfuerzo por eliminar el Job. Cancelar `Migrating` está protegido por confirmación porque matar `bench migrate` puede dejar los cambios de esquema de MariaDB aplicados a medias. El conjunto completo de flechas observables del site está enumerado en `kubeport/tests/test_property_fsm_frappe_site.py::DOCUMENTED_SITE_ARROWS` y verificado contra secuencias aleatorias de reglas por el test FSM basado en propiedades allí.
- **Eliminación (`bench drop-site`)** se ejecuta con `--no-backup --force`. Ante una sonda de ausencia confirmada, la reconciliación elimina la propia fila `Frappe Site` mediante `frappe.delete_doc`. Ante una sonda de aún-presente (el Job de drop-site se ejecutó pero el bench aún tiene el site), la fila aterriza en `Failed` con los logs del Job en `status_detail` para que el operador pueda reintentar.
- **Migración (`bench migrate`)** no necesita credenciales — bench las lee de `site_config.json`. La reconciliación ejecuta la misma sonda funcional usada para la creación; si el código de salida del Job es distinto de cero pero el site sigue siendo funcional (falso negativo), la fila se recupera a `Active`.
- **Backup / restauración** usa filas independientes `Frappe Site Backup`. Los Jobs de backup ejecutan `bench backup --with-files`, empaquetan los ficheros generados en un archivo tar y lo almacenan en un PVC RWX `kubeport-backups` local al namespace. Los Jobs de restauración extraen el archivo y ejecutan `bench restore --force`; la reconciliación trata una sonda funcional del bench como éxito incluso si el Job de restauración sale con código distinto de cero. Un intento de restauración fallido marca el `Frappe Site` objetivo como `Failed` pero devuelve la fila de backup a `Available` con detalle del fallo, porque el archivo sigue siendo utilizable para una restauración posterior.
- El ciclo de vida del archivo de backup es independiente del PVC del site origen y del ciclo de vida de la fila. Las filas de backup disponibles pueden sobrevivir a la fila `Frappe Site` original y conservar los metadatos necesarios para la recuperación.
- **Verdad de terreno del backup mediante sonda PVC.** La reconciliación no finaliza un backup como `Available` basándose únicamente en el código de salida del Job o en los metadatos de stdout. Un Pod de sonda de corta duración `busybox` monta el PVC `kubeport-backups` y lee el sidecar `<archive>.size` que el script de backup del bench escribe solo en un éxito completamente persistido. La sonda se consulta en dos lugares: (a) la ruta de recuperación de Job desaparecido, sustituyendo una heurística rota de `size_bytes` que siempre marcaba como `Failed` los Jobs envejecidos, y (b) la verificación post-éxito, defendiendo contra la ventana estrecha en la que `tar` salió con cero pero el inodo se perdió antes de que la reconciliación lo leyera. `unknown` defiere al siguiente tick.
- **La cancelación se propaga en cascada a los backups en vuelo.** `cancel_site` y `Frappe Site.on_trash` rotan no solo el `operation_token` del site padre sino que también fuerzan el fallo de cada fila `Frappe Site Backup` enlazada en `Pending` / `In Progress` / `Restoring`, rotando su `operation_token` independiente, publicando un evento en tiempo real y encolando la limpieza del clúster. Sin esto, las filas de backup permanecerían en vuelo para siempre y `_has_in_flight_backup` bloquearía todos los backups futuros para ese site.
- **Los backups fallidos limpian los ficheros de archivo.** `Frappe Site Backup.on_trash` encola la eliminación del archivo siempre que `storage_path` esté marcado, independientemente del estado. Un backup que escribió parcialmente un archivo y luego fue marcado como Failed solía dejar el fichero huérfano en el PVC; ahora trasear la fila lo elimina.
- **Los Jobs de ciclo de vida auto-gestionados son explícitos.** Los Jobs de eliminación de archivos llevan `kubeport.io/operation=archive-delete`. El barrido de huérfanos reconoce esta etiqueta y los omite, de modo que la auto-limpieza ya no compite con la ventana de gracia del barrido.
- **Backups programados + retención.** `Frappe Site` lleva un `backup_schedule` opcional (cron de cinco campos) y dos límites de retención (`backup_retention_count`, `backup_retention_days`). El tick de reconciliación de backups (`reconcile_site_backups`, cada 5 min) calcula el siguiente disparo cron desde `backup_schedule_last_run` (con fallback a `creation`); cuando el siguiente hueco está en el pasado avanza el marcador a `now()` y luego encola un único backup mediante el mismo helper `_enqueue_backup` usado por el botón **Backup Now**. Un periodo largo de inactividad dispara exactamente un backup de recuperación por hueco vencido, no una avalancha, porque el avance del marcador acota la base de `get_next` de croniter en el siguiente tick. La poda por retención se ejecuta tras la pasada de programación y solo tira a la papelera filas con estado `Available`; las filas `Failed` (diagnósticas) y las filas `In Progress` / `Restoring` (ya protegidas por `FrappeSiteBackup.on_trash`) nunca se trasean automáticamente. El hook `on_trash` existente propaga la eliminación al archivo del PVC, de modo que la retención comparte su ruta de limpieza con la papelera manual.
- **Las credenciales fluyen a través de Secrets de Kubernetes, nunca como variables de entorno en texto plano.** Create-site construye un Secret `{job_name}-creds` con `ADMIN_PASSWORD` (y `DB_ROOT_PASSWORD` cuando el usuario eligió el campo en texto plano). Drop-site construye el mismo Secret pero solo con `DB_ROOT_PASSWORD` (sin admin implicado). Migrate no necesita Secret en absoluto. Todos los Secrets tienen owner-reference a su Job, por lo que se recolectan junto con la limpieza del TTL del Job. El Job lee los valores mediante `secretKeyRef`.
- Solo se soporta MariaDB como backend de base de datos. El campo `db_type` está fijado a `mariadb`; la fontanería de postgres se ha eliminado intencionadamente — no hay un parche de compatibilidad hacia adelante ni una ruta de interfaz para seleccionarlo.
- Soporta tanto la contraseña root de la base de datos directa como referencias a Secrets de Kubernetes.
- Tokens de operación por ejecución para seguridad ante concurrencia, reutilizados en las tres operaciones.
- **`on_trash` rechaza la eliminación directa de filas `Active`** para evitar dejar huérfano el site real en el PVC del bench; el operador debe pasar primero por Delete Site. También rechaza las filas `Migrating` para que los operadores deban usar la ruta de cancelación con confirmación destructiva. Para cualquier otra fila con `operation_job_name`, `on_trash` rota el token de operación y hace un mejor esfuerzo por eliminar el Job de K8s con `propagation_policy="Background"` (lo que también limpia el Secret de credenciales mediante GC por owner-reference).
- Al expirar el TTL del Job antes de que la reconciliación lea el estado final, la reconciliación recurre a la misma sonda de verdad de terreno del bench usada para la rama de Job-fallido, transicionando la fila apropiadamente o difiriendo en `unknown`.
- Cada Job de operación lleva `activeDeadlineSeconds` (30 min por defecto) para que un pod atascado en `ImagePullBackOff` o incapaz de alcanzar la DB acabe pasando a `failed` en lugar de dejar la fila en un estado en vuelo para siempre.
- La reconciliación valida la etiqueta `kubeport.io/frappe-site` de cada Job antes de confiar en su estado, defendiendo contra colisiones de hash y valores obsoletos de `operation_job_name`.
- **Barrido de Jobs huérfanos**: cada tick de reconciliación lista los Jobs gestionados por Kubeport en cada par cluster / namespace que tenga alguna fila `Frappe Site` o `Frappe Site Backup` y elimina los Jobs de site / backup no referenciados por el `operation_job_name` de ninguna fila. Los Jobs más jóvenes que un tick de reconciliación se omiten para que el barrido no pueda competir con un worker que ha aplicado un Job pero aún no lo ha registrado. El barrido es agnóstico a la operación — recoge igualmente Jobs huérfanos de creación, eliminación, migración, backup y restauración.
- Los nombres de site se validan a etiquetas estilo hostname (alfanuméricos en minúscula más `.`, `-`, `_`, empezando / terminando en alfanumérico) para que sean seguros para el docname `{bench_release}/{site_name}`, el slug del Job de K8s y el entorno del bench.

### Descubrimiento en vivo

- Descubrimiento de releases de Helm con alcance al clúster y de solo lectura mediante `kubeport.api.discovery.get_cluster_discovery`.
- Adopción explícita de una release de Helm descubierta mediante `adopt_helm_release`; el descubrimiento anota si una release ya está siendo seguida pero nunca crea filas sin la acción Track del usuario.
- Descubrimiento de sites de Frappe con alcance a la release y de solo lectura mediante exec de pod.
- Payload estable: `{ benches, sites, errors }`.
- El descubrimiento nunca persiste resultados en MariaDB.

### Reconciliación

- Programada cada 5 minutos mediante `hooks.py`.
- Releases de Helm: combina `helm status` con la preparación de cargas de trabajo de `helm get manifest`; recupera filas `Degraded` a `Deployed` cuando las cargas de trabajo se vuelven listas y marca los estados de Helm pendientes / no desplegados como `Failed`.
- Service Bundles: comprobación de existencia de recursos vía la API de K8s.
- Frappe Sites y Backups: sondeo del estado del Job con verificación de verdad de terreno del site para create / delete / migrate / restore y una sonda sidecar `<archive>.size` del lado del PVC para la finalización del backup (recuperación de Job desaparecido y verificación post-éxito).

### Herramientas del operador

- **Kubernetes Command** es un doctype deliberadamente de herramientas del operador para Get / List / Delete ad-hoc contra una lista permitida fija de tipos con namespace. El acceso de lectura abarca 8 tipos (Pod, Job, Secret, ConfigMap, Service, Deployment, StatefulSet, PVC); el Delete está restringido a `Pod`, `Job`, `ConfigMap` únicamente — los cambios destructivos sobre el cuarteto peligroso (Secret, PVC, Deployment, StatefulSet) deben pasar por los controladores adecuados (Helm Release, Service Bundle, Frappe Site).
- Solo System Manager. Delete requiere `confirm_destructive` aplicado tanto en validate (guardado del formulario) como en execute (worker en segundo plano).
- **Kubernetes Command Audit Log** es un doctype de solo adición escrito en cada ejecución (éxito o fallo). System Manager tiene acceso de lectura; nunca se escribe desde la interfaz. Desacoplado de la fila origen para que el rastro de auditoría sobreviva a la eliminación de filas.

---

## Propiedades de robustez

El código se defiende activamente contra condiciones imperfectas del clúster:

| Propiedad | Implementación |
|---|---|
| El descubrimiento permanece de solo lectura | Ningún estado descubierto se persiste en MariaDB |
| Los datos parciales siguen siendo útiles | Los fallos a nivel de release están aislados; las releases con éxito se siguen devolviendo |
| El fallo en runtime se reporta, no se oculta | Payloads de error estructurados con alcance y contexto |
| Los pods de infraestructura no se confunden con cargas de trabajo | Los pods de MariaDB y Valkey se excluyen del descubrimiento de sites |
| Las cargas de trabajo pendientes no se malinterpretan | Tratadas como problema de clúster / runtime, no como motivo para inferir estado |
| Los workers obsoletos se detienen | Tokens por ejecución + re-comprobaciones de estado antes de actuar |
| Las filas de Helm atascadas pueden recuperarse | La reconciliación de operaciones obsoletas comprueba el estado en vivo de Helm tras 30 minutos |
| Las filas de Helm fallidas no pueden dejar recursos huérfanos silenciosamente | La eliminación directa está bloqueada fuera de `Draft`; la desinstalación es la ruta de limpieza |
| Los sites de bench protegen su release padre | La desinstalación normal de Helm está bloqueada mientras las filas `Frappe Site` enlazadas puedan seguir siendo propietarias de estado del lado del bench |
| La salud de la release de Helm tiene una sola política | Los workers de despliegue y la reconciliación comparten el mismo clasificador de runtime / preparación |
| La preparación de almacenamiento / red es visible | PVC, Service / endpoints, Ingress y eventos de advertencia participan en la salud de la release de Helm |
| La selección de pods es resiliente | Primero por etiqueta con fallback de escaneo del namespace, clasificado por estabilidad |
| No se confía ciegamente en los códigos de salida de los Jobs | Verificación de verdad de terreno mediante comprobación de existencia del site basada en exec |
| La reconciliación puede recuperar | Los documentos pasan de `Degraded` de vuelta a `Deployed` cuando el estado en vivo se normaliza |
| Los fallos transitorios de la sonda no marcan los sites como fallidos | La sonda del site devuelve `unknown` ante errores de exec; la transición de estado se difiere |
| Los Jobs de ciclo de vida colgados se recolectan | `activeDeadlineSeconds` en cada Job de create / delete / migrate |
| Los Jobs huérfanos se barren | Barrido basado en etiquetas en cada tick de reconciliación (agnóstico a la operación) |
| El barrido de huérfanos no compite con los workers | Los Jobs más jóvenes que el tick de reconciliación se excluyen del barrido |
| La identidad del Job se valida | La etiqueta `kubeport.io/frappe-site` se comprueba antes de cualquier escritura de estado |
| Las filas Active no pueden quedar huérfanas silenciosamente | `on_trash` rechaza la eliminación directa de filas `Active` de Frappe Site |
| Los metadatos del backup sobreviven a la eliminación del origen | `Frappe Site Backup` es un DocType independiente y almacena los metadatos de cluster / namespace / site origen |
| Los archivos de backup están desacoplados de los PVCs de site | Los archivos aterrizan en PVCs `kubeport-backups` locales al namespace, no en el PVC de sites del bench |
| La cancelación destructiva está protegida | `cancel_site` requiere `confirm_destructive=True` para `Migrating`; la interfaz requiere escribir `CANCEL` |
| El código de salida de drop-site no se confía ciegamente | La sonda del bench es la fuente de verdad para "el site realmente desaparecido" |
| Los mensajes de fallo del Job son específicos de la operación | `_extract_job_failure_detail` recibe un `operation_label` para que los logs nombren claramente qué comando `bench` falló (`bench new-site`, `bench drop-site`, `bench migrate`) |

---

## Brechas abiertas

### Descubrimiento y observabilidad

- El descubrimiento es un payload de interfaz, no un modelo de estado observado más rico. Los formularios de Helm Release
  y Frappe Site comparten la misma superficie de preparación de cargas de trabajo con desgloses por recurso para logs de pods, eventos
  de Kubernetes y contexto de rollout (el formulario del site acota a la release de bench que lo aloja). El streaming
  de logs en tiempo real y una línea temporal de eventos a nivel de clúster permanecen fuera de alcance.
- El descubrimiento de bench soportado es intencionadamente estrecho: solo releases del chart oficial `erpnext`. Ampliarlo a otras variantes de chart requiere un diseño deliberado.
- Los datos de descubrimiento no se enlazan de vuelta a los documentos `Helm Release` persistidos más allá de coincidir por nombres y namespaces.

### Profundidad de la salud

- La salud de la release de Helm cubre la preparación integrada para `Deployment`, `StatefulSet`, `DaemonSet`, `Pod`, `Job`, `PersistentVolumeClaim`, `Service` e `Ingress`, y muestra filas parciales por recurso en el formulario.
- La salud de Helm aún no inspecciona el historial almacenado de logs / eventos, la presión de almacenamiento más allá del binding del PVC,
  la salud HTTP a nivel de aplicación, ni la salud específica de CRDs.
- La salud de Service Bundle solo comprueba la existencia de recursos.

### Cobertura de plataforma

- Service Bundle solo soporta una lista permitida fija de tipos de recursos integrados (17 tipos). Los CRDs y los recursos personalizados arbitrarios están fuera de alcance.
- El rollback de Helm, el historial y una vista previa de diff de solo lectura (renderiza el manifiesto deseado con `helm template` y lo compara contra `helm get manifest`) están disponibles.

### Ciclo de vida del site

- `Frappe Site` cubre creación, eliminación, migración, backup, restauración, backups programados y retención por site. El almacenamiento en object-store, la restauración entre clústeres y la restauración a un nombre de site distinto permanecen fuera de alcance.
- Los sites descubiertos no se enlazan automáticamente a documentos `Frappe Site`.
- Los benches respaldados por Postgres no están soportados. El campo `db_type` está bloqueado a `mariadb` y se han eliminado las rutas de código de postgres.

### Profundidad de los tests

- Cobertura fuerte: descubrimiento, reconciliación, validación de manifiestos, guardas de concurrencia, parches de limpieza, orquestación compartida de operaciones de Frappe Site (apply de Secret+Job, attach de ownerRef, rollback ante fallo), manejo de errores de la tarea de cancelación, comportamiento de la rama de reconciliación de delete / migrate directos, escenarios completos de simulación del ciclo de vida (create→active, cancelar en vuelo, fail→delete→fila-eliminada, recuperación ante falso negativo de migrate, supersesión concurrente), guardas de backup / restauración del site y finalización del Job, ciclo de vida de la release de Helm (obsolescencia de token de install / upgrade / rollback / uninstall, detección de site bloqueante, desinstalación forzada, recuperación de operación obsoleta), supersesión de sincronización de Helm Repository y reconstrucción del inventario de charts, y preparación de cargas de trabajo por tipo para los ocho tipos soportados con attachment de eventos de advertencia.
- Cobertura débil: tests de integración cross-DocType más amplios.

- `docs/deploy.md` cubre la topología dentro vs. fuera del clúster, el fragmento de empaquetado Helm de la imagen de bench, la matriz RBAC verb-resource, las líneas base de límites de recursos, los endpoints de métricas internas y el procedimiento de copia de seguridad del plano de control. Los manifiestos RBAC de mínimo privilegio kustomizados viven en `deploy/rbac/` y se validan mediante el target `make rbac-smoke`, que sondea cada call site de Kubeport con `kubectl auth can-i`.

---

## Próximos pasos

El trabajo pendiente es trabajo en profundidad — la fontanería central está en su sitio:

1. **Modelado de salud más amplio**: añadir sondas de salud a nivel de aplicación y salud específica de CRDs donde esas señales tengan semánticas claras.
2. **Profundidad de backup**: los backups programados y la retención por site se publicaron el 2026-05-10. Pendientes: backends de object-store, cifrado en reposo, restauración entre clústeres y restauración a un nombre de site distinto (lo último bloqueado a la espera de un modelo de rutas de almacenamiento más rico).
3. **Cobertura de tests**: tests de integración para la sincronización de repositorios, los metadatos de charts y los flujos de trabajo cross-DocType.
4. **Documentación del operador**: completa. `docs/deploy.md` es la guía narrativa de despliegue y `deploy/rbac/` publica los manifiestos RBAC de mínimo privilegio kustomizados con un validador `make rbac-smoke`.
5. **Soporte más amplio de charts**: expansión controlada del descubrimiento de bench más allá de la identificación de charts exclusiva de `erpnext`.
