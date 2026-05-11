# Changelog de Kubeport

Registro de decisiones de arquitectura para colaboradores y agentes. Cada entrada registra qué cambió, por qué se eligió el enfoque y qué alternativas se descartaron.

**Formato**: Las entradas están ordenadas de más reciente a más antigua. Cada entrada incluye Contexto (el problema o la necesidad), la Decisión (qué se eligió y por qué), Alternativas descartadas (qué no se eligió y por qué) e Implementación (cómo se construyó).

---

## 2026-05-10 — Orquestación de MariaDB de Bitnami integrada para releases de Frappe

### Contexto

El chart oficial de Frappe / ERPNext se entrega con `dbHost` vacío: una release recién desplegada arranca las cargas de trabajo de gunicorn / nginx / scheduler / worker / valkey pero no tiene base de datos, por lo que la primera llamada a `bench new-site` dentro del bench falla con un error de conexión. El flujo de trabajo previo del operador era "despliega tu propio MariaDB en otro lugar, copia su host en `values.dbHost`, copia sus credenciales de root en cada fila de `Frappe Site`". Esto incumple la promesa de *funciona desde el primer momento* sobre la que se construye el resto del proyecto — el descubrimiento es en vivo, el ingress puede activarse con un toggle, la imagen del bench está anclada por digest, pero levantar un stack ERPNext funcional seguía requiriendo que el operador ejecutase un flujo paralelo de aprovisionamiento de base de datos antes de poder hacer clic en **Create Site**.

### Decisión

Cuando el chart coincide con `is_frappe_site_chart()` y la nueva casilla `use_external_database` en `Helm Release` no está marcada (valor por defecto), el worker de instalación/actualización de Kubeport también instala una release hermana llamada `<nombre-release>-mariadb` en el mismo namespace, usando el chart OCI de MariaDB de Bitnami anclado en `oci://registry-1.docker.io/bitnamicharts/mariadb` versión `25.1.1`. Los valores renderizados de la release padre reciben `dbHost: <nombre-release>-mariadb` para que el chart resuelva al Service hermano. Desinstalar la release padre desinstala la hermana. El operador puede desactivar esto marcando **Use External Database**, en cuyo caso Kubeport no instala ninguna release hermana y no tocará `dbHost` — el operador mantiene el control total del cableado.

`Frappe Site._preflight_db_topology` se ejecuta de forma síncrona en **Create Site** para rechazar el clic cuando el cableado claramente no va a funcionar: la topología integrada requiere que el Service `<nombre-release>-mariadb` y el Secret de root existan; la topología externa requiere que el operador haya proporcionado credenciales de root en la fila. La comprobación previa es una búsqueda rápida y síncrona de Service / Secret — verde es necesario pero no suficiente (el worker sigue resolviendo dinámicamente), pero rojo significa de forma fiable que el camino feliz está roto, así que lo mostramos en el hilo de la UI en lugar de dejar que un Job se envíe y falle segundos después.

El anclaje del chart importa: un cambio incompatible de Bitnami rompería silenciosamente cada release de Frappe con MariaDB integrado en la siguiente instalación/actualización. El anclaje está documentado al principio de `helm_tasks.py` y las actualizaciones pasan por revisión de código con evidencia de `helm show chart … --version <nueva>`.

Una corrección separada envuelve este trabajo de extremo a extremo: las llamadas al subproceso helm de Kubeport ahora ejecutan `frappe.db.commit()` antes de invocar el shell, de modo que la instalación de la release hermana integrada nunca activa el error "commands out of sync" / "Connection is busy" de MariaDB durante la ruta de renderizado en el hilo de la petición que materializa los valores del chart desde la base de datos.

### Alternativas descartadas

- **Una dependencia de subchart en un wrapper de chart propio de Kubeport.** Acopla el calendario de releases de Kubeport al del chart oficial de Frappe; cada actualización del chart de Frappe requeriría reconstruir y volver a publicar el wrapper. La orquestación de releases hermanas es una integración más ligera con el mismo resultado final.
- **Un MariaDB compartido a nivel de clúster.** Pesadilla de multitenencia: la consulta desbocada de una release congelaría el bench de todas las demás; las copias de seguridad tendrían que sortear esquemas compartidos; el radio de explosión se amplía en todos los niveles.
- **Sin orquestación; documentar un runbook de "desplegar MariaDB primero".** Estado previo a este trabajo. La fricción la paga cada operador en cada nueva release; documentarla no la reduce.
- **Release hermana en un namespace diferente.** Añade un problema de descubrimiento de Service entre namespaces, más superficie de RBAC adicional para el plano de control. Las releases hermanas en el mismo namespace reutilizan el RBAC de la release padre y su semántica de teardown.
- **Detección automática analizando los valores del chart en busca de un `dbHost` existente.** Frágil (los charts varían en forma) y no da respuesta para el caso de base de datos externa (el operador puede no haber rellenado el campo aún). Una casilla explícita es más honesta.

### Implementación

- `kubeport/tasks/helm_tasks.py`:
  - `_BUNDLED_MARIADB_CHART_REF = "oci://registry-1.docker.io/bitnamicharts/mariadb"` y `_BUNDLED_MARIADB_CHART_VERSION = "25.1.1"` anclan el chart upstream para reproducibilidad.
  - `_ensure_bundled_mariadb_release(parent_release_name, …)` y `_uninstall_bundled_mariadb_release(parent_release_name, …)` envuelven `helm.install_or_upgrade` / `helm.uninstall` para la release hermana. `_bundled_mariadb_values()` renderiza un YAML de valores mínimo que ancla el nombre del Secret de la release hermana de Bitnami para que el chart padre pueda acceder al Secret `<release>-mariadb` de la hermana por convención de Bitnami en lugar de depender de `<hermana>-mariadb-mariadb` u otras rutas específicas de la versión del chart.
  - `install_or_upgrade_release` llama a `_ensure_bundled_mariadb_release` cuando `is_frappe_site_chart()` y `use_external_database` no está marcado; `uninstall_release` llama a `_uninstall_bundled_mariadb_release` de forma simétrica y tolera "release: not found" como ya limpio.
