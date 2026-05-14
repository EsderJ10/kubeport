# Kubeport — Guía del operador

Esta guía recorre, junto a un operador (un usuario que ostenta el rol `System Manager` en Frappe), cada flujo de trabajo que Kubeport admite, en el orden en que un operador suele ejecutarlos.

Para la instalación consulta el [`README.md`](../README.md). Para la arquitectura que sustenta estos flujos consulta [`docs/architecture.md`](architecture.md). Para los invariantes y patrones consulta [`AGENTS.md`](../AGENTS.md).

---

## 0. Requisitos previos

- Un bench de Frappe / ERPNext en funcionamiento con la app `kubeport` instalada (consulta los pasos de instalación del README).
- Helm 3 en el `PATH` del host del bench.
- Conectividad de red desde el host del bench (o pod) al servidor de API de Kubernetes que se pretende registrar.
- El usuario Frappe del operador tiene el rol `System Manager`.

Todos los flujos siguientes asumen que el operador tiene el espacio de trabajo Kubeport abierto en el Desk (`/app/kubeport-operations`). La superficie de aterrizaje del espacio de trabajo incrusta el Dashboard `Kubeport Overview`: cuatro number cards en rojo/ámbar (Degraded Helm Releases, Failed Frappe Sites, Failed Backups, Stale Operations) que deberían marcar cero, tres donuts de distribución de estados (Helm Release, Frappe Site, Service Bundle), un gráfico de líneas diario de operaciones Helm y cuatro cards azules/turquesa de observabilidad interna (Reconcile Ticks, Stale Ops Recovered, Orphan Jobs Swept, Helm p95 Latency). Debajo se encuentran accesos directos rápidos a cada DocType de Kubeport.

---

## 1. Conectar un clúster de Kubernetes

Elige el modo de autenticación que se ajuste a tu entorno:

| Modo | Cuándo usarlo |
|---|---|
| **kubeconfig** | Ya tienes un kubeconfig funcionando en tu portátil y quieres importarlo a través del navegador. |
| **bearer token** | Un equipo central de plataforma emite un token de portador y un certificado CA para el clúster. |
| **in-cluster** | El propio bench se ejecuta dentro del clúster objetivo (el token de la cuenta de servicio se monta automáticamente). |

### 1.1 Pasos (modo kubeconfig)

1. Abre **Kubernetes Cluster** → **New**.
2. Rellena `Cluster Name` y cambia `Auth Method` a `kubeconfig`.
3. Haz clic en el selector de fichero de kubeconfig y elige tu kubeconfig local. El navegador parsea los contextos y los lista; elige uno.
4. Si el `server:` del contexto seleccionado es `127.0.0.1`, `0.0.0.0` o `localhost`, Kubeport lo reescribe automáticamente a la IP de la puerta de enlace predeterminada del contenedor del bench (de lo contrario, el bench no puede alcanzar el servidor de API). El endpoint original se muestra junto al reescrito.
5. Opcionalmente activa **Skip TLS verification** para clústeres de desarrollo autofirmados. **Nunca** lo actives en producción.
6. Guarda.
7. Haz clic en **Test Connection**. El formulario hace una petición a la API en vivo y muestra un mensaje de éxito / fallo.

### 1.2 Pasos (modo bearer token)

1. Igual que arriba, pero elige `Auth Method = bearer token`.
2. Rellena la URL del servidor de API, el token de portador y el certificado CA.
3. El certificado CA es obligatorio salvo que **Skip TLS verification** esté activado — bearer-token sin CA no se permite por defecto.
4. Guarda y haz **Test Connection**.

### 1.3 Pasos (modo in-cluster)

1. Elige `Auth Method = in-cluster`.
2. Guarda. No hay otros campos — Kubeport lee el token de la cuenta de servicio montado en el pod en el momento de uso.

---

## 2. Registrar un repositorio Helm