- `kubeport/kubeport/doctype/helm_release/helm_release.json`: nueva sección Database con `use_external_database` (Check, por defecto `0`). `prepare_release_values` renderiza `dbHost: <nombre-release>-mariadb` mediante `bundled_mariadb_release_name()` cuando el chart es Frappe y el operador no ha optado por la alternativa externa. El YAML de `Values` del usuario gana — cualquier `dbHost` preexistente en los valores brutos se deja intacto.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`: `_preflight_db_topology` se ejecuta al inicio de `create_site`. El flujo integrado usa `can_resolve_db_host_for_release` más `_resolve_db_root_secret_for_release`; el flujo externo requiere `db_root_password` o `db_root_secret` en la fila de Frappe Site.
- `kubeport/tasks/site_tasks.py`: la resolución de DB_HOST por Frappe Site tiene alcance al namespace de la release padre y respeta la división integrado-vs-externo. Las lecturas obsoletas de `_resolve_db_host_for_release` ya no se filtran entre releases. La limpieza previa del Job ahora reintenta en caso de transición, liberando los Secrets `kubeport-bench-creds` previos antes de enviar el nuevo Job.
- `kubeport/tests/test_helm_tasks.py`: cubre el camino feliz de ensure/uninstall, el salto en charts que no son de Frappe, el salto en opt-out de base de datos externa, la tolerancia a "release not found" en uninstall, y el registro de otros errores.
- `kubeport/utils/helm.py` / puntos de llamada en `kubeport/api/helm_diff.py` y el controlador del doctype: `frappe.db.commit()` se ejecuta antes de cada llamada al subproceso helm desde el hilo de la petición. Las tareas que mutan el clúster ya hacen commit antes de `enqueue`, así que esto solo parchea los shell-outs de helm del lado de lectura (`helm template`, `helm get manifest`).
- `docs/operator-guide.md` §3.5 (Base de datos — integrada vs. externa) y el paso §5.1 Crear-un-site ahora documentan el toggle y su efecto en los campos de Frappe Site.

---

## 2026-05-10 — UX de Ingress: sugerencias de descubrimiento de solo lectura y vía de escape "anulación avanzada"

### Contexto

Los campos de ingress estructurados que se publicaron antes en el día permitían a los operadores activar el ingress con cuatro campos del formulario, pero el formulario no daba ninguna señal en vivo sobre *qué escribir*. Los nuevos operadores miraban un `Ingress Class` y un `cert-manager ClusterIssuer` vacíos sin saber si el clúster tenía un `IngressClass` por defecto, si cert-manager estaba instalado siquiera, o qué hostname usar en un clúster `kind` / `k3d` sin zona DNS. Un problema secundario surgió durante las pruebas: un bloque `ingress:` vacío o por defecto del chart en el YAML de `Values` bruto del usuario se filtraba y anulaba silenciosamente los campos estructurados, mientras que un bloque con múltiples hosts o anotaciones personalizadas estaba siendo sobreescrito por ellos — la división autoritativa entre "campos estructurados del formulario" y "vía de escape YAML bruto" no era consistente y dependía de qué ruta materializaba los valores primero.

### Decisión

Añadir descubrimiento de ingress en vivo y de solo lectura al formulario de Helm Release mediante un único endpoint público `kubeport.api.discovery.get_ingress_suggestions(cluster_name, release_name)` que devuelve `{ingress_classes, default_ingress_class, cluster_issuers, default_cluster_issuer, controller_addresses, suggested_hostname, capabilities, errors}` en un solo round-trip. El formulario lo usa para poblar sugerencias no persistidas:

1. **Ingress Class** — el `IngressClass` por defecto del clúster (el anotado con `ingressclass.kubernetes.io/is-default-class: "true"`), o el único detectado cuando hay exactamente uno.
2. **cert-manager ClusterIssuer** — un `ClusterIssuer` listo cuando se detecta cert-manager en el clúster. Vacío cuando cert-manager está ausente o ningún emisor está en estado `Ready: True`.
3. **Hostname sugerido** — cuando el controlador de ingress elegido expone un Service LoadBalancer con una IP externa, el formulario propone `<nombre-release>.<ip>.nip.io`. Útil en `kind` / `k3d` / minikube sin una zona DNS real.

Las sugerencias se obtienen en vivo, nunca se persisten en MariaDB, y los campos permanecen editables cuando no se detecta nada para que una configuración en un clúster nuevo no quede bloqueada por la ausencia de sugerencias.

La división autoritativa entre campos estructurados y YAML bruto se reenuncia como una regla explícita de "anulación avanzada". `render_ingress_values()` pasa el YAML del usuario por `_has_advanced_ingress_override()`, que clasifica un bloque `ingress` como avanzado si lleva alguna clave más allá de `{enabled, className, hosts, annotations, tls}`, tiene más de un host, tiene algún path distinto del `/ ImplementationSpecific` por defecto estructurado, o tiene anotaciones más allá de `cert-manager.io/cluster-issuer`. Las anulaciones avanzadas preservan el YAML del usuario sin modificar (la vía de escape para certificados SAN multi-host). Los bloques `ingress:` simples o vacíos (valores por defecto del chart, fragmentos residuales) son reemplazados por el renderizado estructurado para que los campos del formulario sigan siendo la fuente de verdad.

### Alternativas descartadas

- **Persistir las sugerencias descubiertas en MariaDB para que aparezcan sin que el clúster sea accesible.** Las sugerencias quedarían obsoletas (un `IngressClass` eliminado en el clúster seguiría apareciendo en el formulario), y el invariante definitorio de Kubeport es que el estado observado nunca se persiste.
- **Bloquear el guardado cuando no se detecta ningún `IngressClass`.** Demasiado restrictivo — los operadores en clústeres de desarrollo sin `IngressClass` aún necesitan una forma de establecer `Enable Ingress = false` y continuar.
- **Regla "cualquier clave `ingress` gana".** Tentador porque es una línea de código, pero hace que los campos estructurados sean inútiles para cualquier release cuyo chart por defecto ya incluya un bloque `ingress:` stub (la mayoría lo hacen). El clasificador de anulación avanzada es más código pero coincide con la intención del operador.
- **Fusión de tres vías entre el YAML bruto y los campos estructurados.** La fusión de YAML se vuelve ambigua rápidamente y frustraría el objetivo de "los campos del formulario son la fuente de verdad para el caso simple".
- **Omitir la sugerencia de IP de LoadBalancer → nip.io.** Es la diferencia entre "haz clic en Guardar y funciona en `kind`" y "ve a buscar qué es nip.io". Barato de renderizar y fácil de ignorar.

### Implementación

- `kubeport/api/discovery.py`: un nuevo endpoint público `get_ingress_suggestions(cluster_name, release_name)` que devuelve el payload completo anterior. Los helpers internos `_pick_default_ingress_class`, `_pick_default_cluster_issuer` y `_suggest_hostname` dan forma a los valores por defecto. `_discover_cluster_capabilities` absorbe los errores por alcance en el payload `errors[]` para que una CRD ausente o una denegación de RBAC en (p. ej.) `ClusterIssuer` no vacíe toda la respuesta.
- `kubeport/utils/discovery.py`: sondas compartidas `discover_ingress_classes(cluster_name)`, `discover_ingress_controller_addresses(cluster_name)` y `discover_cluster_issuers(cluster_name)` (listado de cert-manager con fallback suave ante CRD ausente mediante apiextensions).
- `kubeport/kubeport/doctype/helm_release/helm_release.js`: en la carga y en el cambio de clúster llama al endpoint de sugerencias, puebla los campos vacíos y renderiza una pequeña nota "Detectado: …" en línea junto a cada campo; las sugerencias nunca sobreescriben un campo no vacío.
- `kubeport/kubeport/doctype/helm_release/helm_release.py`: `render_ingress_values()` llama a `_has_advanced_ingress_override()` en el bloque `ingress` del usuario; en anulaciones avanzadas devuelve el YAML sin modificar. El helper `_is_single_root_ingress_host()` ancla la forma por defecto estructurada (un único host, un único path `/` con `pathType: ImplementationSpecific`).
- `kubeport/kubeport/doctype/kubernetes_cluster/kubernetes_cluster.js` expone la detección de ingress en el panel de descubrimiento en vivo para que el operador pueda comprobar el clúster desde la fila del clúster antes de abrir nunca un formulario de Helm Release.
- `docs/operator-guide.md` §3.4 (Ingress) actualizado para documentar las sugerencias de descubrimiento de solo lectura y la vía de escape de anulación avanzada.

---

## 2026-05-10 — Deduplicación del ruido en el log de reconciliación y refuerzo del harness de seed de escalado

### Contexto

Una release cuyo chart estaba permanentemente roto (typo en una referencia de chart, versión de chart eliminada en upstream, PVC atascada) hacía que el reconciliador de 5 minutos llamara a `frappe.log_error` con el mismo título y mensaje en cada tick, indefinidamente. Frappe escribe una fila de `Error Log` por llamada, así que una sola release rota producía 288 filas de log al día sin añadir ninguna señal más allá de la primera. La rotación del log de errores nunca daba abasto.

En un problema paralelo, el harness de seed de evaluación/escalado usaba un `frappe.utils.now()` sin semilla para la generación de marcas de tiempo de backup en `eval/scaling/seed.py`, lo que hacía que los escenarios de reproducción determinista fueran no deterministas en los límites de fecha.

### Decisión

Un nuevo helper `_TickErrorLog` mantiene un conjunto de claves `bucket` de deduplicación por tick y enruta cada llamada a `frappe.log_error` del reconciliador a través de `emit(bucket, *, title, message, warn_msg=None)`. La primera aparición de un bucket por tick escribe la fila del Error Log como antes; las apariciones posteriores del mismo bucket en el mismo tick registran una única línea `frappe.logger("kubeport").warning` para que la causa raíz recurrente siga siendo visible en el log del worker sin multiplicar filas en la base de datos. Se construye un `_TickErrorLog` nuevo al inicio de cada función de reconciliación de nivel superior para que los ticks nunca compartan estado de deduplicación.

Los buckets son cadenas estables de la forma `"<alcance>::<clúster>::<clase de error>"`, elegidas para que una release / bench / clúster mal configurado colapse en un único bucket independientemente de cuántas filas lo enumeren dentro del tick. Los errores de `helm release: not found` obtienen su propio sufijo de bucket (`release-not-found`) mediante `_bucket_for_error()` para que nunca oculten clases de error genuinamente nuevas.

El harness de seed de escalado ahora genera las marcas de tiempo de backup a partir de un reloj de fixture derivado del RNG determinista del seed en lugar del reloj de pared, de modo que seeds idénticos producen contenidos de fila idénticos entre ejecuciones.

### Alternativas descartadas

- **Limitar la tasa de `frappe.log_error` globalmente.** Demasiado grueso: enmascararía errores no relacionados que se disparan en la misma ventana desde componentes completamente diferentes.
- **Degradar el error a un log de debug.** Los errores del reconciliador son accionables por el operador; relegarlos ocultaría problemas reales detrás del ruido que intentábamos suprimir.
- **Supresión por release con una ventana TTL.** Tentador, pero añade estado con un TTL sobre el que hay que razonar entre reinicios de workers. Por tick es sin estado y más simple.
- **Hacer hash del mensaje en lugar de elegir una clave de bucket.** Dos errores semánticamente idénticos con mensajes de excepción diferentes caerían en buckets de hash distintos y ambos se registrarían; un bucket explícito `<alcance>::<clúster>::<clase de error>` los colapsa como se pretende.

### Implementación

- `kubeport/tasks/reconciliation.py`: nueva clase `_TickErrorLog` con `emit(bucket, *, title, message, warn_msg=None) -> bool` más un helper `_bucket_for_error(error: Exception) -> str` que da a Helm "release not found" su propio sufijo de bucket. Se instancia al inicio de cada función de reconciliación de nivel superior (`reconcile_all_releases`, `reconcile_site_backups`); los bucles por fila propagan la instancia a cada ruta de error que antes llamaba a `frappe.log_error` directamente.
- `kubeport/hooks.py`: los puntos de entrada del trabajo programado no cambian externamente, pero la vida útil del colector por tick coincide con un único tick.
- `kubeport/tests/test_reconciliation.py`: los tests de regresión anclan el comportamiento de deduplicación por tick (mismo bucket dos veces en un tick → una fila de Error Log más una advertencia; mismo bucket en dos ticks consecutivos → dos filas de Error Log).
- `eval/scaling/seed.py` y `eval/scaling/_inproc.py`: `now()` se reemplaza por un helper de reloj con semilla. Los escenarios de escalado existentes resiembran una vez al inicio de cada escenario para eliminar la contaminación de estado entre escenarios.

---

## 2026-05-10 — El espacio de trabajo del operador refleja el Dashboard de visión general

### Contexto

El espacio de trabajo `Kubeport Operations` exponía ocho tarjetas numéricas y un acceso directo al Dashboard `Kubeport Overview`, pero ninguno de los cuatro gráficos del dashboard (tres donuts de estado + un gráfico de líneas de operaciones diarias) se renderizaba nunca en el propio espacio de trabajo. Los operadores tenían que hacer clic en una página de Dashboard separada para ver la distribución de estados, lo que derrota el propósito de una superficie de aterrizaje de un vistazo.

Un segundo problema surgió durante la auditoría: cada tarjeta numérica existente en `content[]` usaba `"type":"card"` con `"data":{"card_name":"…"}`. Ese tipo de bloque renderiza el widget "Card" de Frappe (una lista de enlaces), no una Number Card. Las tarjetas en el espacio de trabajo desplegado solo aparecían porque Frappe recurre al array `number_cards[]` del espacio de trabajo cuando el bloque de contenido no puede resolverse — no estaban siendo controladas por el layout.

### Decisión

Reescribir el `content[]` del espacio de trabajo para (a) incrustar los cuatro gráficos del dashboard como bloques `chart`, (b) corregir los bloques de tarjetas numéricas a `"type":"number_card"` para que el layout controle el renderizado, y (c) añadir un subtítulo de párrafo corto bajo cada encabezado de sección para el acabado que distingue una superficie de aterrizaje de una cuadrícula de depuración.

Flujo de secciones, de arriba a abajo:

1. **Fleet Health** — cuatro tarjetas numéricas rojas/ámbar que deberían leer cero (Helm Releases Degradadas, Frappe Sites Fallidos, Backups Fallidos, Operaciones Obsoletas).
2. **Distribución de estados** — dos donuts lado a lado (Helm Release, Frappe Site) y un donut de ancho completo (Service Bundle). Service Bundle es de ancho completo para evitar el layout asimétrico de "donut solitario junto a espacio vacío".
3. **Actividad de operaciones** — gráfico de líneas de ancho completo de operaciones de Helm Release por día.
4. **Observabilidad interna** — cuatro tarjetas numéricas azul/teal (Ticks de Reconciliación, Ops Obsoletas Recuperadas, Jobs Huérfanos Barridos, Latencia p95 de Helm).
5. **Acceso rápido** — acceso directo a `Kubeport Overview` promovido al primer tile, luego accesos directos a listas de DocType agrupados por frecuencia de uso.

Los cuatro gráficos también se añaden al array `charts[]` del espacio de trabajo para que el registro de gráficos de Frappe los vincule a este espacio de trabajo en el momento de la sincronización.

### Alternativas descartadas

- **Mantener el espacio de trabajo solo con tarjetas y confiar en el Dashboard de visión general separado.** Dos superficies mostrando datos parcialmente solapados — el coste de un clic extra se paga en cada sesión del operador, y el registro de gráficos del Dashboard ya existe.
- **Tres donuts a `col=4` cada uno en una fila.** Prohibido por el bloque de gráficos del espacio de trabajo (`min_width: 6` en `chart.js`).
- **Tres donuts apilados a `col=12`.** Verticalmente alto y visualmente monótono — enterrar el gráfico de líneas de Operaciones debajo de tres donuts de ancho completo empuja la Observabilidad Interna fuera del primer viewport.
- **Eliminar los donuts por completo y mostrar solo el gráfico de líneas.** Los donuts de estado son el widget más consultado del Dashboard de visión general; eliminarlos habría sido una regresión en la densidad de información.

### Implementación

- `kubeport/kubeport/workspace/kubeport_operations/kubeport_operations.json` — bloques de contenido reescritos; `charts[]` poblado con los cuatro gráficos del dashboard; `modified` actualizado.
- Sin cambios en DocType, API o tareas — es una edición pura de fixture/layout. Los gráficos del dashboard y las tarjetas numéricas referenciadas no cambian y siguen viviendo en `kubeport/kubeport/dashboard_chart/` y `kubeport/kubeport/number_card/`.

---

## 2026-05-10 — Campos de ingress en Helm Release (charts de Frappe)

### Contexto

Una release ERPNext recién desplegada levanta todas las cargas de trabajo (gunicorn, nginx, socketio, scheduler, workers, valkey) pero **sin Ingress** — el chart de Frappe tiene por defecto `ingress.enabled=false`. Los operadores que accedían al site tenían que hacer `kubectl port-forward` o editar manualmente el YAML de `values` bruto, copiando el esquema `ingress.*` del chart (hosts, paths, className, annotations, tls) de memoria o de la documentación. El doctype de Helm Release ya era específico para charts de Frappe (inyección de StorageClass, renderizado de imagen de site), así que los campos de ingress estructurados con el mismo alcance encajaban en el diseño existente.

### Decisión

Añadir cuatro campos al doctype de Helm Release — `ingress_enabled`, `ingress_hostname`, `ingress_class_name`, `ingress_cluster_issuer` — que renderizan los valores `ingress.*` del chart de Frappe cuando están activados. TLS es opt-in mediante cert-manager: cuando se especifica un emisor, Kubeport emite la anotación `cert-manager.io/cluster-issuer` y un bloque `tls` que referencia un Secret por release `<nombre-release>-tls`.

El renderizado está controlado por `is_frappe_site_chart()` (la misma puerta usada para la inyección de StorageClass), y el YAML de `values` proporcionado por el usuario gana — si el YAML bruto ya contiene alguna clave `ingress`, Kubeport lo deja intacto. Esto preserva la vía de escape para certificados SAN multi-host y otras configuraciones avanzadas.

Los cuatro campos de ingress se incorporan a `calculate_release_spec_hash` para que activar el ingress sin tocar `values` siga activando `pending_changes`. Un parche de post-sincronización de modelo único (`recompute_helm_release_spec_hash`) recalcula `last_applied_spec_hash` para cada fila existente para que la nueva forma del payload no haga que cada release informe falsamente de deriva tras la migración.

### Alternativas descartadas

- **Solo YAML de valores bruto (estado actual).** Requería que los operadores memorizaran el esquema de ingress del chart de Frappe. Alto riesgo de copiar y pegar mal (nombres de clave incorrectos → sin efecto silencioso).
- **Renderizado automático para cualquier chart.** Los esquemas de ingress de los charts varían (los charts más antiguos de Bitnami usan `ingress.hostname` en lugar de `ingress.hosts`). El mismo alcance de radio de explosión que la inyección de StorageClass existente solo para Frappe.
- **Un doctype de Ingress separado vinculado desde Helm Release.** Sobremodelado — la vida útil del ingress es la de la release, y los cuatro campos caben en el formulario existente sin saturarlo.
- **SAN multi-host como campos estructurados.** Explosión de esquema para un caso raro. Los operadores con esa necesidad pasan por la vía de escape del YAML bruto.

### Implementación

- Nueva `render_ingress_values()` en `kubeport/kubeport/doctype/helm_release/helm_release.py`. Misma forma que `render_chart_starter_values`: analizar → solo Frappe → preservar valores del usuario → emitir. Codifica `path: /` y `pathType: ImplementationSpecific` (valores por defecto del chart de Frappe).
- `prepare_release_values()` gana kwargs de ingress solo por clave (los valores por defecto preservan todos los puntos de llamada existentes). Orden del pipeline: valores de inicio → ingress → imagen de site. Propagado a través de `kubeport/tasks/helm_tasks.py` (rutas de instalación/actualización y rollback), `kubeport/tasks/reconciliation.py` (recuperación de operaciones obsoletas) y `kubeport/api/helm_diff.py` (vista previa de diff). `api/discovery.py` (adopción de releases) tiene ingress desactivado por defecto — los campos estructurados no intentan hacer ingeniería inversa del estado del ingress desde el YAML de valores existente.
- Los campos del DocType usan `depends_on: "eval:doc.ingress_enabled"` para que solo aparezcan cuando el ingress está activo, más `mandatory_depends_on: "eval:doc.ingress_enabled"` en el hostname. `validate()` del lado del servidor defiende contra escrituras directas a la API.
- Migración: `kubeport/patches/post_model_sync/recompute_helm_release_spec_hash.py` recalcula los hashes de cada fila existente para absorber el cambio de forma del payload sin activar `pending_changes` en todas partes.

---

## 2026-05-10 — Backups programados y retención para Frappe Site

### Contexto

`Frappe Site` se publicó con backup y restauración manuales, pero los operadores tenían que hacer clic en **Backup Now** a mano y podar los archivos antiguos ellos mismos. TODO-20 (P5) cierra la brecha: un calendario cron por site que el reconciliador respeta, y límites de retención por site para que la acumulación ilimitada de archivos no llene el PVC `kubeport-backups`.

### Decisión

Tres campos opcionales en `Frappe Site`: `backup_schedule` (cron de cinco campos), `backup_retention_count` (máximo de filas `Available`) y `backup_retention_days` (antigüedad máxima en días). Más un marcador oculto de solo lectura `backup_schedule_last_run` que impulsa el cálculo de la próxima ejecución del cron.

El calendario se respeta en el tick existente de `reconcile_site_backups` (cada 5 min, trabajo programado separado de `reconcile_all_releases`). El orden del tick es: finalizar filas en vuelo → encolar backups programados → podar retención, de modo que una fila `Available` recién finalizada es visible para el podador en el mismo tick en lugar de esperar otros 5 minutos.

La evaluación del cron usa el `croniter` incluido en Frappe. La base para `get_next` es `backup_schedule_last_run` (o `creation` en la primera ejecución). El marcador se avanza **antes** de que el backup se encole, de modo que un tick concurrente que observe el mismo slot vea que el marcador se ha movido y lo omita. Tras el avance del marcador, el planificador vuelve a obtener el documento del site y vuelve a comprobar el estado + `_has_in_flight_backup()` para manejar la carrera manual-vs-planificador (el operador hace clic en **Backup Now** entre la lectura y el encolado). El método público `backup_site` ahora delega a un helper privado `_enqueue_backup` compartido para que los flujos manual y programado produzcan la misma forma de fila de backup y el mismo ciclo de vida.

El tiempo de inactividad prolongado activa exactamente **un** backup de recuperación por slot vencido, no una avalancha — `croniter.get_next(base)` devuelve el primer slot perdido, y el avance del marcador limita el cómputo del siguiente tick.

La poda de retención solo considera filas con estado `Available`. Las filas `Failed` permanecen para diagnóstico, y las filas `In Progress` / `Restoring` ya están protegidas por `FrappeSiteBackup.on_trash`. El podador usa `frappe.delete_doc` para que la ruta on_trash existente desmonte el archivo del PVC — la retención comparte su limpieza con la papelera manual.

### Alternativas descartadas

- **Una entrada cron del planificador separada por site.** Crearía N entradas cron en el planificador de Frappe — frágil para añadir/eliminar y fuera de la ventana de reconciliación de Kubeport. Reutilizar el tick de cada 5 min cuesta como máximo 5 minutos de granularidad (el diseño acepta esto) y mantiene la lógica de planificación junto al resto del tick de backup.
- **Un campo `trigger_source` en `Frappe Site Backup`.** Tentador para analíticas ("manual" vs "programado"), pero ningún consumidor existente lo necesita. `triggered_by = "Administrator"` para las ejecuciones programadas es suficiente por ahora; los metadatos de procedencia pueden ser un TODO posterior.
- **Poda durante el bucle en vuelo.** Acoplar los dos haría que el comportamiento transaccional de la retención se filtrara en una ruta que ya gestiona presupuestos de sonda y relecturas de Job. Un paso separado que se ejecuta tras la finalización es más simple y más fácil de razonar.

### Implementación

- `kubeport/kubeport/doctype/frappe_site/frappe_site.json`: nuevos campos `backup_schedule` (Data), `backup_retention_count` (Int, non_negative), `backup_retention_days` (Int, non_negative) y `backup_schedule_last_run` (Datetime, oculto), agrupados bajo el salto de sección Backups existente.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`: helper `_enqueue_backup(triggered_by)` extraído de `backup_site`; añadido `_validate_backup_schedule` (usa `croniter.is_valid`); rechaza límites de retención negativos.
- `kubeport/tasks/reconciliation.py`: nuevos `_run_scheduled_backups` y `_prune_backup_retention`, conectados a `reconcile_site_backups` tras la finalización en vuelo existente.
- `kubeport/tests/test_reconciliation.py`: extendido el test de delegación con los dos nuevos barridos; añadido `UnitTestRunScheduledBackups` (seis casos incluyendo recuperación, salto en vuelo, carrera del marcador, carrera de estado) y `UnitTestPruneBackupRetention` (límite de recuento, límite de antigüedad, unión de ambos, sin operación cuando no hay sites configurados).
- `docs/control-plane-state.md`, `docs/operator-guide.md`, `docs/codebase-summary.md`: describen el nuevo comportamiento y los casos límite.

---

## 2026-05-10 — Vista previa de diff de Helm Release

### Contexto

Los operadores de `Helm Release` no tenían forma de ver qué cambiaría `helm upgrade --install` antes de hacer clic en Deploy. La capacidad ya estaba listada como fuera del alcance en `docs/control-plane-state.md` §Cobertura de plataforma, y TODO-18 (P5) llama a cerrarla: una superficie de "Vista previa" que renderiza el manifiesto deseado con `helm template` y lo compara con el manifiesto de la release en vivo.

### Decisión

Añadir un endpoint público de solo lectura `kubeport.api.helm_diff.preview_release` más un botón de barra de herramientas "Preview Diff" en el formulario de Helm Release. Ambos lados del diff son operaciones de Helm de solo lectura — `helm template` es solo local según la documentación de Helm 3 (sin contacto con el clúster) y `helm get manifest` es una llamada de API de solo lectura — por lo que el endpoint es seguro de invocar desde el hilo web sin violar el invariante de asíncrono primero.

El endpoint reutiliza `prepare_release_values` del controlador de Helm Release para que el estado deseado renderizado coincida exactamente con lo que aplicaría `install_or_upgrade_release` (incluyendo el renderizado de imagen de site y la inyección de valores de inicio). Ambos manifiestos se indexan por `(kind, namespace, name)`, se canonizan con `yaml.safe_dump(sort_keys=True)` y se procesan con `difflib.unified_diff` por recurso. Un resumen ({added, removed, changed, unchanged, live_present}) acompaña al texto del diff para que el formulario pueda mostrar contadores incluso cuando el diff está vacío.

### Alternativas descartadas

- **Trabajo en segundo plano para el diff.** Ambas llamadas a helm son de solo lectura y están limitadas por el techo existente de `_HELM_READ_TIMEOUT_SECONDS=30`; enrutarlas a través de `frappe.enqueue` añadiría latencia sin cambiar el perfil de seguridad.
- **Plugin externo `helm-diff`.** Añade una dependencia del sistema y una segunda CLI que empaquetar; un diff por recurso basado en `difflib` se ajusta al caso de uso (vista previa del operador, no fusión de tres vías estricta) sin él.
- **Botón del formulario junto a Deploy.** Requeriría un nuevo campo de DocType renderizado como botón. El `add_custom_button` de la barra de herramientas coincide con el patrón existente de Force Uninstall / Show History y mantiene el JSON del doctype sin cambios.

### Implementación

- Nuevo wrapper `helm.template(release_name, chart_ref, namespace, values_yaml, chart_version)` en `kubeport/utils/helm.py`. Refleja la forma de lista analizada de `get_manifest`; sin kubeconfig porque `helm template` no contacta el clúster.
- Nuevo `kubeport/api/helm_diff.py` con `preview_release(name) -> dict` (solo System Manager). Devuelve `{diff, added, removed, changed, unchanged, live_present, error}`. Trata "release: not found" de `helm get manifest` como `live_present: False` y deja que el diff presente cada recurso deseado como añadido.
- Botón de barra de herramientas `Preview Diff` en `helm_release.js` abre un modal con un resumen de contadores y el diff unificado. Oculto mientras haya una operación en vuelo; se bloquea si el formulario tiene cambios sin guardar.
- Tests en `kubeport/tests/test_helm_tasks.py` cubren cuatro escenarios: deseado/live idénticos (diff vacío, solo `unchanged`), cambio solo de valores, cambio de versión de chart (también afirma que la versión se propaga a los kwargs de `helm.template`), y release en vivo ausente (`live_present: false`, todo lo deseado clasificado como `added`).
- Línea de `docs/control-plane-state.md` §Cobertura de plataforma actualizada — la vista previa de diff ya no aparece como fuera del alcance.

---

## 2026-05-10 — Observabilidad interna: contadores, histograma de latencia de helm e IDs de correlación

### Contexto

La tesis afirma defensas de robustez (reconciliador de operaciones obsoletas, barrido de Jobs huérfanos, temporización del subproceso helm) pero no tenía forma de medir con qué frecuencia se activaba alguna de ellas bajo carga. TODO-14 pide contadores en proceso expuestos en el espacio de trabajo del operador más un ID de correlación UUID4 propagado a través de `frappe.enqueue` para que una única operación pueda rastrearse por grep desde web → enqueue → worker.

### Decisión

Añadir un `kubeport/utils/metrics.py` nativo de Frappe respaldado por `frappe.cache()` (Redis por debajo, el mismo almacén que Frappe ya usa). Los contadores (`reconcile_ticks_total`, `stale_ops_recovered_total`, `orphan_jobs_swept_total`) usan `INCRBY` bruto contra claves con alcance de site mediante `RedisWrapper.make_key`. Un histograma de ventana deslizante de latencia de reloj de pared del subproceso helm se registra mediante `LPUSH` + `LTRIM`; los percentiles (p50/p95/p99) se calculan en el momento de la lectura.

El ID de correlación se genera en el punto de encolado de `frappe.enqueue`, se pasa como kwarg al worker y se vincula a `frappe.local.correlation_id` durante el cuerpo del worker mediante un gestor de contexto `correlation_scope`. Un pequeño proxy `metrics.logger(name)` lee ese local y prefija cada línea de log con `[correlation_id=<cid>]`, dando la misma subcadena de log en ambos lados del enqueue.

### Alternativas descartadas

- **Dependencia de Prometheus / métricas externas.** Fuera del alcance de la superficie de la tesis; el TODO lo prohíbe explícitamente. Lo nativo de Frappe es suficiente para exponer valores en vivo en el espacio de trabajo del operador.
- **Solo contadores locales al proceso.** No satisfaría el criterio de aceptación (el worker y la petición web viven en procesos diferentes).
- **Persistir métricas en una nueva fila de DocType.** Aplazado; los valores en Redis cumplen el criterio y evitan una ruta de escritura intensiva en cada tick de reconciliación.

### Implementación

- Nuevo `kubeport/utils/metrics.py`: contadores (comprobados en lista blanca), histograma deslizante, helpers `correlation_scope` / `current_correlation_id` / `logger`.
- Nuevos endpoints públicos en `kubeport/api/dashboard.py`: `internal_metrics_summary`, `reconcile_ticks_card_value`, `stale_ops_recovered_card_value`, `orphan_jobs_swept_card_value`, `helm_p95_latency_card_value`.
- Cuatro nuevas Number Cards bajo `kubeport/kubeport/number_card/`, expuestas como una nueva sección "Observabilidad Interna" en el espacio de trabajo `Kubeport Operations`.
- Incrementos de contadores conectados a `kubeport/tasks/reconciliation.py` (`reconcile_all_releases`, `reconcile_site_backups`, `_set_stale_helm_operation_state`, `_sweep_orphan_site_jobs`) y el temporizador del subproceso helm conectado a `kubeport/utils/helm.py` (`_run_helm`).
- ID de correlación generado en `Helm Release` `deploy_release`, `uninstall_release`, `rollback_release`; propagado como kwarg `correlation_id` a los workers de tarea `install_or_upgrade_release`, `rollback_release`, `uninstall_release`. Un commit posterior extendió el mismo patrón a cada punto de encolado restante: `Frappe Site` (create / delete / migrate / backup / restore / cancel / on_trash / cascade), `Frappe Site Backup.on_trash` → `delete_backup_archive_task`, `Service Bundle` (apply / delete), `Helm Repository` (`add_and_sync_repo` / `sync_repo_charts` y el tick del planificador diario `sync_all_repos`), `Kubernetes Command.execute` y `site_image_tasks.enqueue_sync_site_image_catalog`. Cada tarea acepta `correlation_id: str | None = None` y envuelve su cuerpo en `metrics.correlation_scope`.
- `stale_ops_recovered_total` cuenta cada escritura exitosa de `_set_stale_helm_operation_state` (incluyendo el terminal `Failed`); `orphan_jobs_swept_total` cuenta cada acción de barrido tomada (los 404 en el lado del clúster siguen contando como el huérfano ya había desaparecido — la fila se reconcilió de todas formas).
- Nueva suite de tests unitarios `kubeport/tests/test_metrics.py` cubre los contratos de contador, histograma y alcance de correlación; `kubeport/tests/test_api_dashboard.py` se extiende con un test por nuevo endpoint.

---

## 2026-05-10 — Revisión de documentación: estructura estándar de la industria

### Contexto

La documentación del repositorio había crecido de forma orgánica. La superficie publicada era un `README.md` completo, un `AGENTS.md` de invariantes, el registro de decisiones de arquitectura aquí, y dos documentos de referencia bajo `docs/` (`control-plane-state.md`, `codebase-summary.md`). Lo que faltaba, frente a la estructura estándar de la industria para un proyecto de código abierto, era una separación clara entre un README de punto de entrada, un documento de incorporación de colaboradores, una política de divulgación de seguridad, un documento de arquitectura con diagramas y una guía de operador orientada al usuario. La referencia `docs/codebase-summary.md` también había derivado — era anterior a la adición de `Kubernetes Command`, `Kubernetes Command Audit Log`, `api/observability.py`, `api/dashboard.py` y `tasks/kubernetes_command_tasks.py` y listaba 10 DocTypes cuando el recuento real es 12 (más la fila de auditoría de `Kubernetes Command Audit Log`). `license.txt` aún llevaba los marcadores de posición `[year] [fullname]` sin rellenar. Tres documentos de planificación vivían bajo `docs/plans/` junto a un procedimiento de verificación manual (`docs/frappe-site-smoke.md`), mezclando material de planificación histórico con material de referencia actual.

### Decisión

- Reestructurado el árbol de documentación para coincidir con la estructura estándar de la industria para un proyecto de código abierto:
  - `README.md` es ahora un punto de entrada enfocado — resumen de capacidades, esquema de arquitectura, instalación, primeros pasos y un mapa de documentación que apunta a cada otro documento por objetivo.
  - Añadido `CONTRIBUTING.md` (entorno de desarrollo, convenciones de ramas y PR, estilo de mensajes de commit que coincide con el `git log` existente, tabla de estilo de código, comandos de test, los siete invariantes que todo colaborador debe respetar y una matriz "actualizar cuando" por documento).
  - Añadido `SECURITY.md` (política de divulgación privada, alcance, modelo de confianza y recomendaciones de hardening específicas para una app Frappe como plano de control).
  - Añadido `docs/architecture.md` con diagramas C4 de contexto / contenedor en Mermaid, un diagrama de relaciones de DocType, diagramas de secuencia en tiempo de ejecución para los flujos de deploy / create-site / reconciliación, y una tabla de verdad de mutación por capa que hace auditable el invariante "deseado vs. observado".
  - Añadido `docs/operator-guide.md` que cubre cada flujo de trabajo del operador de extremo a extremo: conexión de clúster, registro de repositorio, ciclo de vida de Helm Release, Service Bundle, ciclo de vida de Frappe Site (create / migrate / cancel / drop), backup / restore, descubrimiento, herramientas del operador, reconciliación y el procedimiento de verificación previo a la publicación (integrado desde `docs/frappe-site-smoke.md`).
  - Añadido `docs/thesis.md` encuadrando el proyecto: declaración del problema, revisión del arte previo, cinco objetivos testables, metodología, resultados frente a objetivos, limitaciones declaradas y trabajo futuro. Este es el documento de contexto de entregable de carga.
- Actualizado `docs/codebase-summary.md` para reflejar el código real: añadidos `Kubernetes Command`, `Kubernetes Command Audit Log`, los módulos faltantes `api/observability.py` y `api/dashboard.py`, el módulo faltante `tasks/kubernetes_command_tasks.py` y referencias a los directorios de fixtures `kubeport/workspace/` y `kubeport/number_card/`.
- Actualizada la tabla de DocTypes de `AGENTS.md` para listar los dos DocTypes de `Kubernetes Command` que anteriormente solo se mencionaban en `control-plane-state.md`.
- Actualizado el índice de documentación de `CLAUDE.md` para reflejar la nueva estructura.
- Renombrado `license.txt` → `LICENSE` (convención de la industria — mayúsculas, sin extensión) y rellenados los marcadores de posición del copyright con `2026 Los Favs` (coincidiendo con `app_publisher` en `hooks.py` y `authors` en `pyproject.toml`).
- Movido `docs/plans/` → `docs/history/` e integrado `docs/frappe-site-smoke.md` en el mismo archivo, con un `docs/history/README.md` que marca explícitamente el archivo como no autoritativo y dirige a los lectores a los documentos actuales para cada tema.

### Alternativas descartadas

- **Reescritura total de `README.md`, `AGENTS.md`, `control-plane-state.md` y `codebase-summary.md`.** Son densos y precisos. Las reescrituras totales perderían información sin mejorar nada demostrable. El trabajo se limitó a correcciones de deriva más reestructuración en torno a la nueva tríada de punto de entrada / arquitectura / operador.
- **Un `CODE_OF_CONDUCT.md`.** Decorativo para un proyecto de autor único; añadiría superficie de mantenimiento sin cambiar el comportamiento. Omitido — puede añadirse más adelante cuando haya colaboradores externos que gobernar.
- **Referencia de API generada (Sphinx / mkdocs-material).** La superficie de API pública es pequeña y ya está enumerada en `docs/codebase-summary.md` con comentarios por endpoint más útiles que las firmas autogeneradas. Las herramientas de generación añaden superficie de CI por un beneficio insignificante.
- **Dividir `CHANGELOG.md` en "decisiones" y "notas de versión".** El formato de registro de decisiones ya captura explícitamente Contexto / Decisión / Alternativas descartadas / Implementación, que es el rastro auditable que el proyecto necesita. Un segundo archivo de notas de versión duplicaría sin añadir señal.

### Implementación