1. Abre **Helm Repository** → **New**.
2. Rellena `Repository Name` (se usa como nombre en `helm repo add`) y `URL`.
3. Opcionalmente establece `Include Patterns` (globs separados por comas) para filtrar qué charts se indexan — útil cuando un repo trae cientos de charts y solo quieres un puñado.
4. Guarda.
5. La primera vez que guardas se encola una sincronización de charts en la cola `long`. Observa el campo `Sync Status` de la fila; pasa a `Synced` cuando el worker en segundo plano termina.
6. Las sincronizaciones posteriores se ejecutan a diario en el scheduler. También puedes forzar una sincronización desde el formulario.

Las filas `Helm Chart` y `Helm Chart Version` aparecen automáticamente a medida que la sincronización avanza. Su caché de `default_values` se refresca cuando cambia la última versión de un chart.

---

## 3. Desplegar una Helm Release

1. Abre **Helm Release** → **New**.
2. Elige el `Cluster`, `Namespace` y `Release Name` objetivo. El trío es la identidad de la fila — coincide con el alcance real de Helm.
3. Elige `Chart` del catálogo sincronizado. El formulario obtiene el `values.yaml` por defecto del chart como referencia.
4. Edita `Values` (YAML). El formulario valida el YAML al guardar.
5. **(Para charts ERPNext / Frappe)** Elige una `Site Image` del catálogo (o déjalo vacío para usar la imagen por defecto del chart). Cuando se selecciona una Site Image:
   - `image.repository`, `image.tag` e `image.pullPolicy` se inyectan en los valores de Helm.
   - Cuando la Site Image tiene un digest registrado, el tag se renderiza como `tag@sha256:…` para que Kubernetes descargue los bytes exactos de la imagen.
   - Los overrides manuales de `values.image.*` se rechazan para mantener la fuente de verdad inequívoca.
6. Guarda.
7. Haz clic en **Deploy**. La fila pasa por `Draft → In Progress → Deployed | Degraded | Failed`.
8. El formulario carga la preparación de cargas de trabajo de forma asíncrona: cada fila no preparada abre in situ un panel de observabilidad con tres vistas — logs de pods, eventos de Kubernetes con alcance limitado y contexto de rollout para Deployment / StatefulSet / DaemonSet.

### 3.1 Actualizar

Edita valores o la versión del chart, guarda y haz clic en **Deploy** otra vez. El mismo `helm upgrade --install` idempotente se ejecuta a través de un worker en segundo plano. El flag `pending_changes` de la fila se activa cada vez que la spec deseada difiere del último apply exitoso.

### 3.2 Rollback

El formulario expone el historial de la release de Helm en vivo. Elige una revisión y haz clic en **Rollback**. Un rollback exitoso actualiza los valores deseados guardados y la versión del chart para que coincidan con la revisión en vivo seleccionada (de modo que la fila ya no muestre cambios pendientes).

### 3.3 Desinstalar

Haz clic en **Uninstall**. La desinstalación normal está **bloqueada** cuando una fila `Frappe Site` enlazada está en un estado activo o en vuelo — desinstalar el bench mientras existe un site dejaría los datos del lado del bench huérfanos.

Para la vía de override del operador, haz clic en **Force Uninstall** y teclea la cadena de confirmación tecleada. Esto omite la protección de site enlazado.

Una Helm Release en `Failed` no se puede eliminar directamente (puede que aún sea propietaria de recursos del clúster); la desinstalación es la vía de limpieza soportada.

### 3.4 Ingress (charts de Frappe)

Por defecto el chart Frappe/ERPNext se despliega con `ingress.enabled=false`, por lo que la release solo es alcanzable desde dentro del clúster (`kubectl port-forward` para acceso ad-hoc). El formulario de Helm Release expone campos estructurados de ingress que renderizan por ti los valores `ingress.*` del chart:

- **Enable Ingress** — activa el renderizado. Cuando está desmarcado los campos del formulario no tienen efecto.
- **Hostname** — obligatorio cuando ingress está habilitado. Se convierte en `ingress.hosts[0].host`. La ruta está fijada a `/` con `pathType: ImplementationSpecific`. Si Kubeport detecta una IP de ingress-controller de tipo LoadBalancer, sugiere `<release-name>.<ip>.nip.io` para clústeres locales / de desarrollo sin un dominio real.
- **Ingress Class** — `ingress.className`. Kubeport sugiere la `IngressClass` por defecto del clúster, o la única clase detectada cuando hay exactamente una. Déjalo en blanco para que el clúster elija su predeterminada.
- **cert-manager ClusterIssuer** — opcional. Kubeport sugiere un `ClusterIssuer` listo y detectado cuando cert-manager está presente. Cuando se establece, Kubeport renderiza la anotación `cert-manager.io/cluster-issuer` y un bloque `tls` referenciando el secret `<release-name>-tls` (cert-manager crea el secret en la primera reconciliación). Déjalo en blanco para HTTP únicamente.