- Ficheros nuevos / reestructurados: `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `LICENSE` (renombrado desde `license.txt`), `CLAUDE.md` (índice de documentos actualizado), `AGENTS.md` (añadidas dos filas de DocType de `Kubernetes Command`), `docs/architecture.md`, `docs/operator-guide.md`, `docs/thesis.md`, `docs/codebase-summary.md` (correcciones de deriva), `docs/history/README.md`, `docs/history/plan-*.md` (movidos desde `docs/plans/`), `docs/history/frappe-site-smoke.md` (movido desde `docs/`).
- El documento de arquitectura usa Mermaid para los diagramas C4 (renderizados de forma nativa por GitHub), así que no se introduce ninguna herramienta de diagrama externa. Los diagramas de secuencia cubren deploy, creación de Frappe Site y tick de reconciliación — los tres flujos que ejercitan cada invariante.
- La guía del operador es dogmática sobre el orden de las operaciones (conectar clúster → registrar repositorio → desplegar release → crear site → backup) para que un lector pueda seguirla linealmente sin referencias cruzadas.
- `docs/thesis.md` enuncia cada uno de los cinco objetivos como una afirmación testable y evalúa cada uno en §5 con evidencia concreta a nivel de código — para que la afirmación "resultados frente a objetivos" del entregable sea auditable, no aspiracional.

---

## 2026-05-09 — Limpieza de calidad de código y gating de tests en CI

### Contexto

Se realizó una revisión exhaustiva del código para detectar bugs, ineficiencias, problemas de seguridad y desviaciones de las mejores prácticas. La arquitectura en sí se encontró sólida: 12 DocTypes solo para estado deseado, todas las mutaciones del clúster encoladas en la cola long con recomprobaciones de token de operación/sincronización, el descubrimiento es de solo lectura, sin `shell=True`, sin construcción de cadenas SQL, anotaciones de tipo aplicadas en APIs públicas. Lo que la auditoría detectó fue una lista corta de problemas tácticos que no merecían una reescritura estructural pero sí merecían corregirse in situ. El flujo de trabajo de publicación de imágenes de site tampoco bloqueaba los tests en PR — las regresiones de formato y tests unitarios podían fusionarse sin ser detectadas.

### Decisión

- Reemplazada la única violación de `self.reload()` en `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.py` con una búsqueda dirigida de `frappe.db.get_value()`. Esta es la única llamada a `reload()` en el repositorio y el invariante del proyecto (`CLAUDE.md`, `AGENTS.md`) lo prohíbe explícitamente a favor de `db_set` / `frappe.db.get_value`.
- Extraído un helper `_cleanup_op_resources()` en `kubeport/tasks/site_tasks.py` y reemplazados tres bloques idénticos de limpieza de job-and-secret dentro de `_run_site_op` con llamadas al mismo. El flujo de control del orquestador es ahora lineal: recomprobación de token → build → apply → record → limpieza en rollback → limpieza en excepción, con un único lugar con nombre donde vive la semántica de rollback.
- Añadido `.github/workflows/ci.yml` con dos jobs: un job `lint` rápido (`ruff format + check`, anclado a v0.14.10 para coincidir con `.pre-commit-config.yaml`) y un job `test` que arranca un bench Frappe v16 contra contenedores de servicio MariaDB y Redis, instala la app kubeport y ejecuta `bench --site test_site run-tests --app kubeport`. Ambos bloquean en PR y push a main.

### Alternativas descartadas

- **Reescritura total en capas hexagonal/DDD/repositorio.** Los controladores de DocType, los métodos públicos y `frappe.enqueue` *son* los modismos del framework — envolverlos en una capa de servicio lucharía contra Frappe y produciría cambios contra los 8.7k LoC existentes de tests de integración sin mejorar nada demostrable.
- **Consolidar el `_APP_NAME_RE` duplicado entre `frappe_site.py:29` y `tasks/site_tasks.py:67`.** El comentario en `tasks/site_tasks.py:64-67` documenta explícitamente la duplicación como defensa en profundidad deliberada: un valor malformado que llegue al worker a través de una escritura directa en BD o una importación de esquema debe seguir siendo rechazado en el límite de interpolación del shell. Compartir una constante no cambiaría la propiedad de seguridad, pero va en contra de la intención declarada del autor de "validar en cada límite de confianza".
- **Envolver `kubernetes.client.exceptions.ApiException` en un `KubeportApiError` personalizado.** La lista inicial de revisión lo señaló, pero una auditoría más detallada encontró que el código ya hace el manejo de excepciones por capas correctamente: `k8s_resources.py` usa capturas estrechas de `ApiException` con re-raise adecuado, y `observability.py`/`discovery.py`/`release_health.py` extraen consistentemente `.status` y `.reason` para mensajes útiles antes de recurrir a `RuntimeError`. Los bloques `except Exception` amplios restantes en `tasks/` son correctos — son hooks del orquestador de último recurso donde cualquier fallo inesperado debe aún activar la limpieza del estado. Sin cambio accionable.
- **Reemplazar el wrapper del subproceso Helm con un SDK de Python.** El ecosistema de SDK de Python para Helm (PyHelm y forks) no tiene mantenimiento; `subprocess.run` con una lista de args (sin `shell=True`) es el estándar de la industria.

### Implementación

- `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.py:130-131` — reemplazado `self.reload(); return {... "status": self.status}` con `status = frappe.db.get_value("Kubernetes Command", self.name, "status")` y devuelto ese valor.
- `kubeport/tasks/site_tasks.py` — añadido `_cleanup_op_resources()` adyacente a los helpers `_best_effort_delete_*` existentes, y sustituido por las tres llamadas a los tres bloques de limpieza duplicados dentro de `_run_site_op` (el rollback de recomprobación de token post-apply, el rollback de `record_job` rechazado, y la limpieza de `except` amplio). El comportamiento no cambia — el tercer punto de llamada reenvía `job_name_for_cleanup if job_applied else None` para que el helper siga respetando la salvaguarda "solo eliminar el job si fue realmente aplicado".
- `.github/workflows/ci.yml` — el job `lint` ejecuta ruff contra la raíz del repositorio usando un runner de Python 3.14; el job `test` configura MariaDB 10.6, Redis 7 (caché + cola) y un bench Frappe v16 nuevo, luego ejercita la suite completa de tests de kubeport. La suite de tests era anteriormente ejecutable solo dentro del contenedor de desarrollo del proyecto; el job de CI reproduce ese entorno en runners estándar alojados en GitHub. El job `test` se publica con `continue-on-error: true` para el primer ciclo — el arranque de bench-en-CI tiene puntos de fragilidad conocidos (cobertura de wheels de Python 3.14 en Ubuntu, deriva ocasional de flags de `bench`, temporización de contenedores de servicio) que es más fácil detectar y corregir desde una ejecución real que anticipar. El flag se eliminará una vez que aterrice una ejecución en verde, restaurando el bloqueo completo en PR.

---

## 2026-05-09 — Automatizar las actualizaciones del catálogo curado de imágenes de site al publicar una etiqueta

### Contexto

El flujo de trabajo de publicación de imágenes de site ya construía y publicaba la imagen GHCR de Kubeport con un digest verificado, pero `kubeport/site_images/catalog.json` aún se actualizaba a mano después de cada versión (el commit `ec2a398` fue el prototipo manual). El manifiesto de "lo que publicamos" se aleja de "lo que se publicó realmente" entre versiones, y un pegado con un carácter incorrecto vuelve a entrar en el doctype en la siguiente sincronización diaria del catálogo.

### Decisión

- Añadido `scripts/update_site_catalog.py`: un script Python independiente, sin frappe, que refleja las expresiones regulares de repositorio GHCR, etiqueta de imagen y digest sha256 del doctype, localiza la fila curada por `(image_repository, frappe_major)` y reescribe solo sus campos `image_tag`, `image_digest`, `source_revision` y `apps_json_hash`. El script aborta con error en cualquier fallo de validación o coincidencia ambigua.
- Conectado el script a `.github/workflows/publish-site-image.yml` para que un push de etiqueta `v*` ejecute la actualización tras la verificación del digest existente, escriba el diff y los nuevos valores en el resumen del paso de la ejecución y suba el `catalog.json` reescrito como artefacto de build llamado `site-image-catalog-<etiqueta>`.
- El operador sigue en el bucle para el commit y el PR: el flujo de trabajo no hace push, no hace commit ni abre un PR. El operador descarga el artefacto (o copia los valores del resumen del paso), hace commit en una rama temática y abre el PR de actualización mediante el flujo de revisión que prefiera.
- La mutación del catálogo está condicionada a `startsWith(github.ref, 'refs/tags/v')`, por lo que los push a main y las builds de PR lo omiten por completo. La tarea diaria `sync_site_image_catalog` sigue reconciliando el catálogo publicado en MariaDB sin cambios.

### Alternativas descartadas

- **Abrir un PR automáticamente con `peter-evans/create-pull-request@v6`.** Requeriría elevar el permiso `contents` del flujo de trabajo a `write` y añade otra pieza móvil en CI. El operador prefirió mantener el paso de commit/PR manual; la ruta del artefacto + resumen del paso entrega los valores calculados sin esa escalada.
- **Hacer push de la actualización directamente a `main`.** Pierde el rastro de revisión y evita el flujo de rama protegida usado en todo lo demás en este repositorio.
- **Guardar el script dentro de `kubeport/site_images/`.** Una edición futura podría traer `frappe` al grafo de importación y romper el runner de publicación, que no tiene instalación de Frappe. `scripts/` mantiene el helper inequívocamente en el lado de CI.

### Implementación

- Reescritor del catálogo: `scripts/update_site_catalog.py`. Las expresiones regulares se copian literalmente desde `kubeport/kubeport/doctype/kubeport_site_image/kubeport_site_image.py:11-13`.
- Pasos del flujo de trabajo: `Bump curated catalog row`, `Summarize catalog bump`, `Upload rewritten catalog` en `.github/workflows/publish-site-image.yml`. El resumen del paso incluye los cuatro campos antes/después más un `git diff` del fichero reescrito.
- Salvaguarda de deriva: `kubeport/tests/test_publish_automation.py::UnitTestRegexDriftFromDoctype` carga el script mediante `importlib.util` y afirma que sus cadenas de `pattern` de expresión regular igualan a las del doctype, para que la suite de tests falle inmediatamente si cualquiera de los lados cambia sin el otro.
- Tests de reescritura: el `UnitTestSiteCatalogRewrite` del mismo módulo cubre el camino feliz, la validez de extremo a extremo a través de `kubeport.site_images.catalog.load_catalog` y cada ruta de abort por validación/coincidencia.

---

## 2026-05-09 — Catálogo de imágenes de site curado y despliegues de bench con digest anclado

### Contexto

Las Helm Releases que respaldaban benches de Frappe se renderizaban con referencias de imagen `frappe/erpnext` arbitrarias suministradas por el operador. Nada impedía que una imagen no revisada llegara a un clúster, y el hash de especificación de la release ignoraba completamente la identidad de la imagen, por lo que intercambiar la etiqueta de imagen en el lugar no activaba un redespliegue. Los despliegues de bench también fallaban en clústeres sin un StorageClass predeterminado anotado: `helm upgrade` fallaba en lo profundo del chart con un error opaco de "no PV found".

### Decisión

- Introducido el doctype `Kubeport Site Image` (con hijo `Kubeport Site Image App`) que registra una imagen publicada en GHCR curada, su digest `sha256:` anclado y las apps incluidas en ella.
- Publicado un catálogo JSON en `kubeport/site_images/catalog.json` y una tarea de sincronización diaria que siembra el doctype desde el manifiesto, marcando las filas curadas. Las filas curadas solo son eliminables marcándolas como Deprecated; las filas registradas por el usuario conviven con las curadas y siguen siendo eliminables cuando no están referenciadas.
- Validadas las coordenadas GHCR y el digest al guardar el doctype para que las referencias de imagen no revisables no puedan entrar en el registro de estado deseado.
- Incorporado el digest de imagen resuelto en el hash de especificación de Helm Release para que los cambios de digest (republicaciones curadas, actualizaciones de anclaje registradas por el usuario) activen un redespliegue en el siguiente paso de reconciliación.
- Inyectado automáticamente el StorageClass predeterminado del clúster y una lista de modos de acceso compatible en los valores Helm renderizados cuando se selecciona una imagen de site, para que los clústeres de un solo nodo y `local-path` se desplieguen sin intervención del operador. El despliegue se aborta de antemano con un mensaje accionable cuando no hay ninguna clase predeterminada anotada y el operador no proporcionó una en los valores.
- Añadido un flujo de trabajo de GitHub Actions que construye la imagen compuesta `frappe/erpnext/kubeport` propia de Kubeport y la publica en GHCR, con atestación de procedencia y verificación de digest.

### Alternativas descartadas

- **Obtener etiquetas sin anclar en el momento del despliegue.** Pierde reproducibilidad — la misma fila de release resolvería a contenido de imagen diferente con el tiempo, y los rollbacks se vuelven imposibles.
- **Construir imágenes dentro de Frappe en el momento de la petición.** Llevaría un daemon Docker al footprint del plano de control y violaría los invariantes de asíncrono primero/estado deseado vs. estado observado. La construcción de imágenes pertenece a CI.
- **Filas del catálogo solo en MariaDB, sin fuente JSON.** Sin rastro de revisión, sin forma de que los colaboradores propongan una imagen curada mediante PR y sin forma de arrancar una instalación nueva.
- **Obligar a los operadores a especificar `persistence.worker.storageClass` manualmente.** Los despliegues de bench son el flujo de trabajo más común; hacer el caso obvio del predeterminado del clúster ergonómico vale la pequeña cantidad de lógica de inyección de valores. Las anulaciones manuales siguen funcionando porque la auto-inyección solo se activa cuando el operador no ha establecido la clave.

### Implementación

- Controladores de doctype + hijo: `kubeport/kubeport/doctype/kubeport_site_image/` y `kubeport/kubeport/doctype/kubeport_site_image_app/`.
- Manifiesto del catálogo y cargador: `kubeport/site_images/catalog.json`, `kubeport/site_images/catalog.py`.
- Tarea de sincronización diaria: `kubeport.tasks.site_image_tasks.sync_site_image_catalog` (registrada en `hooks.py`).
- Hash de especificación + inyección de StorageClass: `kubeport/tasks/helm_tasks.py` (`_get_site_image_digest_for_hash`, `_image_tag_with_digest`, `_resolve_default_storage_class`, `_user_values_have_worker_storage_class`).
- Helper de descubrimiento de StorageClass: `kubeport.utils.discovery.discover_default_storage_class`.
- API de solo lectura para búsqueda de imagen de site: `kubeport/api/site_images.py`.
- Flujo de publicación: `.github/workflows/publish-site-image.yml` (push en etiquetas `main` y `v*`).
- Tests: `kubeport/tests/test_site_image_catalog.py`, `kubeport/kubeport/doctype/kubeport_site_image/test_kubeport_site_image.py`, más extensiones a `kubeport/tests/test_helm_tasks.py` y `kubeport/tests/test_reconciliation.py`.

---

## 2026-05-08 — Desglose de observabilidad de Helm Release

### Contexto

La preparación de Helm Release ya identificaba qué recurso renderizado estaba fallando, pero los operadores aún tenían que salir de Kubeport para el siguiente paso de diagnóstico: logs de pod, eventos de Kubernetes con alcance o contexto de rollout de la carga de trabajo.

### Decisión

- Añadidos endpoints de observabilidad de Helm Release de solo lectura que resuelven la identidad del clúster desde la fila de la release, aplican acceso de lectura del documento más System Manager, y validan el recurso solicitado contra el manifiesto Helm en vivo antes de devolver datos de diagnóstico.
- Añadidos helpers de observabilidad de Kubernetes para logs de pod con alcance de recurso, eventos y contexto de rollout de Deployment / StatefulSet / DaemonSet. Las lecturas de log están limitadas por recuento de líneas de cola y tamaño de respuesta; la API resuelve pods cuya propiedad está probada para el recurso y devuelve logs solo para el pod seleccionado, más la lista de pods y el `selected_pod` por defecto para la UI.
- Extendido el panel de preparación de Helm Release a un panel de observabilidad persistente en el formulario. Cada fila no lista expone acciones de Logs, Eventos y Rollout; la vista de Logs incluye un selector de pod que obtiene el pod seleccionado en cada cambio, mientras que Eventos y Rollout devuelven payloads uniformes `{rows, error}` para que el panel pueda degradarse por sección.
- Añadido un campo `pod_count` a las filas de preparación de carga de trabajo para que el formulario pueda ocultar el selector de pod cuando solo un pod respalda un recurso.
- Todos los datos de observabilidad permanecen efímeros. No se añadieron nuevos DocTypes ni campos de estado observado persistidos.

### Alternativas descartadas

- **Persistir logs/eventos/historial.** Viola el límite de estado deseado frente a estado observado de Kubeport y haría que los diagnósticos obsoletos parecieran autoritativos.
- **Diálogos modales por acción.** La primera iteración usaba `frappe.ui.Dialog` por clic de Logs/Eventos/Rollout; cambiar pods o acciones repetidamente cerraba y reabría modales. Un panel persistente en el formulario mantiene la tabla de preparación y los diagnósticos visibles juntos y sobrevive a las actualizaciones internas del panel.
- **Obtener todos los logs de pod en una sola petición.** Hacía que una sola petición del formulario escalara con el recuento de pods de la carga de trabajo y podía bloquear el hilo web en releases grandes o no saludables. La API ahora obtiene un pod seleccionado por petición mientras sigue devolviendo la lista de pods para la navegación del selector.
- **Añadir logs en streaming.** Útil más adelante, pero mayor que el alcance actual de desglose de diagnóstico.

### Implementación

- `kubeport/api/observability.py`: endpoints públicos de solo lectura con autorización de alcance de release y validación de pertenencia al manifiesto. Eventos y rollout devuelven `{rows, error}`; logs devuelven `{pods, selected_pod, logs_by_pod, errors_by_pod, error}`.
- `kubeport/utils/observability.py`: helpers de Kubernetes con timeouts de petición y límites de payload por pod; `list_pods_for_resource` recorre referencias de propietario para `Deployment` / `StatefulSet` / `DaemonSet` / `Pod` independiente.
- `kubeport/utils/release_health.py`: `pod_count` por carga de trabajo para que el formulario sepa si renderizar el selector de pod.
- `kubeport/kubeport/doctype/helm_release/helm_release.js`: las acciones Logs / Eventos / Rollout de las filas de preparación se renderizan en un panel de observabilidad persistente en el formulario que comparte el canal de actualización en tiempo real existente e ignora las respuestas asíncronas obsoletas después de que los operadores cambien recursos, vistas o pods.
- `kubeport/tests/test_observability.py` y `kubeport/tests/test_api_observability.py`: cobertura de utilidades y API para límites, autorización, obtención de logs de pod seleccionado, envoltorio `{rows, error}` y peticiones malformadas.

---

## 2026-05-04 — Ground truth de backup, cascada de cancelación y refuerzo de Kubernetes Command

### Contexto

La rama `feat/frappe-site-backup` publicó backup/restauración pero una auditoría de fuente primaria detectó cuatro temas que valía la pena corregir antes de la fusión: (1) las filas de backup cuyo Job había expirado del clúster se marcaban silenciosamente como `Failed` incluso cuando el archivo existía en el PVC, (2) cancelar un site a mitad de backup dejaba la fila de backup atascada en `Pending` para siempre porque solo se rotaba el `operation_token` del site, (3) el `cancel_site_task` de mutación del clúster se encolaba en `short` en lugar de `long`, violando la regla de diseño, y (4) el nuevo doctype `Kubernetes Command` exponía Delete en Secret / PVC / Deployment / StatefulSet — una superficie de privilegio más amplia de lo que justifican los casos de uso de diagnóstico, sin log de auditoría distinto de la propia fila.

### Decisión

- **Sonda de ground truth en el lado del PVC para la finalización del backup.** Añadida `_probe_backup_archive_on_pvc(backup, api_client, core_v1)` en `kubeport/tasks/reconciliation.py`. Envía un Pod de corta duración de `busybox` con el PVC `kubeport-backups` montado, lee el sidecar `<archivo>.size` que el script de backup del bench ya escribe solo en éxito, y devuelve `(exists, size_bytes)` / `(missing, None)` / `(unknown, None)` reflejando `_probe_site_state`. `_reconcile_site_backup` consulta la sonda en ambas ramas: Job desaparecido (reemplaza la heurística rota de `size_bytes`) y post-éxito (defiende contra la ventana estrecha donde `tar` sale con cero pero el inodo se pierde antes de que la reconciliación lo lea).
- **Cascada de cancelación a backups en vuelo.** `FrappeSite.cancel_site` y `FrappeSite.on_trash` ahora llaman a `_cancel_inflight_backups_for_site(self.name, ...)`, que rota `operation_token`, establece `status = "Failed"` en cada fila de backup vinculada al site cuyo estado es `Pending` / `In Progress` / `Restoring`, publica un evento en tiempo real y encola `cancel_site_task` para cualquier Job registrado. Esto es lo que evita que la fila de backup quede huérfana por la cancelación a nivel de site.
- **Cola long para mutaciones del clúster.** `cancel_site_task` se encola en `queue="long"` desde ambos puntos de llamada. La cola short tenía timeouts agresivos y reintentos mínimos; la cola long coincide con todas las demás tareas en segundo plano que mutan el clúster.
- **Limpieza del archivo de backup fallido.** `FrappeSiteBackup.on_trash` ahora encola la limpieza del archivo siempre que `storage_path` esté establecido, independientemente del estado. Un backup que escribió parcialmente un archivo y luego falló (p. ej., corrupción de `tar` a mitad de escritura) antes filtraba el fichero en el PVC; ahora se elimina al mover a la papelera como lo haría una fila `Available`.
- **Etiqueta de operación explícita para Jobs autogestionados.** Añadidos `OPERATION_LABEL = "kubeport.io/operation"` y `SELF_MANAGED_OPERATION_VALUES = {"archive-delete"}` en `site_tasks`. El barrido de huérfanos omite los Jobs que llevan estas etiquetas para que la ruta de limpieza no sea manejada accidentalmente por la carrera de ventana de gracia en el barrido.
- **Lista de permitidos de Delete de `Kubernetes Command` reducida.** Delete ahora está restringido a `Pod`, `Job`, `ConfigMap` — tipos reiniciables / recuperables. Secret, PVC, Deployment, StatefulSet, Service están excluidos de la ruta destructiva; los operadores pasan por los controladores adecuados (Helm Release, Service Bundle, Frappe Site) para esos. `validate()` ahora lanza en Delete sin `confirm_destructive` (antes era un `pass` enmascarado como comprobación).
- **Doctype `Kubernetes Command Audit Log`.** De solo adición, solo lectura para System Manager. Cada ejecución añade una fila que captura usuario, clúster, namespace, acción, kind, nombre, resultado y un extracto de salida — desacoplado de la fila origen para que el rastro de auditoría sobreviva a la eliminación de la fila.
- **Truncado de salida visible.** `_finalize` ahora añade un marcador `[... truncated, original N chars ...]` para que un operador depurando un mensaje de error largo sepa que el cuerpo está incompleto.
- **Listener en tiempo real del formulario de backup.** `frappe_site_backup.js` ahora se suscribe a `frappe_site_backup_status_update` y recarga al coincidir el docname, reflejando el listener existente en el formulario de `Frappe Site`.

### Alternativas descartadas

- **Sin sonda — diferir todo mediante `unknown`.** Sin una sonda, la rama de Job desaparecido no tiene señal de la que recuperarse; diferir indefinidamente es lo mismo que pérdida silenciosa.
- **Job de sonda asíncrono rastreado entre ticks de reconciliación.** Añade un campo de estado en la fila de backup más latencia de dos ticks. El Pod de sonda síncrono está limitado (`activeDeadlineSeconds` de 15 segundos) y la recuperación de Job desaparecido es rara en la práctica.
- **Revertir `Kubernetes Command` completamente.** Los casos de uso de diagnóstico (eliminar un PVC atascado, listar pods) son reales. Reducir la lista de permitidos más un log de auditoría es suficiente.

### Implementación

- `kubeport/tasks/reconciliation.py`: nuevo `_probe_backup_archive_on_pvc`, firma de `_reconcile_site_backup` actualizada (ahora toma `api_client`), el barrido de huérfanos reconoce `OPERATION_LABEL` + `SELF_MANAGED_OPERATION_VALUES`.
- `kubeport/tasks/site_tasks.py`: añadidos `OPERATION_LABEL`, `SELF_MANAGED_OPERATION_VALUES`; `delete_backup_archive_task` etiqueta su Job con `kubeport.io/operation=archive-delete`.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`: `cancel_site` / `on_trash` usan `queue="long"` y llaman a `_cancel_inflight_backups_for_site`.
- `kubeport/kubeport/doctype/frappe_site_backup/frappe_site_backup.py`: `on_trash` cubre las filas Failed con un `storage_path` registrado.
- `kubeport/kubeport/doctype/frappe_site_backup/frappe_site_backup.js`: listener en tiempo real.
- `kubeport/kubeport/doctype/kubernetes_command/kubernetes_command.py`: lista de permitidos `_DELETABLE_KINDS`, `validate()` lanza en `confirm_destructive` ausente.
- `kubeport/kubeport/doctype/kubernetes_command_audit_log/`: nuevo doctype de log de auditoría.
- `kubeport/tasks/kubernetes_command_tasks.py`: `_finalize` añade el marcador de truncado, llama a `_append_audit_log`.
- Tests añadidos en `kubeport/tests/test_reconciliation.py`, `kubeport/tests/test_site_tasks.py`, `kubeport/kubeport/doctype/frappe_site_backup/test_frappe_site_backup.py`, `kubeport/kubeport/doctype/kubernetes_command/test_kubernetes_command.py`.