Estos campos solo se aplican a los charts de Frappe (identificados porque el nombre del chart contiene "frappe" o "erpnext"). Para otros charts, configura el ingress mediante el YAML `Values` en bruto.

**Vía de escape.** Kubeport clasifica el bloque `ingress` del usuario como **avanzado** cuando tiene más de un host, una ruta distinta de `/` con `pathType: ImplementationSpecific`, anotaciones personalizadas más allá de `cert-manager.io/cluster-issuer`, o cualquier clave de nivel superior más allá de `{enabled, className, hosts, annotations, tls}`. Los overrides avanzados preservan el YAML en bruto intacto — esa es tu vía de escape para certificados SAN multi-host, tipos de ruta personalizados o anotaciones adicionales. Los bloques `ingress:` simples o vacíos (predeterminados del chart, fragmentos sobrantes) se reemplazan por el renderizado estructurado, de modo que los campos del formulario siguen siendo autoritativos para el caso común.

Las sugerencias de ingress son descubrimiento de clúster en vivo y de solo lectura. No se persisten en ningún sitio salvo en los campos de Helm Release que guardes explícitamente, y los campos siguen siendo editables cuando no se detecta nada.

Tras guardar con cambios de ingress, se enciende **Pending Changes**. Haz clic en **Preview Diff** antes de desplegar para ver el recurso `Ingress/<release>` que se va a añadir (o el cambio de su bloque `tls`) en el diff deseado vs en vivo. Tras el despliegue, el área Release Status muestra **Reachable at** una vez que el Ingress en vivo reporta una dirección de load-balancer; hasta entonces muestra que Kubeport está esperando la dirección.

### 3.5 Base de datos (charts de Frappe): MariaDB empaquetada vs externa

El chart Frappe/ERPNext se entrega con `dbHost` vacío, por lo que un bench recién desplegado no puede crear sites hasta que algo cablea una base de datos. Kubeport por defecto cierra esa brecha por ti:

- **MariaDB empaquetada (predeterminado).** Deja **Use External Database** desmarcado en el formulario de Helm Release. En install/upgrade, Kubeport también instala una release hermana llamada `<release-name>-mariadb` en el mismo namespace (el chart de MariaDB de Bitnami, anclado a una versión conocida-buena) y renderiza `dbHost: <release-name>-mariadb` en los valores de la release padre. Desinstalar la padre desinstala la hermana. **No** necesitas rellenar `DB Root Password` ni `DB Root Secret` en `Frappe Site` — `Create Site` cablea automáticamente el Secret `<release>-mariadb` del chart que contiene `mariadb-root-password`.
- **Base de datos externa.** Marca **Use External Database** cuando operes una MariaDB fuera de la release (p. ej., una instancia gestionada estilo RDS, una MariaDB compartida en el clúster o un servicio del clúster existente). Kubeport no instalará una hermana y no tocará `dbHost` por ti — debes establecer `dbHost` (y cualquier valor de conexión relacionado) en el YAML `Values` en bruto, y debes proporcionar `DB Root Password` o `DB Root Secret` en cada fila `Frappe Site` que use este bench.

`Create Site` ejecuta una comprobación de pre-vuelo síncrona antes de encolar el Job: la topología empaquetada se rechaza si el chart no produjo el Service `<release>-mariadb` o el Secret root, y la topología externa se rechaza si olvidaste proporcionar credenciales. El mensaje de error te dice exactamente qué corregir.

---

## 4. Aplicar manifiestos en bruto (Service Bundle)

Cuando necesitas desplegar algo que no está empaquetado como chart de Helm — un pequeño `ConfigMap`, un `CronJob`, un `Ingress`:

1. Abre **Service Bundle** → **New**.
2. Elige el `Cluster` y `Namespace` objetivo.
3. Pega un YAML multi-documento (separado por `---`) en `Manifest`.
4. Guarda. El formulario valida cada documento contra la [lista permitida de kinds soportados](../AGENTS.md#supported-resource-kinds-service-bundle): `Pod`, `Service`, `Deployment`, `ConfigMap`, `Secret`, `Namespace`, `Ingress`, `PersistentVolumeClaim`, `StatefulSet`, `DaemonSet`, `Job`, `CronJob`, `ServiceAccount`, `ClusterRole`, `ClusterRoleBinding`, `Role`, `RoleBinding`. Los CRDs quedan fuera de alcance por diseño.
5. Haz clic en **Apply**. El apply en el lado del servidor se ejecuta en la cola `long`.
6. Haz clic en **Delete** para eliminar los recursos del bundle. La fila del bundle entra en estado `Deleting` hasta que termina la limpieza.

El reconciliador comprueba la existencia de los recursos cada 5 minutos y marca como `Degraded` los recursos esperados que falten.

---

## 5. Gestionar Frappe Sites en un bench

Una fila `Frappe Site` es estado deseado para un Frappe site sobre un bench `Helm Release`. Entre bastidores, cada operación es un Job de Kubernetes cuya plantilla de pod se clona de un pod de carga de trabajo de bench en vivo.

### 5.1 Crear un site

1. Abre **Frappe Site** → **New**.
2. Elige `Bench Release` (la fila `Helm Release`).
3. Rellena `Site Name` (debe ser una etiqueta estilo hostname: alfanuméricos en minúsculas, `.`, `-`, `_`, comenzando y terminando con alfanumérico).
4. Rellena `Admin Password`. **DB Root Password** / **DB Root Secret** son opcionales cuando el bench usa MariaDB empaquetada (el predeterminado — Kubeport cablea automáticamente el Secret root del chart) y **obligatorios** cuando el bench tiene **Use External Database** marcado. Consulta §3.5 para la división empaquetada-vs-externa.
5. Opcional: lista las `Apps` a instalar (`erpnext`, `hrms`, etc.).
6. Guarda y haz clic en **Create Site**.
7. Transiciones de estado: `Draft → In Progress → Active | Failed`.

Las credenciales se escriben en un Secret de Kubernetes por Job, con owner-reference al Job, de modo que se eliminan junto con la limpieza por TTL del Job. **Nunca** se colocan en variables de entorno en texto plano.

### 5.2 Migrar un site

En una fila `Active`, haz clic en **Migrate**. Se envía un Job de `bench migrate`. Transiciones de estado: `Active → Migrating → Active | Failed`. No se necesita Secret de credenciales — bench las lee de `site_config.json`.

Si el Job de migrate termina con código distinto de cero pero el site sigue funcional (un caso conocido de falso negativo para `bench migrate`), la fila se recupera a `Active` en la siguiente reconciliación.

### 5.3 Cancelar una operación en vuelo

Mientras un site está en `In Progress`, `Migrating` o `Deleting`, haz clic en **Cancel**. El operation token se rota, la fila se marca como `Failed` y el Job de K8s se elimina con esfuerzo de mejor caso (`propagation_policy=Background` limpia el Secret mediante GC por owner-reference).

Cancelar una operación `Migrating` requiere confirmación tecleada porque matar `bench migrate` a mitad de ejecución puede dejar cambios de esquema de MariaDB aplicados a medias.

### 5.4 Eliminar un site

En una fila `Active` o `Failed`, haz clic en **Delete Site**. Se envía un Job de `bench drop-site --no-backup --force`. Cuando la sonda del bench confirma la ausencia, la propia fila `Frappe Site` se elimina. Si la sonda detecta que sigue presente, la fila queda en `Failed` con los logs del Job en `status_detail` para que el operador pueda reintentar.

La eliminación directa de la fila para una fila `Active` se **rechaza** — dejaría huérfano el site real en la PVC del bench. Usa **Delete Site** en su lugar.

---

## 6. Copia de seguridad y restauración de un Frappe Site

Las copias de seguridad son de primera clase: sus metadatos son un DocType independiente (`Frappe Site Backup`) y sobreviven a la eliminación de la fila `Frappe Site` origen, de modo que un archivo "Available" puede usarse para restaurar incluso después de que la fila origen haya desaparecido.

### 6.1 Realizar una copia de seguridad

1. Desde un formulario `Frappe Site`, haz clic en **Backup**.
2. El worker garantiza que existe una PVC local al namespace llamada `kubeport-backups`, la monta y ejecuta `bench backup --with-files` en un Job de bench clonado.
3. El resultado es un archivo tar en la PVC. Un fichero sidecar `<archive>.size` se escribe **solo** ante un éxito completamente confirmado en disco.
4. La fila pasa por `Pending → In Progress → Available | Failed`. `Available` solo se finaliza tras que un pod de sonda confirme que el sidecar de tamaño existe.

### 6.2 Restaurar desde una copia de seguridad

1. Desde una fila `Frappe Site Backup` en `Available`, haz clic en **Restore**.
2. El worker monta la misma PVC de backup, extrae el archivo y ejecuta `bench restore --force` (con archivos de ficheros públicos/privados si están presentes).
3. Transiciones de estado: `Available → Restoring → Available | Failed`. Una restauración fallida devuelve la fila de backup a `Available` (el archivo sigue siendo utilizable para un reintento posterior) pero marca el site objetivo como `Failed`.

### 6.3 El ciclo de vida del backup está desacoplado de la fila del site

- El ciclo de vida de un archivo es independiente de la PVC del bench del site origen.
- Eliminar una fila `Frappe Site Backup` encola la eliminación del archivo en la PVC (incluso en filas `Failed`, para limpiar archivos con escritura parcial).
- Cancelar un `Frappe Site` padre se propaga en cascada a los backups en vuelo: rota el `operation_token` de cada backup, los marca como `Failed` y encola la limpieza del clúster.

### 6.4 Programar backups y configurar la retención

`Frappe Site` expone tres campos opcionales que convierten los backups manuales en un ciclo desatendido:

- **Backup Schedule (cron)** — expresión cron de cinco campos, p. ej. `0 2 * * *` para diario a las 02:00. Vacío deshabilita la programación.
- **Retention: Max Backups** — mantén como máximo N backups `Available`; los más antiguos se envían a la papelera automáticamente. `0` deshabilita la retención por cantidad.
- **Retention: Max Age (days)** — envía a la papelera automáticamente cualquier backup `Available` con más días que este valor. `0` deshabilita la retención por antigüedad.

Cómo se ejecuta:

1. En cada tick de reconciliación (`*/5 * * * *`), el scheduler comprueba cada site `Active` que tenga una programación no vacía.
2. Si el siguiente disparo calculado desde la ejecución previa (o desde el `creation` de la fila, la primera vez) está en el pasado, el tick avanza el marcador a `now()` y entonces encola un backup por la misma vía que el botón **Backup Now**.
3. Tras la pasada del scheduler, la poda por retención envía a la papelera cualquier fila `Available` que exceda el límite de cantidad o de antigüedad. Enviar una fila a la papelera dispara la limpieza existente del archivo en la PVC, de modo que el archivo se elimina junto con los metadatos.

Casos límite que conviene conocer:

- **Recuperación tras inactividad.** Una inactividad larga (p. ej., el bench estuvo apagado una semana) dispara exactamente **un** backup de puesta al día, no un aluvión — el avance del marcador acota el cálculo del siguiente disparo de croniter.
- **Solape manual + programado.** Si haces clic en **Backup Now** entre la lectura y el encolado del scheduler, el scheduler ve el backup en vuelo y se salta sin encolar un duplicado. El marcador igualmente avanza una franja, lo que solo retrasa la siguiente ejecución programada en una franja.
- **La retención solo toca las filas `Available`.** Las filas `Failed` permanecen para diagnóstico; las filas `In Progress` y `Restoring` están protegidas por la propia salvaguarda de eliminación del doctype.
- **Granularidad.** La resolución cron mínima útil es de 5 minutos — expresiones cron más finas siguen disparándose, pero como mucho una vez por tick de reconciliación.

---

## 7. Descubrimiento en vivo

Desde una fila `Kubernetes Cluster`, el formulario renderiza tablas de descubrimiento en vivo:

- **Helm releases** en el clúster (a nivel de todo el clúster).
- **Frappe sites** en releases identificadas como benches de Frappe (actualmente el chart oficial `erpnext`).

El descubrimiento es de **solo lectura**: nunca persiste filas en MariaDB.

Para convertir una Helm release descubierta en una fila `Helm Release` rastreada, haz clic en **Track** sobre la release. Kubeport crea la fila adoptando la identidad de la release (`cluster/namespace/release_name`). El descubrimiento nunca crea filas de forma silenciosa.

---

## 8. Herramientas del operador

### 8.1 Kubernetes Command

Un doctype de operator-tools intencional para trabajo ad-hoc sobre el clúster. La lista permitida de kinds está fijada:

| Operación | Kinds permitidos |
|---|---|
| Get / List | `Pod`, `Job`, `Secret`, `ConfigMap`, `Service`, `Deployment`, `StatefulSet`, `PersistentVolumeClaim` |
| Delete | `Pod`, `Job`, `ConfigMap` únicamente |

Los cambios destructivos sobre el cuarteto peligroso (`Secret`, `PVC`, `Deployment`, `StatefulSet`) deben pasar por los controladores adecuados — no por este doctype.

Delete requiere un campo `confirm_destructive` tecleado, aplicado tanto al guardar el formulario como de nuevo al ejecutar.

### 8.2 Kubernetes Command Audit Log

Solo añadido. Cada ejecución (éxito o fallo) escribe una fila. `System Manager` tiene acceso de lectura; el doctype nunca se escribe desde la UI. La fila de auditoría está desacoplada de la fila `Kubernetes Command` origen para que el rastro de auditoría sobreviva a la eliminación de la fila.

### 8.3 Catálogo de imágenes de site

Las filas curadas se siembran desde `kubeport/site_images/catalog.json` mediante la sincronización diaria. Los operadores también pueden registrar sus propias imágenes públicas precompiladas en GHCR junto a las filas curadas. Solo las filas curadas pueden marcarse como `is_default`. Las filas curadas no se pueden eliminar (márcalas como `Deprecated` en su lugar); las filas de usuario solo pueden eliminarse cuando ninguna `Helm Release` las referencia.

---

## 9. Reconciliación y deriva

El tick de reconciliación de 5 minutos se ejecuta desatendido. Lo que hará:

- Recuperar las Helm releases `Degraded` a `Deployed` cuando las cargas de trabajo vuelvan a estar preparadas.
- Marcar Helm releases `Deployed` como `Degraded` cuando las cargas de trabajo dejen de estar preparadas.
- Recuperar operaciones Helm `In Progress` / `Uninstalling` obsoletas tras 30 minutos comprobando el estado de Helm en vivo.
- Marcar filas `Service Bundle` como `Degraded` cuando falten los recursos esperados.
- Finalizar las filas `Frappe Site` y `Frappe Site Backup` en vuelo tras una sonda de estado real; aplazar cuando la sonda devuelve `unknown`.
- Barrer Jobs huérfanos gestionados por Kubeport que no estén referenciados por ninguna fila `Frappe Site` / `Frappe Site Backup` (los Jobs más recientes que un tick se omiten para evitar una carrera con un worker).

Si una fila se queda en `Degraded` indefinidamente, eso es un problema real del clúster, no un problema de Kubeport — abre el desglose de preparación en el formulario para ver qué recurso no está preparado.

---

## 10. Procedimiento de smoke-test (pre-release)

Los tests unitarios mockeados no pueden afirmar de forma creíble el comportamiento real en un clúster: aceptación por admission de los manifiestos de Job emitidos, comportamiento de `bench new-site --force` entre versiones mayores de Frappe, GC en cascada real por `ownerReference` al eliminar un Job, suficiencia del RBAC para la cuenta de servicio del plano de control.

Antes de fusionar un cambio que toque el aprovisionamiento de Frappe Site (o antes de publicar una release), pasa por lo siguiente en un clúster real (`kind`, `k3d` o un clúster real). Cada escenario es una única comprobación observable por humano; comparte la misma bench release entre ellos.

1. **Camino feliz**: crea `smoke-1.example.com` con `install_apps=erpnext`. Se espera `Draft → In Progress → Active` en unos 10 min. Confirma vía `kubectl exec` que `bench --site smoke-1.example.com list-apps` devuelve 0 y que el Secret de credenciales fue recolectado junto con el Job.
2. **Recreate (Force)**: marca `Force Create` y haz clic en **Recreate Site (Force)**. Confirma que se crean un nuevo Job y un nuevo Secret y que el Secret antiguo fue recolectado.
3. **Cancel durante In Progress**: arranca `smoke-2.example.com` con un `install_apps=erpnext,hrms` lento. Mientras esté `In Progress`, haz clic en **Cancel**. Confirma que el Job y el Secret desaparecen en segundos.
4. **Delete-while-In-Progress**: arranca `smoke-3.example.com`. Elimina la fila a mitad de `In Progress`. Confirma que no quedan Job ni Secret varados.
5. **Recuperación tras crash de worker (orphan-Job sweep)**: arranca un site, luego SIGKILL al worker de RQ después de que el Job se aplique pero antes de que `operation_job_name` se registre. Espera un tick del reconciliador. El barrido de huérfanos debe eliminar el Job; el log del operador contiene `Sweeping orphan Frappe Site Job '...'`.
6. **Recuperación tras TTL expirado**: baja temporalmente `_JOB_TTL_SECONDS` (`kubeport/tasks/site_tasks.py`) y el cron del scheduler. Crea un site para que el Job sea recolectado antes de la siguiente lectura del reconciliador. Se espera que se dispare la rama 404 y que la fila transicione vía la sonda de estado real.
7. **`activeDeadlineSeconds` (pod colgado)**: crea un site contra un bench cuya BD sea inalcanzable (p. ej., escala MariaDB a 0). Tras `_JOB_ACTIVE_DEADLINE_SECONDS`, se espera `DeadlineExceeded` y que el siguiente tick del reconciliador marque la fila como `Failed` con detalle del log del pod.
8. **Reinicio transitorio del bench durante la reconciliación**: arranca un site, luego dispara un rolling restart del Deployment del bench de modo que los pods queden brevemente no disponibles. Se espera que la sonda devuelva `SITE_PROBE_UNKNOWN` y que la fila no se finalice en este tick; el siguiente tick tras completarse el rollout la finaliza.

En la descripción del PR registra: tipo de clúster, versión de K8s, versión del chart de ERPNext, tag de la imagen del bench, qué escenarios pasaron y cualquier desviación.

---

## 11. Resolución de problemas

### El descubrimiento muestra releases pero ningún site

1. Confirma que la release es el chart oficial `erpnext` — ningún otro chart de bench se reconoce en v1.
2. Comprueba el namespace objetivo en busca de pods de carga de trabajo **en ejecución**. Los pods de infraestructura (MariaDB, Valkey) se excluyen por diseño.
3. Inspecciona el desglose de preparación de `Helm Release` para logs, eventos, contexto de rollout y estado de PVC con alcance limitado.

```bash
kubectl get pods -n <namespace>
kubectl describe pod -n <namespace> <pod-name>
kubectl get pvc -n <namespace>
kubectl get events -n <namespace> --sort-by=.lastTimestamp
```

### Una Helm Release se queda atascada en `In Progress` o `Uninstalling`

Se auto-recuperará tras 30 minutos por el reconciliador, que comprueba el estado de Helm en vivo y ajusta el estado de la fila. Si quieres recuperar antes, puedes volver a disparar la operación una vez se confirme que el worker anterior está muerto.

### Un Frappe Site se mantiene en `In Progress` después de que el Job haya desaparecido

El reconciliador lo recogerá en el siguiente tick. Si el Job fue limpiado por TTL antes de que el reconciliador lo leyera, la rama de recuperación basada en sondas finaliza la fila.

### Un backup sigue informando `In Progress` después de que el Job haya salido

El pod de sonda que monta la PVC de backup y lee el sidecar `<archive>.size` es la fuente de verdad. Si `<archive>.size` falta, el backup **no** se considera `Available` — eso es intencional. Inspecciona los logs del Job y el contenido de la PVC.