---

## 2026-04-30 — Ciclo de vida de backup y restauración de Frappe Site

### Contexto

`Frappe Site` soportaba creación, eliminación y migración, pero `drop-site --no-backup --force` hacía que un borrado por error fuera irreversible desde dentro de Kubeport. Los operadores necesitaban una ruta de backup/restauración de primera clase que preservara la separación deseado-vs-observado y mantuviera las mutaciones del clúster fuera del hilo web.

### Decisión

- **Metadatos de backup independientes.** Añadido `Frappe Site Backup` como DocType normal, no como tabla hija, para que los metadatos de backup `Available` puedan sobrevivir a la fila de `Frappe Site` origen.
- **Almacenamiento respaldado por PVC para este ciclo.** Los Jobs de backup crean archivos en un PVC RWX `kubeport-backups` local al namespace. `Kubernetes Cluster.backup_storage_class` puede anular la clase de almacenamiento; en blanco usa el predeterminado del namespace.
- **Operaciones de backup/restauración asíncronas.** `backup_site` y `restore_site` rotan el `operation_token` del site padre, escriben los metadatos de operación de la fila de backup y encolan Jobs de cola long a través del andamiaje de operación de site compartido. La restauración requiere confirmación destructiva y reutiliza el estado padre `Migrating`.
- **La reconciliación posee el estado final.** Las filas de backup se convierten en `Available` o `Failed` a partir del estado del Job y los metadatos del archivo. La finalización de la restauración usa la misma sonda funcional de bench que migrate para que una salida falsa negativa del Job aún pueda recuperarse a `Active`.
- **El barrido de huérfanos reconoce los Jobs de backup.** El barrido ahora tiene en cuenta los nombres y etiquetas de Jobs de operación tanto de `Frappe Site` como de `Frappe Site Backup`.

### Alternativas descartadas

- **Almacenar archivos en MariaDB.** Los grandes blobs binarios en la base de datos de estado deseado difuminarían las responsabilidades de metadatos y almacenamiento.
- **Almacenamiento de objetos primero.** El soporte de S3/GCS/Azure necesita credenciales, retención y política de transferencia entre clústeres; el almacenamiento en PVC es suficiente para cerrar la brecha inmediata de no pérdida de datos.
- **Adjuntar backups como tabla hija.** Las filas hija desaparecerían con la fila del site padre, frustrando la recuperación de una eliminación por error.

### Implementación

- `kubeport/kubeport/doctype/frappe_site_backup/`: nuevo DocType, guardas del controlador, script de formulario y tests.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`: añadidos `backup_site()` y `restore_site(...)`.
- `kubeport/tasks/site_tasks.py`: añadidos comandos de backup/restauración, lógica de asegurar/montar PVC de backup, tarea de eliminación de archivo por best effort y etiquetas de Job específicas de backup.
- `kubeport/tasks/reconciliation.py`: añadida reconciliación de backup/restauración y extendido el barrido de huérfanos.
- `kubeport/api/site.py` y `frappe_site.js`: añadidos listado de backups, acción de restauración y logs de backup/restauración.

### Seguimientos conocidos

Los backups programados, la política de retención, los backends de almacenamiento de objetos, el cifrado, la restauración entre clústeres y la restauración a un nombre de site diferente permanecen aplazados.

---

## 2026-04-30 — Ciclo de vida de Helm Release: rollback, desinstalación y salud basada en manifiesto

### Contexto

Las filas de `Helm Release` podían desplegarse y redesplegarse, pero el ciclo de vida post-despliegue era escaso: la clasificación en tiempo de ejecución se basaba solo en `helm status` (por lo que una release `deployed` con pods en crash-loop parecía sana), no había ruta de desinstalación de primera clase (las filas podían eliminarse en cualquier estado, huerfanando silenciosamente los recursos del clúster), no había rollback (los operadores tenían que hacer `helm rollback` fuera de banda y luego convivir con que la especificación deseada de la fila divergía de la realidad), y no había recuperación para las releases atascadas a mitad de operación. El worker de despliegue tampoco tenía salvaguarda de concurrencia contra una operación más nueva que lo superara a mitad de llamada, por lo que un `helm upgrade` lento podía sobrescribir el estado de uno más reciente.

### Decisión

- **Recorrido de preparación basado en manifiesto.** `kubeport/utils/release_health.py:walk` analiza la salida renderizada de `helm get manifest`, filtra a ocho tipos de carga de trabajo integrados (`Deployment`, `StatefulSet`, `DaemonSet`, `Pod`, `Job`, `PersistentVolumeClaim`, `Service`, `Ingress`) y consulta el estado en vivo de cada recurso. `classify_release_state` combina el estado en tiempo de ejecución de Helm con la preparación por recurso del walker: `deployed` + todos listos ⇒ `Deployed`; `deployed` + alguno no listo ⇒ `Degraded`; estados Helm pendientes/fallidos ⇒ `Failed`. Los workers de despliegue y la reconciliación llaman al mismo clasificador para que la política de salud viva en un único lugar.
- **Tokens por operación para Helm.** `tasks/helm_tasks.py:_release_operation_matches` refleja el patrón de la tarea de site: cada método público rota `operation_token` antes de encolar, y el worker vuelve a comprobar tanto el token como el estado antes de cada escritura (entrada, post-llamada-Helm, finalización). Si una operación más nueva ha tomado el control, el worker descarta su escritura silenciosamente. La misma salvaguarda en `_set_helm_reconciliation_state` para que la reconciliación no pueda sobrescribir una acción del operador en vuelo.
- **Recuperación de operaciones obsoletas a los 30 minutos.** `tasks/reconciliation.py:_reconcile_stale_helm_operations` recoge filas `In Progress` / `Uninstalling` de más de `_HELM_OPERATION_STALE_SECONDS = 1800` segundos, vuelve a comprobar el estado Helm en vivo y las recupera (reclasificando mediante el clasificador compartido) o enruta la desinstalación a través de `_reconcile_stale_uninstall` (que trata "release not found" como éxito y restablece la fila a `Draft`). Ventana fija en lugar de un heartbeat del worker: más simple, sin estado adicional y encaja bien con la cadencia del planificador de 5 minutos.
- **Rollback como operación en segundo plano.** `helm_release.py:rollback_release` acepta una revisión objetivo, la valida, rota el token de operación y encola `helm_tasks.rollback_release`. En éxito el worker escribe de vuelta la versión del chart revertida y los valores en vivo en la especificación deseada de la fila — para que el estado deseado de la fila coincida con lo que está ejecutándose realmente, y el siguiente tick de reconciliación no señale deriva.
- **Desinstalación con reconocimiento de dependencias y ruta forzada.** La `uninstall_release` normal se bloquea mientras algún `Frappe Site` vinculado esté en `Active` / `In Progress` / `Deleting` / `Migrating`, o en `Failed` con un puntero a Job (el bench puede aún tener estado dentro de la release). La ruta forzada requiere una confirmación escrita `UNINSTALL <nombre_release>` en la UI y registra la anulación en `helm_status_detail`. Los errores `release: not found` de Helm se tratan como éxito (idempotencia).
- **Eliminación directa bloqueada fuera de `Draft`.** `on_trash` rechaza cualquier fila que no sea `Draft`; la desinstalación es la única ruta de limpieza. Misma justificación que la ampliación de `on_trash` del ciclo de vida del site: una fila `Failed` puede aún tener recursos del clúster y eliminarla silenciosamente de MariaDB los huerfana.
- **Señal de deriva del hash de especificación.** `calculate_release_spec_hash` produce un hash estable sobre chart, versión del chart, namespace y valores; `validate()` recalcula `desired_spec_hash` en cada guardado y señala `pending_changes` cuando diverge de `last_applied_spec_hash`. El hash aplicado solo se actualiza en un despliegue o rollback exitoso, para que los operadores vean la intención no guardada antes de la siguiente operación.

### Alternativas descartadas

- **Confiar solo en `helm status` para la salud.** Barato, pero una release `deployed` con pods en crash-loop o PVCs sin enlazar reportaría verde. El punto de un plano de control es observar el estado real, por lo que el recorrido del manifiesto no es negociable.
- **Mantener un heartbeat del worker en lugar de una ventana de obsolescencia fija.** Más piezas móviles (tabla de heartbeat, barrido de expiración) para un problema que una comparación de marca de tiempo de 30 minutos ya resuelve. La recomprobación del token existente ya maneja la carrera "gana la operación más nueva"; la obsolescencia solo es para workers genuinamente atascados.
- **Recorrer tipos de recursos arbitrarios mediante descubrimiento de CRD.** Fuera del alcance; la salud por CRD no tiene semántica general. Restringir a los ocho tipos integrados mantiene el walker predecible y coincide con lo que la documentación ya promete.
- **Permitir la desinstalación independientemente de los sites vinculados.** Arriesga huerfanar la base de datos/archivos de un bench dentro del PVC de la release. Bloquear por defecto + puerta de fuerza escrita da al operador una ruta deliberada sin hacer fácil la ruta insegura.
- **Persistir filas de preparación por recurso.** Viola la regla "el estado observado nunca se persiste" en `AGENTS.md`. El desglose del formulario vuelve a consultar bajo demanda mediante `frappe.xcall`.
- **Desinstalación automática al eliminar la fila.** La mutación destructiva implícita del clúster desencadenada por una eliminación de MariaDB es exactamente lo que la separación estado deseado-vs-estado observado pretende evitar; la desinstalación sigue siendo una acción explícita del operador.

### Implementación

- `kubeport/kubeport/doctype/helm_release/helm_release.py`: añadidos `deploy_release`, `uninstall_release` (con `force: bool = False` y detección de site bloqueante), `rollback_release`, `get_release_health`, `load_defaults`, `get_release_history`; `validate()` calcula `desired_spec_hash` y `pending_changes`; `on_trash` bloquea filas que no son `Draft`; `build_release_docname` limita la identidad a clúster/namespace/release; la validación de almacenamiento rechaza combinaciones de `local-path` + `ReadWriteMany`.
- `kubeport/kubeport/doctype/helm_release/helm_release.js`: indicadores de estado, listener en tiempo real `helm_release_status_update`, máquina de estados de botones (Install / Upgrade / Retry / Redeploy), diálogo de confirmación escrita de fuerza-desinstalar, diálogo de historial/rollback con selector de revisión, desglose de salud post-despliegue, autocompletado de namespace y versión de chart.
- `kubeport/tasks/helm_tasks.py`: `install_or_upgrade_release`, `rollback_release`, `uninstall_release` todos enrutados a través de `_release_operation_matches` con recomprobaciones de token+estado antes de cada escritura; `_safe_walk` aísla los errores del walker; `_finalize_uninstall_success` restablece la fila a `Draft`; `_is_release_not_found_error` clasifica la desinstalación idempotente.
- `kubeport/tasks/reconciliation.py`: `_reconcile_helm_releases` (curación de Deployed/Degraded), `_reconcile_stale_helm_operations` + `_reconcile_stale_uninstall` (ventana de obsolescencia de 30 min), `_set_helm_reconciliation_state` (escritura guardada por token), comprobación de ventana de `_helm_operation_is_stale`.
- `kubeport/utils/release_health.py`: `walk`, `summarize`, `classify_release_state`, `classify_release_from_cluster`, más preparación por tipo para los ocho tipos integrados y `_attach_warning_events` para la anotación de los últimos N eventos.
- `kubeport/utils/helm.py`: `install_or_upgrade`, `rollback`, `uninstall`, `status`, `get_manifest`, `get_values`, `history`, `show_chart`, `show_values` — todos wrappers de subproceso sobre un kubeconfig temporal por llamada.
- `kubeport/api/discovery.py`: `get_cluster_discovery` anota cada release en vivo con si existe una fila de seguimiento; `adopt_helm_release` crea una fila de estado deseado a partir de una release descubierta con versión de chart + valores como línea base.
- Tests añadidos: `test_helm_release.py` (validación, inmutabilidad, puerta de despliegue), `test_helm_tasks.py` (obsolescencia de token para install/rollback/uninstall, superación de sincronización de repo, inventario de chart), `test_release_health.py` (los ocho tipos + adjuntar evento de advertencia), `test_reconciliation.py` (curación de Helm + recuperación de op obsoleta + ruta uninstall-not-found), `test_discovery.py` (anotación de release + adopción).
- Documentación: `README.md`, `docs/codebase-summary.md` y `docs/control-plane-state.md` actualizados en el mismo ciclo para describir rollback/desinstalación/salud/reconciliación como publicado.

### Seguimientos conocidos

El diff/vista previa de Helm, el desglose de logs de pod e historial de eventos en el formulario, la salud HTTP a nivel de aplicación y la salud con reconocimiento de CRD permanecen fuera del alcance (registrados en `docs/control-plane-state.md` Brechas abiertas).

---

## 2026-04-27 — Ciclo de vida de Frappe Site: refuerzo previo a la fusión

### Contexto

La auditoría previa a la fusión de `feat/site-lifecycle` detectó cinco elementos: cancelar una migración en curso es inseguro porque el DDL de MariaDB no es atómico, el barrido de huérfanos podía competir con la ventana de `db_set` de apply del Job del worker, `on_trash` ignoraba las filas `Failed` que aún tenían un puntero a Job, las nuevas ramas de reconciliación de `Deleting`/`Migrating` carecían de tests de comportamiento directos, y las tres funciones de tarea de site duplicaban la mayor parte de su andamiaje de apply.

### Decisión

- **Cancelación con confirmación para `Migrating`.** `cancel_site` acepta `confirm_destructive: bool = False`; `Migrating` lo requiere. El detalle de estado registra "cancelación destructiva reconocida". El cliente usa un diálogo escrito `CANCEL`, y `on_trash` rechaza las filas `Migrating` para que los operadores deban usar la ruta guardada.
- **Período de gracia del barrido de huérfanos.** `_sweep_orphan_site_jobs` omite los Jobs más jóvenes que `_ORPHAN_SWEEP_GRACE_SECONDS` (5 minutos, coincidiendo con el tick de reconciliación), cerrando la carrera de apply-a-BD del worker.
- **Limpieza de `on_trash` ampliada.** Tras los rechazos de `Active` y `Migrating`, la limpieza se ejecuta siempre que `operation_job_name` esté establecido, independientemente del estado de la fila.
- **Refactorización del orquestador.** `create_site_task`, `delete_site_task` y `migrate_site_task` son envoltorios finos sobre `_run_site_op(...)`. El comportamiento por operación vive en `SiteOpConfig` más callables simples para comando, env y construcción opcional del Secret de credenciales.
- **Tests de comportamiento.** Los tests directos ahora cubren las transiciones de rama de reconciliación de `Deleting` y `Migrating`, el comportamiento de gracia del barrido de huérfanos y las rutas de éxito/superación del `_run_site_op` compartido.

### Alternativas descartadas

- **Bloquear completamente la cancelación para `Migrating`.** Demasiado severo para migraciones genuinamente colgadas; el operador tendría que esperar a `activeDeadlineSeconds`.
- **SIGTERM con un período de gracia largo.** `bench migrate` no tiene un manejador de señal de límite DDL seguro, por lo que esto solo retrasa el mismo riesgo.
- **Período de gracia de barrido de huérfanos más corto.** Un período más corto funcionaría, pero cinco minutos coincide con la cadencia del planificador: un Job más joven que un tick completo de reconciliación nunca se barre.
- **Clases de operación orientadas a objetos.** Más pesado que el estilo de función simple del código base; la configuración de dataclass más callables mantiene el código específico de operación explícito y local.
- **Añadir un estado de ciclo de vida `Cancelling`.** Principalmente cosmético y requeriría nuevas ramas de reconciliación para un estado que normalmente dura segundos.

### Implementación

- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`: `cancel_site` ahora acepta `confirm_destructive`, bloquea `Migrating` y registra el reconocimiento destructivo en `status_detail`.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.js`: cancelar `Migrating` abre un diálogo personalizado cuya acción principal está desactivada hasta que el operador escribe `CANCEL`; envía `confirm_destructive: 1`.
- `kubeport/tasks/site_tasks.py`: añadidos `SiteOpConfig`, `_run_site_op` y `_attach_creds_secret_owner_ref`; las tareas públicas de create/delete/migrate delegan al orquestador.
- `kubeport/tasks/reconciliation.py`: añadidos `_ORPHAN_SWEEP_GRACE_SECONDS = 300` y se omiten candidatos de huérfanos recientes por `creation_timestamp` de Kubernetes.
- Tests: añadidos tests de confirmación del controlador, tests del orquestador, tests de antigüedad del barrido de huérfanos y tests de comportamiento directos para la reconciliación de delete/migrate.

### Seguimientos conocidos

D2 (estado terminal `Deleted` para rastro de auditoría), D3 (forzar eliminación para `Failed` sin Job), D4 (estado `Cancelling`), H5 (sonda bench de pre-vuelo para migrate) y H6 (envoltorio de modo mantenimiento) permanecen fuera del alcance para este ciclo.

---

## 2026-04-26 — Frappe Site: ciclo de vida post-creación (eliminar + migrar)

### Contexto

Una vez que una fila de `Frappe Site` alcanzaba `Active`, el plano de control no tenía forma de actuar sobre el site del lado del bench. `on_trash` solo limpiaba los Jobs de creación *en vuelo* (volvía al inicio salvo que `status == "In Progress"`), por lo que eliminar una fila `Active` huerfanaba silenciosamente el site real (base de datos + archivos) en el PVC del bench. El único remedio era hacer `kubectl exec` en un pod de bench y ejecutar `bench drop-site` a mano — derrotando el propósito de un plano de control que crea recursos pero no puede destruirlos ni mantenerlos. Las migraciones de esquema eran similarmente fuera de banda.

### Decisión

- **Dos nuevos estados en vuelo**: `Deleting` y `Migrating`. El ciclo de vida es ahora `Draft → In Progress → Active | Failed`, más `Active → Migrating → Active | Failed` y `Active|Failed → Deleting → [doc eliminado] | Failed`.
- **Método público `delete_site()`**: rota el token de operación, establece `status="Deleting"`, encola `delete_site_task`. La tarea envía un Job de Kubernetes que ejecuta `bench drop-site --no-backup --force` usando el mismo patrón de clonación del pod de referencia que `create_site_task`. La reconciliación sondea el Job, ejecuta la sonda de bench de tres estados existente, y al confirmar que el site está ausente llama a `frappe.delete_doc` para eliminar la fila. Si el site aún está presente, la fila aterriza en `Failed` con los logs del Job en `status_detail`.
- **Método público `migrate_site()`**: mismo patrón, ejecuta `bench --site $SITE_NAME migrate`. No se necesita Secret de credenciales (el bench lee desde `site_config.json`). La reconciliación ejecuta la sonda funcional; una salida no cero del Job con un site aún funcional se recupera a `Active` (tolerancia de falso negativo).
- **`on_trash` rechaza las filas `Active`** con un mensaje que indica al operador que use "Delete Site" primero. Para las filas en vuelo aún rota el token y encola el `cancel_site_task` existente para eliminar el Job de K8s.
- **`cancel_site()` se extiende a los tres estados en vuelo** con mensajes de detalle de estado conscientes de la operación.
- **`_build_job_manifest` se convierte en `_build_op_job_manifest`** — un constructor de forma de Job genérico que toma `operation_label`, `container_command` y `container_env`. Cada tarea de operación ensambla su propio comando y env, manteniendo las preocupaciones por operación locales mientras comparte la ruta de clonación de la especificación del pod.
- **La reconciliación despacha por estado** mediante `_reconcile_site_create` / `_reconcile_site_delete` / `_reconcile_site_migrate`. `_finalize_site_status` gana un parámetro `expected_status` para que guarde las escrituras de `Deleting` y `Migrating` igual que guardó las de `In Progress`. El nuevo `_finalize_site_deletion` elimina la fila bajo la misma salvaguarda de token.

### Alternativas descartadas

- **Cascada automática de `on_trash` a `delete_site` para filas Active.** Mezcla el ciclo de vida asíncrono en la eliminación de filas: la fila permanecería ahí hasta que el Job confirme, y los operadores que hacen clic en "Delete" esperan desaparición inmediata. El botón explícito "Delete Site" es inequívoco y mantiene el efecto en el clúster visible.
- **Transicionar la fila a `Draft` tras una eliminación exitosa (patrón "Uninstalling → Draft" de Helm Release).** Una fila de Helm Release es una plantilla reutilizable — el despliegue está pensado para volver a desplegarse. Una fila de `Frappe Site` identifica un site específico; una vez eliminado, la fila es restos operacionales. La auto-eliminación coincide con la intención del usuario.
- **`bench drop-site` por defecto (con backup).** Deja un volcado SQL no indexado en el PVC del bench sin forma de exponerlo (aún no tenemos la funcionalidad de backup-restauración). `--no-backup` mantiene el PVC limpio. El backup vendrá como una operación de primera clase con manejo adecuado del almacenamiento.
- **Backup/restauración en este PR.** Superficie sustancialmente mayor — necesita una estrategia de almacenamiento de backup (PVC vs. almacenamiento de objetos), un DocType hijo para las ejecuciones de backup y la plomería de carga de archivos. Aplazado a un ciclo dedicado.
- **Renombrar `creation_job_name`/`creation_job_token` eliminando "creation_".** Los campos ahora contienen el Job de la *operación actual*, no específicamente un Job de creación. Renombrar requiere un parche de esquema y es ortogonal al trabajo del ciclo de vida; aplazado.

### Implementación

- `kubeport/kubeport/doctype/frappe_site/frappe_site.json`: opciones de `status` ganan `Deleting` y `Migrating`. Nuevos botones `migrate_site_btn` y `delete_site_btn` (color danger). `cancel_site_btn` reetiquetado como "Cancel" genérico. Descripciones de `creation_job_name` / `creation_job_token` ampliadas a semántica "Operation Job…".
- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`: `DF.Literal` de `status` extendido. Nuevos métodos públicos `delete_site()` y `migrate_site()`, cada uno rotando el token de operación y limpiando los punteros `creation_job_*` previos antes de encolar. `cancel_site()` acepta `In Progress | Deleting | Migrating`; `status_detail` especifica qué operación fue cancelada. `on_trash` lanza en `Active`; la rama de limpieza en vuelo ampliada a los tres estados en vuelo.
- `kubeport/kubeport/tasks/site_tasks.py`: `_build_job_manifest` → `_build_op_job_manifest(..., operation_label, container_command, container_env, ...)`. El comando y env por operación ahora los construye el llamador. Nuevas `delete_site_task` y `migrate_site_task` reflejando `create_site_task` (salvaguarda de token antes y después del apply, limpieza en excepción, eventos en tiempo real). Nuevos helpers: `_bench_drop_site_command`, `_bench_migrate_command`, `_build_drop_env`, `_build_drop_creds_secret_manifest`. Drop-site y migrate nunca tocan credenciales de administrador; migrate no toca credenciales en absoluto.
- `kubeport/kubeport/tasks/reconciliation.py`: `_reconcile_frappe_sites` filtra por `status IN (In Progress, Deleting, Migrating)` y despacha a manejadores por operación. Nuevos helpers `_reconcile_site_delete` / `_reconcile_site_migrate` / `_apply_delete_probe`; la lógica de creación existente movida a `_reconcile_site_create`. `_finalize_site_status` gana el parámetro `expected_status` (puntos de llamada actualizados; tests de comportamiento existentes actualizados). El nuevo `_finalize_site_deletion` llama a `frappe.delete_doc(..., ignore_permissions=True, force=True, delete_permanently=True)` tras publicar un evento `frappe_site_status_update` con `status="Deleted"`.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.js`: el mapa de indicadores de estado cubre los seis estados. Visibilidad de botones por estado (`Migrate Site` solo para `Active`; `Delete Site` para `Active` y `Failed`-con-Job; `Cancel` durante cualquier estado en vuelo; reetiqueta por operación). El listener en tiempo real detecta `status="Deleted"` y enruta a la vista de lista en lugar de intentar `reload_doc` en un 404.
- `kubeport/kubeport/api/site.py`: docstring de `get_site_job_logs` ampliado al Job de operación actual.
- Tests: tests de `_build_job_manifest` reescritos para usar el constructor genérico nuevo mediante un helper `_create_manifest`; nuevos tests para el comando drop-site, el comando migrate, el env drop (sin contraseña de administrador), la forma del Secret de credenciales drop, el `name=operation_label` del contenedor y la salvaguarda del token de operación para `Deleting`/`Migrating`.
- Documentación: capacidades de `docs/control-plane-state.md` + brechas abiertas + tabla de robustez actualizadas; bullet de capacidad de README ampliado a "Ciclo de vida de Frappe Site".

### Seguimientos conocidos

- Tests de comportamiento de reconciliación para las nuevas ramas de `Deleting` y `Migrating` (basados en mock, similares a los tests de reconciliación de creación existentes). Los tests de creación existentes aún cubren la ruta de despacho+salvaguarda-de-token; los casos explícitos de Deleting/Migrating reforzarían la garantía.
- Renombrado de campos (`creation_job_*` → `operation_job_*`) una vez que se presente una ventana tranquila para un parche de esquema.
- Backup/restauración como el siguiente ciclo dedicado.

---

## 2026-04-23 — Frappe Site: barrido de Jobs huérfanos, timeout de cuelgue, reconciliación guardada por etiqueta, sonda de bench de tres estados

### Contexto

La auditoría previa a la fusión de `feat/frappe-site-provisioning` detectó cinco brechas de robustez que las rondas anteriores no cerraron:

1. Un worker eliminado de forma brusca (OOM, drenaje de nodo, SIGKILL) entre `apply_resource(job)` y `db_set("creation_job_name", ...)` deja un Job real ejecutándose sin puntero del DocType. La reconciliación filtra por `creation_job_name` no vacío, por lo que el Job es invisible; el efecto secundario del PVC persiste hasta que alguien lo note.
2. Un Job atascado en `ImagePullBackOff` / incapaz de alcanzar la BD nunca pasa a `succeeded` o `failed`, por lo que el sondeo de la reconciliación deja la fila en `In Progress` indefinidamente.
3. La reconciliación lee el Job puro por nombre; un `creation_job_name` obsoleto (o la improbable colisión de nombres) permitiría finalizar el estado de la fila incorrecta.
4. Un reinicio rodante del bench durante la rama de Job-fallido o TTL-expirado hace que la sonda basada en exec eleve una excepción, que el código anterior traducía a `False` → `Failed` terminal — aunque el site podría estar perfectamente bien.
5. La opción de UI `db_type` anunciaba `postgres`, pero el comando del bench siempre usaba los flags `--mariadb-root-*` y el superusuario estaba codificado como `"postgres"` sin forma de anularlo. Nunca se había validado en un bench real respaldado por postgres.

### Decisión

- **Barrido de Jobs huérfanos.** Un nuevo `_sweep_orphan_site_jobs()` se ejecuta al final de cada tick de reconciliación de 5 minutos. Para cada par clúster/namespace que tenga al menos una fila de `Frappe Site` lista Jobs etiquetados `app.kubernetes.io/managed-by=kubeport,kubeport.io/frappe-site` y elimina cualquiera cuyos nombres no aparezcan en el `creation_job_name` de ninguna fila. Usa el `_best_effort_delete_job` existente con propagación Background para que el Secret de credenciales se recoja mediante ownerRef en el mismo barrido.
- **`activeDeadlineSeconds` en cada Job de creación de site.** Por defecto 30 minutos (`_JOB_ACTIVE_DEADLINE_SECONDS`). Suficiente margen para un `bench new-site --install-app=erpnext` realista en hardware modesto, lo bastante ajustado para que los cuelgues genuinos salgan a la luz antes de que el operador lo note.
- **Reconciliación guardada por etiqueta.** Antes de finalizar el estado en cualquier Job, `_reconcile_frappe_sites` llama a `_job_belongs_to_site` para confirmar que la etiqueta `kubeport.io/frappe-site` del Job coincide con el `_safe_label_value(docname)` del documento. No coincidencia → registrar y omitir. Nunca toca la fila.
- **Sonda de bench de tres estados.** `_site_exists_in_bench` se convierte en `_probe_site_state` y devuelve `SITE_PROBE_EXISTS` / `SITE_PROBE_MISSING` / `SITE_PROBE_UNKNOWN`. Los fallos de transporte a nivel exec (fallo de selección de pod, errores de stream) aparecen como `unknown`; el llamador difiere la transición de estado al siguiente tick en lugar de escribir `Failed`. `_exec_bench_site_functional` ahora vuelve a lanzar en lugar de absorber los errores exec para que la distinción sea posible.
- **Ocultar postgres de la UI por ahora.** `frappe_site.json` elimina `postgres` de las opciones de `db_type`. Las ramas de postgres en `_build_env` / `_bench_new_site_command` permanecen para compatibilidad hacia adelante pero solo son alcanzables mediante escritura directa en BD hasta que el flujo esté correctamente conectado y validado en un bench postgres real.

### Alternativas descartadas

- **Reservar `creation_job_name` antes de aplicar el Job.** Rechazado de nuevo por la misma razón que en la entrada de 2026-04-22: nombres fantasma en filas para Jobs que aún no existen. El barrido basado en etiquetas alcanza los mismos Jobs huérfanos sin la ventana de inconsistencia.
- **TTL más corto (`ttlSecondsAfterFinished`) en lugar de `activeDeadlineSeconds`.** El TTL solo se activa una vez que el Job se completa. No ayuda para un Job que aún está colgado — precisamente el caso que necesitamos acotar.
- **Conectar postgres correctamente en este PR.** Requiere un bench respaldado por postgres en CI o al menos una ejecución de verificación conocida como buena. Ninguna está disponible en este ciclo; publicar una opción visible pero rota es peor que publicar una funcionalidad más estrecha.

### Implementación

- `kubeport/tasks/site_tasks.py`: nuevas constantes de nivel de módulo `_JOB_ACTIVE_DEADLINE_SECONDS`, `SITE_DOC_LABEL`, `MANAGED_BY_LABEL`, `MANAGED_BY_VALUE`. `_build_job_manifest` añade `spec.activeDeadlineSeconds = _JOB_ACTIVE_DEADLINE_SECONDS` y usa las constantes de etiqueta. `_build_creds_secret_manifest` también usa las constantes de etiqueta para que el selector del barrido esté garantizado como consistente con lo que escribe el worker.
- `kubeport/tasks/reconciliation.py`: nuevas constantes `SITE_PROBE_EXISTS` / `SITE_PROBE_MISSING` / `SITE_PROBE_UNKNOWN`. `_site_exists_in_bench` → `_probe_site_state` (retorno de 3 estados). Los fallos de transporte exec devuelven `unknown`. `_exec_bench_site_functional` ahora lanza en lugar de absorber excepciones. Nuevo `_job_belongs_to_site(job, site)` llamado en `_reconcile_frappe_sites` antes de cualquier escritura de estado. Nuevo `_sweep_orphan_site_jobs()` conectado a `reconcile_all_releases`. Tanto la rama de Job-fallido como la rama de 404-TTL llaman a `_probe_site_state`; `SITE_PROBE_UNKNOWN` difiere al siguiente tick en lugar de finalizar.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.json`: opciones de `db_type` reducidas de `mariadb\npostgres` a `mariadb`.
- `kubeport/tests/test_site_tasks.py`: nuevo `test_manifest_sets_active_deadline_seconds`.
- `kubeport/tests/test_reconciliation.py`: los tests de Job-fallido existentes cambiados de `_site_exists_in_bench` a `_probe_site_state` con valores de retorno `"exists"` / `"missing"`, y todos los tests de `_reconcile_frappe_sites` ahora parchean `_job_belongs_to_site` para evitar la inspección de etiquetas en los fakes `SimpleNamespace`. Nuevo `test_reconcile_frappe_sites_skips_job_with_wrong_site_label`. Nuevo `test_reconcile_skips_finalize_on_transient_probe_failure`. Nuevo `UnitTestJobBelongsToSite` (4 casos). Nuevo `UnitTestSweepOrphanSiteJobs` (nombre rastreado preservado, huérfano eliminado, nombre de job de creación vacío aún barrido). `test_reconcile_all_releases_only_runs_active_sweeps` extendido con aserción de `_sweep_orphan_site_jobs`.
- `docs/frappe-site-smoke.md`: nuevo procedimiento de verificación en clúster real (8 escenarios) — recuperación de caída del worker (barrido), pod colgado (activeDeadlineSeconds), reinicio transitorio del bench, etc.
- `docs/control-plane-state.md`: capacidades actualizadas, tabla de robustez y brecha "Site Lifecycle" para postgres.

---

## 2026-04-22 — Frappe Site: corregir la ruta Recreate (Force) y Job huérfano en eliminación a mitad de vuelo

### Contexto

La revisión previa a la fusión de `feat/frappe-site-provisioning` detectó dos defectos que sobrevivieron a las rondas anteriores:

1. **`Recreate Site (Force)` era código muerto.** La UI reetiqueta el botón principal y permite el clic cuando `force_create=1` y `status="Active"`, pero el servidor `create_site()` lanzaba en `status="Active"` incondicionalmente, por lo que el botón siempre daba error. El flag `--force` en `_bench_new_site_command` solo era alcanzable desde la ruta Failed → reintentar, no desde Active → recrear, que es el caso de uso anunciado.
2. **Job huérfano en eliminación a mitad de vuelo / recreación forzada.** `create_site_task` vuelve a comprobar el token de operación después de aplicar el Job (para manejar el caso de que el usuario cancele o vuelva a disparar mientras estábamos en vuelo) y vuelve al inicio si no coincide. Nada desmontó el Job que acababa de aplicar. `creation_job_name` permanece vacío en la fila, por lo que `on_trash` y la reconciliación no pueden ver el Job. Corrió hasta completarse sin seguimiento, creando un site en el PVC del bench sin fila en MariaDB. El propio TTL del Job recogió los recursos de K8s pero no los datos del PVC.

### Decisión

- **La puerta del controlador respeta `force_create`.** `create_site()` ahora lanza en Active solo cuando `force_create` no está marcado. La salvaguarda de Python coincide con el contrato del botón JS.
- **El worker se limpia a sí mismo al ser superado.** Cuando la comprobación de token post-apply falla, `create_site_task` llama a `_best_effort_delete_job` y `_best_effort_delete_secret` en el Job y el Secret que acaba de crear antes de volver. La misma limpieza se ejecuta en el manejador de excepciones cuando `job_applied` es verdadero, para que un fallo a mitad del paso de ownerRef no filtre un Job. `_best_effort_delete_job` usa `propagation_policy="Background"` para que el GC de K8s también recoja el Secret mediante ownerRef (cuando estaba adjunto) y los pods del Job.

### Alternativas descartadas

- **Reservar `creation_job_name` antes de aplicar el Job.** Haría el Job visible para `on_trash` antes, pero introduce una nueva ventana de inconsistencia (un nombre registrado para un Job que aún no existe) y obliga a la reconciliación a tolerar nombres fantasma. Eliminar desde el propio worker, usando el estado que ya tiene en alcance, es más simple.
- **Hacer que `on_trash` liste y elimine Jobs por selector de etiqueta cuando `creation_job_name` está vacío.** Funciona, pero la llamada de descubrimiento paga un round-trip en cada papelera de un documento In Progress solo para cubrir una carrera de ventana corta. La limpieza del lado del worker es más barata y captura la misma carrera.

### Implementación

- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`: la puerta de `create_site` cambiada a `if self.status == "Active" and not self.force_create`.
- `kubeport/tasks/site_tasks.py`: nuevo `_best_effort_delete_job` reflejando `_best_effort_delete_secret` (tolerante a 404, advertir-y-continuar en todo lo demás, propagación Background). `create_site_task` rastrea `job_applied: bool` y `job_name_for_cleanup`; la recomprobación de token post-apply y la rama de excepción ambas eliminan el Job huérfano antes de volver.
- `kubeport/tests/test_site_tasks.py`: nuevo `UnitTestBestEffortDeleteJob` para el helper. Nuevo `test_create_site_task_deletes_orphan_job_when_token_superseded_after_apply` llevando `_site_operation_matches` a devolver `[True, False]`. Nuevo `test_manifest_passes_force_flag_through_to_bench_command` como salvaguarda de regresión para la ruta Recreate.

---

## 2026-04-22 — Credenciales de Frappe Site movidas a Secret por Job; limpieza de Job huérfano al mover a papelera

### Contexto

El aprovisionamiento inicial de Frappe Site aterrizó con dos brechas de seguridad/ciclo de vida detectadas durante la revisión previa a la fusión:

1. `ADMIN_PASSWORD` se inyectaba como `value` env en texto plano en la especificación del pod del Job, visible para cualquiera con RBAC de lectura de pods y persistido en etcd hasta el TTL del Job. `DB_ROOT_PASSWORD` tenía una ruta a Secret de Kubernetes pero mantenía un fallback en texto plano para el caso en que el usuario proporciona la contraseña.
2. Eliminar un documento de `Frappe Site` mientras un Job de creación estaba en vuelo huerfanaba el Job: la reconciliación filtra filas por `status="In Progress"`, por lo que la fila eliminada era invisible y el Job corría hasta completarse creando un site no rastreado.
3. Cuando el `ttlSecondsAfterFinished` del Job expiraba antes de que la reconciliación leyera su estado final, el site quedaba atascado en "In Progress" indefinidamente.

### Decisión

- **Enrutar cada credencial a través de un Secret por Job.** `create_site_task` crea `{nombre_job}-creds` (etiquetado `app.kubernetes.io/managed-by=kubeport`, `kubeport.io/frappe-site=<docname>`) antes de enviar el Job. El Job consume `ADMIN_PASSWORD` (siempre) y `DB_ROOT_PASSWORD` (cuando no hay Secret proporcionado por el usuario) mediante `secretKeyRef`. Tras existir el Job parcheamos el Secret con `ownerReferences` → Job + `blockOwnerDeletion: true`, para que el GC de K8s lleve el Secret con la limpieza del TTL del Job. En excepción antes o durante el apply del Job, best-effort `delete_namespaced_secret` para evitar huerfanar credenciales de administrador.
- **Añadir `on_trash` a `FrappeSite`.** Cuando el documento se elimina mientras un Job está en vuelo rota `operation_token` (invalida el worker) y encola `cancel_site_task`, que elimina el Job con `propagation_policy="Background"`; el GC de ownerRef luego recoge el Secret de credenciales como efecto secundario. `cancel_site_task` también llama a `_best_effort_delete_secret` como medida de respaldo para la ventana estrecha donde el parche de ownerRef nunca se adjuntó.
- **Recuperar "In Progress" zombi en 404.** La rama 404 de `read_namespaced_job` de la reconciliación ahora llama a `_site_exists_in_bench` — la misma sonda de bench de dos etapas ya usada en Job-fallido — y transiciona el site a Active o Failed en lugar de registrar y dejarlo atascado.
- **Validar `site_name`.** Rechazar cualquier cosa fuera de una etiqueta de estilo hostname (alfanuméricos en minúscula, `.`, `-`, `_`, comenzando/terminando alfanumérico) para que el autonombre `{bench_release}/{site_name}`, el slug del Job de K8s y el env del bench permanezcan bien formados. La seguridad del shell ya era correcta — `"$SITE_NAME"` en `_bench_new_site_command` no expande sustituciones de comandos en el valor de la variable — pero la brecha de corrección del nombrado necesitaba cerrarse.

### Alternativas descartadas

- **Montar contraseñas mediante `envFrom: secretRef`**: funciona, pero pierde la capacidad de mezclar limpiamente nuestro Secret de credenciales con las entradas `envFrom` existentes del pod de referencia del bench sin arriesgar filtraciones accidentales de variables de entorno. `secretKeyRef` por clave es más preciso.
- **Poner `ownerReferences` en el Secret desde el principio**: rechazado porque el UID del Job no se conoce hasta después de `apply_resource(Job)`. El apply en dos pasos (crear Secret, crear Job, volver a aplicar Secret con UID) es el patrón canónico.
- **Eliminar el Secret de credenciales solo desde `cancel_site_task`**: rechazado porque la rara ruta "Secret aplicado, apply de Job fallido" filtraría credenciales fuera del flujo normal de cancelación. La limpieza best-effort en la rama de excepción de `create_site_task` cierra esa ventana.

### Implementación

- `kubeport/tasks/site_tasks.py`: nuevo `_build_creds_secret_manifest(secret_name, namespace, site_docname, admin_password, db_root_password)`. `_build_env` reescrito: sin más parámetros de contraseña en texto plano; toma `creds_secret_name` y `db_root_in_creds` y emite `secretKeyRef` para ADMIN_PASSWORD y DB_ROOT_PASSWORD. `_build_job_manifest` los propaga. `create_site_task`: apply de Secret → apply de Job → lectura de Job → re-apply de Secret con `ownerReferences`. Cualquier excepción pre-Job activa `_best_effort_delete_secret`. `cancel_site_task`: ruta de eliminación de Job sin cambios, más un `_best_effort_delete_secret` de respaldo al final. Nuevo helper `_best_effort_delete_secret` compartido por ambas rutas.
- `kubeport/kubeport/doctype/frappe_site/frappe_site.py`: nuevo `_SITE_NAME_RE` y `_validate_site_name()` llamado desde `validate()`. Nuevo `on_trash(self)` que rota el token de operación y encola `cancel_site_task` cuando el estado es "In Progress" y hay un `creation_job_name` establecido.
- `kubeport/tasks/reconciliation.py`: la rama 404 en `read_namespaced_job` ahora llama a `_site_exists_in_bench` y finaliza a Active o Failed.
- `kubeport/tests/test_site_tasks.py`: tests de `_build_env` expandidos para los tres nuevos casos (secretKeyRef de ADMIN_PASSWORD, Secret de BD del usuario, fallback de Secret de credenciales). Nuevos tests de `_build_creds_secret_manifest`. Nuevo `UnitTestCreateSiteTask` (salida por token obsoleto, ruta feliz apply-de-Secret→Job→Secret sin contraseñas en texto plano, rollback de excepción con limpieza de Secret huérfano). Nuevo `UnitTestCancelSiteTask` (tolerancia a 404, registro de no-404, limpieza de Secret en ambos).

---

## 2026-04-17 — Creación de Frappe Site mediante Jobs de Kubernetes

### Contexto

Kubeport ya descubría benches de ERPNext (releases de Helm de `chart_name == "erpnext"`) y los sites que vivían en cada bench (mediante exec en pods en ejecución). La siguiente capacidad es crear nuevos sites en esos benches.

El chart de ERPNext (`frappe/helm`) proporciona `jobs.createSite` — una plantilla estándar de Job de Kubernetes (confirmado: sin anotaciones de hook de Helm) que ejecuta `bench new-site` con variables de entorno con plantilla. El mecanismo oficial para activar este job es un `helm upgrade` con `jobs.createSite.enabled: true`.

**Ese enfoque fue rechazado** para esta implementación (ver Decisión de arquitectura más abajo). En su lugar, enviamos un Job de Kubernetes directamente desde un nuevo DocType `Frappe Site` — de la misma manera que `Service Bundle` envía manifiestos en bruto.

### Decisión de arquitectura: envío directo del Job (no helm upgrade)

Evidencia de la investigación de `frappe/helm`:

- `job-create-site.yaml` es un Job batch/v1 *estándar*, no un hook de Helm.
- Solo se renderiza condicionalmente cuando `jobs.createSite.enabled: true`.
- Tras ejecutarse el Job, `enabled` debe volver a `false` o se vuelve a activar en el siguiente `helm upgrade` — creando una colisión de nombre de Job (o un intento duplicado de creación de site).
- `adminPassword` y `dbRootPassword` estarían incrustados como texto plano en el campo `values` de Helm Release (respaldado por MariaDB, no cifrado para este caso de uso).

**Consecuencias de la ruta helm upgrade:**

| Problema | Impacto |
|---|---|
| Los valores son el *estado constante* deseado | Alternar `jobs.createSite.enabled` confunde operaciones puntuales con configuración de despliegue |
| Sin seguimiento limpio del estado | Hay que observar el estado del Job de todas formas; la abstracción de Helm no aporta nada |
| Credenciales en el YAML de valores | adminPassword + dbRootPassword expuestos a cualquier volcado de valores |
| Riesgo de re-activación | Cualquier `helm upgrade` posterior vuelve a activar la creación del site salvo que el operador lo limpie manualmente |

**Ventajas del envío directo del Job:**

- Reutiliza `apply_resource()` que ya gestiona el tipo `Job`
- El Job es efímero y propiedad de kubeport, no de la release de Helm
- El estado es rastreable mediante `BatchV1Api.read_namespaced_job()` en la reconciliación
- Encaja en el patrón existente DocType → tarea en segundo plano → bucle de reconciliación
- `ttlSecondsAfterFinished` limpia automáticamente los Jobs completados

### Diseño: DocType, tarea en segundo plano y reconciliación

**Campos del DocType:**
- `site_name`: El dominio del site (p. ej., `erp.example.com`)
- `bench_release`: Enlace a la Helm Release objetivo (el bench)
- `admin_password`: Contraseña de administrador del site
- `install_apps`: Apps a instalar (p. ej., `erpnext`)
- `db_root_password` o `db_root_secret`: Credenciales de base de datos
- `status`: Draft → In Progress → Active | Failed
- `creation_job_name`: Nombre del Job de K8s para el seguimiento de la reconciliación
- `operation_token`: Salvaguarda de concurrencia

**Tarea en segundo plano** (`create_site_task`):
1. Encuentra un pod de bench en ejecución mediante las utilidades de descubrimiento existentes
2. Clona su imagen de contenedor y el montaje del PVC de sites dinámicamente
3. Construye y envía un manifiesto de Job de Kubernetes
4. Almacena el nombre del Job para que la reconciliación lo rastree

**Reconciliación** (`_reconcile_frappe_sites`):
- Sondea `BatchV1Api.read_namespaced_job()` cada 5 minutos
- En `status.succeeded > 0`: marca Active
- En `status.failed > 0`: **verifica primero la existencia real del site** (ver corrección más abajo)

### Elección de diseño clave: por qué la inspección dinámica del pod para imagen/volúmenes

El contenedor del Job debe usar la misma imagen y montajes de PVC que el bench en ejecución para garantizar que `bench` esté disponible y el directorio correcto de sites esté montado. Codificar etiquetas de imagen o convenciones de nombres de PVC del chart de Helm rompería en actualizaciones de versión o valores personalizados.

Al leer la especificación desde un pod de referencia en vivo, la tarea es resiliente a la evolución del chart sin cambios de código.

---

## 2026-04-17 — Corrección de la reconciliación de Frappe Site: estado real sobre códigos de salida del Job

### Problema

Cuando `bench new-site` sale con estado no cero **a pesar de haber creado el site correctamente**, Kubernetes establece `job.status.failed = 1`. Nuestra reconciliación marcaba el site como Failed, contradiciendo el estado real del clúster (el site existe y es usable).

Esto ocurre frecuentemente en ERPNext cuando:
- El parámetro `--install-app` activa migraciones post-instalación que emiten advertencias no críticas
- Las compilaciones de assets o pasos de configuración de base de datos registran en stderr
- Algún hook post-creación sale con no cero mientras la configuración del site ya estaba escrita

### Por qué importa

El directorio del site y `site_config.json` existen en el bench (confirmado por el descubrimiento) pero kubeport reporta "Failed" — un falso negativo que confunde a los operadores y requiere verificación manual en el clúster.

### Solución: verificación del estado real

Cuando `job.status.failed > 0`:

1. Llamar a `_site_exists_in_bench()` — ejecutar en un pod de bench en vivo y comprobar si `site_config.json` existe realmente (reutilizando las mismas utilidades de descubrimiento).
2. Si el fichero existe → marcar el site como **Active** (el código de salida del Job fue un falso negativo).
3. Solo si el fichero no existe → marcar como **Failed** e incluir las líneas reales del log del pod.

Esto refuerza el invariante del proyecto: **el estado observado siempre se consulta en vivo desde el clúster**. La reconciliación basa su decisión en el estado real, no en un código de salida que carece de significado semántico.

### Mejores detalles de fallo

`_extract_job_failure_detail()` ahora obtiene las últimas 30 líneas del log de stdout del pod fallido en lugar de analizar `terminated.message` (que siempre está vacío — nunca configuramos `terminationMessagePath` en la especificación del Job). Esto proporciona una salida de error significativa cuando el site genuinamente falla (p. ej., contraseña de base de datos incorrecta, error de instalación de app).

### Implementación

- Añadido helper `_site_exists_in_bench(site, core_v1) -> bool` que usa el descubrimiento basado en exec
- Modificado el bloque `elif failed > 0` en `_reconcile_frappe_sites()` para comprobar primero la existencia del site
- Actualizado `_extract_job_failure_detail()` para obtener logs del pod mediante `read_namespaced_pod_log()`
- Añadidos `bench_release` y `site_name` a los campos `get_all()` de la consulta de reconciliación

### Procedimiento de prueba

1. Crear un Frappe Site y hacer clic en Create Site
2. Monitorizar el Job: `kubectl get jobs -n <namespace> -l app.kubernetes.io/managed-by=kubeport`
3. Si el pod del Job sale con no cero, esperar a la reconciliación (5 min) o activarla manualmente
4. **Esperado:** el estado transiciona a **Active** si el fichero del site existe, a pesar de la salida no cero
5. Para probar la ruta de fallo: crear un site con contraseña de root de BD incorrecta
6. **Esperado:** el estado transiciona a **Failed** con el error real de `bench new-site` en el detalle