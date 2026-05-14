# Kubeport Context

Este documento enmarca el repositorio como el artefacto técnico de un proyecto. Plantea el problema, examina el estado del arte, define los objetivos, resume la metodología y presenta los resultados obtenidos frente a dichos objetivos.

El resto de documentos del repositorio (`README.md`, `docs/architecture.md`, `docs/operator-guide.md`, `docs/control-plane-state.md`, `docs/codebase-summary.md`, `docs/evaluation.md`, `CHANGELOG.md`) son la evidencia técnica que respalda las afirmaciones realizadas aquí.

---

## 1. Planteamiento del problema

Frappe y ERPNext son plataformas de negocio de código abierto ampliamente desplegadas. Los operadores que ya gestionan una instancia de Frappe para ERP, CRM, contabilidad o RRHH disponen de dos superficies de control independientes cuando extienden su despliegue a Kubernetes:

1. La **interfaz Frappe Desk**, donde trabajan a diario los operadores (administradores, integradores, administradores de sistemas a tiempo parcial).
2. Una **cadena de herramientas de Kubernetes** independiente (`kubectl`, `helm`, paneles de control, repositorios de IaC), que exige un modelo mental diferente, una gestión de identidad separada y un registro de auditoría propio.

Esta división genera un coste real:

- Los operadores deben cambiar de contexto entre dos interfaces para realizar una única operación de negocio (por ejemplo, "crear un nuevo sitio orientado al cliente en el clúster bench").
- Las operaciones sobre el clúster se realizan habitualmente de forma manual con `kubectl` / `helm`, lo que no deja ningún registro de auditoría de primer nivel dentro del mismo sistema que registra el estado del negocio.
- "¿Qué hay realmente desplegado?" se responde leyendo el estado en vivo del clúster; "¿qué debería estar desplegado?" se responde leyendo archivos de IaC en otro repositorio. Ambos divergen en silencio.

**El problema de la tesis**: diseñar e implementar un plano de control de Kubernetes *dentro de* Frappe que permita a un operador gestionar clústeres, releases de Helm, manifiestos sin procesar y sitios Frappe desde la misma interfaz Desk que ya utiliza, sin perder los invariantes operacionales que un operador real de Kubernetes espera (corrección bajo concurrencia, sin deriva silenciosa, sin salud con falsos positivos, verificación de la fuente de verdad de los efectos secundarios).

---

## 2. Estado del arte

La tesis se posiciona en dos ejes: un eje orientado al operador (las interfaces de gestión de Kubernetes existentes entre las que los operadores eligen hoy) y un eje de investigación (los patrones de diseño de la literatura sobre sistemas distribuidos e ingeniería de plataformas que informan los invariantes de Kubeport). El análisis de productos establece la brecha operacional; los subapartados en prosa que siguen establecen el linaje conceptual.

### 2.1 Superficies de gestión de Kubernetes existentes

Ninguno de los sistemas siguientes cierra la brecha específica identificada en §1.

| Sistema | Qué hace | Por qué no resuelve este problema |
|---|---|---|
| **Lens** (Mirantis) | IDE de escritorio para Kubernetes | Herramienta de escritorio por operador; no es un plano de control multiusuario; no está integrado en ninguna plataforma de negocio. |
| **Headlamp** (CNCF) | Interfaz web de Kubernetes | Panel de control genérico de Kubernetes; sin concepto de sitio Frappe, bench ERPNext o enlace con el estado del negocio. |
| **Rancher** (SUSE) | Gestor de Kubernetes multiclúster | Sólida gestión multiclúster, pero es en sí mismo un producto independiente con su propia identidad, auditoría e interfaz. |
| **OpenShift Console** (Red Hat) | Interfaz integrada para OpenShift | Vinculado a OpenShift; no se integra en una plataforma de negocio externa. |
| **Komodor / Spacelift / Argo CD UI** | Paneles orientados a equipos DevOps | Enfocados en ingenieros de plataformas, no en operadores de aplicaciones de negocio; sin noción de primer nivel del ciclo de vida de un sitio Frappe. |
| **`kubectl` + `helm` + repositorio IaC** | El estado actual | Sin auditoría unificada, detección de deriva ni enlace con el estado del negocio; elevado nivel de habilidad requerido para el operador. |

La brecha conceptual no es "otra interfaz de Kubernetes". Es: **un plano de control cuya unidad de gestión es un sitio Frappe o un bench ERPNext, respaldado por Kubernetes, integrado en el mismo Desk donde funciona el resto del negocio**, con los invariantes operacionales normalmente asociados a las herramientas de ingeniería de plataformas (ejecución en segundo plano, reconciliación de deriva, verificación de la fuente de verdad) en lugar de los invariantes más laxos de una interfaz gráfica sobre `kubectl`.

### 2.2 Patrón operator y bucles de reconciliación

El patrón operator, formalizado por CoreOS en 2016 [1] y actualmente canónico en la documentación de Kubernetes [2], codifica el conocimiento operacional específico del dominio como un controlador software que reconcilia un *estado deseado* (un recurso personalizado) frente al clúster en vivo en cada ciclo. El patrón desciende directamente de la arquitectura de controlador introducida en Borg y heredada por Kubernetes [3], donde cada objeto de la API es un registro de intención que un bucle de control asíncrono impulsa hacia la observación. Hightower, Burns y Beda [4] formulan el bucle como disparado por nivel en lugar de por flanco: el controlador no reacciona a eventos, sino que observa el mundo periódicamente y corrige la deriva, lo que hace que el bucle sea idempotente ante fallos, reintentos y eventos perdidos.

Kubeport adopta este patrón en la capa de Frappe en lugar de hacerlo como un operator de Kubernetes. El ciclo de reconciliación (`*/5 * * * *` en `kubeport/hooks.py`) desempeña el papel del bucle de controlador, la fila del DocType desempeña el papel del recurso personalizado, y el `operation_token` por ejecución desempeña el papel del guardián de versión de recurso que rechaza a los workers obsoletos. La contribución no es el patrón en sí, sino su aplicación dentro de una plataforma de negocio cuyo modelo de ejecución (manejadores de solicitudes síncronos, colas de fondo RQ) es ajeno a la literatura sobre bucles de control.

### 2.3 Estado deseado frente a estado observado en sistemas declarativos

La separación estricta del estado deseado del estado observado es la disciplina fundamental de la gestión de clústeres a gran escala. Verma et al. [5] la documentan como la decisión de diseño central en Borg: el operador declara la intención y el sistema informa de forma asíncrona lo que observó. Los principios de OpenGitOps [6] generalizan la misma idea — una fuente de verdad declarativa, continuamente reconciliada con el sistema en vivo — en un estándar neutral respecto al proveedor.

La disciplina no es meramente organizativa; es una condición previa para un comportamiento correcto ante fallos parciales. El análisis de Vogels sobre la consistencia eventual [7] y la retrospectiva CAP de Brewer [8] establecen que, una vez que las escrituras superan un único viaje de ida y vuelta, un sistema que confunde "lo que se solicitó" con "lo que es verdad ahora" pierde la capacidad de recuperarse de la inconsistencia transitoria. Kubeport aplica esta lección literalmente: el DocType respaldado por MariaDB es el almacén de estado deseado; las consultas al clúster en vivo son las sondas de estado observado; y ambos nunca se combinan en una proyección persistida única. Las rutas de código de descubrimiento son de solo lectura, y la reconciliación impulsa la fila hacia la verdad observada, no al revés.

### 2.4 Patrones de ejecución en segundo plano en plataformas de negocio

El entorno de ejecución de Frappe es fundamentalmente solicitud-respuesta: los manejadores web se ejecutan dentro de un hilo de solicitud síncrono con un tiempo de espera estricto, y el mecanismo canónico para trabajo de larga duración es encolar un job en una cola respaldada por RQ [9]. Esta forma refleja el patrón más amplio que Dean y Ghemawat [10] identificaron para cualquier sistema cuya unidad de trabajo supera una única solicitud: descomponer la operación en una parte delantera síncrona pequeña y una cola asíncrona grande, y dejar que la cola pueda reintentarse, monitorizarse y recuperarse de forma independiente.

Para Kubeport esto es innegociable. Las invocaciones de subprocesos de Helm [11] suelen ejecutarse durante decenas de segundos; `bench new-site` se ejecuta durante minutos; `helm upgrade --install` contra un registro lento puede ejecutarse durante más tiempo. Nada de eso es aceptable dentro de un manejador web de Frappe. El plano de control encola por tanto cada llamada que muta el clúster en la cola `long` de RQ con `enqueue_after_commit=True`, y el worker vuelve a comprobar el estado de la fila y el `operation_token` antes de actuar. La comprobación del token convierte la cola de un buzón de disparar y olvidar en algo más parecido a un guardián de reloj lógico en el sentido de Lamport [12]: un mensaje obsoleto puede aún llegar, pero el receptor detecta la obsolescencia solo a partir del token, sin una vista global del orden de los mensajes.

Las entradas bibliográficas se enumeran en §9 y están vinculadas a registros BibTeX en [`docs/references.bib`](references.bib).

---

## 3. Objetivos

El proyecto persigue cinco objetivos. Se enuncian como afirmaciones verificables para que la sección de resultados (§5) pueda evaluarse frente a ellos.

| # | Objetivo | Criterio de éxito |
|---|---|---|
| O1 | Los operadores pueden conectarse a clústeres de Kubernetes desde Frappe Desk usando cualquiera de los tres modos de autenticación del mundo real (kubeconfig, token bearer, cuenta de servicio en clúster). | Existe un DocType `Kubernetes Cluster` que valida cada modo de autenticación, admite la importación de kubeconfig desde el navegador y ejercita con éxito una prueba de conexión contra un clúster real. |
| O2 | Los operadores pueden registrar repositorios de Helm y desplegar / actualizar / revertir / desinstalar releases de Helm desde el Desk. | Existen los DocTypes `Helm Repository`, `Helm Chart` y `Helm Release`; el catálogo de charts se sincroniza en segundo plano; los despliegues se ejecutan mediante jobs en segundo plano y son idempotentes. |
| O3 | Los operadores pueden aplicar y eliminar manifiestos de Kubernetes arbitrarios (con lista de tipos permitidos) desde el Desk. | Un DocType `Service Bundle` valida los manifiestos contra una lista fija de tipos de recursos integrados y los aplica / elimina mediante jobs en segundo plano usando server-side apply. |
| O4 | Los operadores pueden crear, migrar, hacer copias de seguridad, restaurar y eliminar sitios Frappe en un bench ERPNext en ejecución desde el Desk. | Un DocType `Frappe Site` (vinculado a un `Helm Release`) y un DocType `Frappe Site Backup` orquestan el ciclo de vida mediante Jobs de Kubernetes y verifican los resultados mediante sondas de fuente de verdad del bench en lugar de confiar en los códigos de salida del Job. |
| O5 | El plano de control respeta los invariantes de ingeniería de plataformas normalmente asociados a las herramientas de bucle de reconciliación: el estado deseado y el estado observado nunca se confunden, todo el trabajo que muta el clúster se ejecuta fuera del hilo de solicitud, y los workers concurrentes u obsoletos no pueden corromper el estado. | La ruta de código de descubrimiento no persiste nada en MariaDB; todas las escrituras en el clúster se enrutan a través de `frappe.enqueue(... queue="long")`; todos los workers en segundo plano llevan tokens de operación por ejecución que se recomprueban antes de cualquier escritura de estado; un bucle de reconciliación programado se ejecuta cada 5 minutos. |

---

## 4. Metodología

### 4.1 Decisión arquitectónica

La decisión arquitectónica definitoria es la separación estricta del **estado deseado** (DocTypes de Frappe respaldados por MariaDB) del **estado observado** (consultas en vivo contra el clúster). Cada decisión de diseño posterior — descubrimiento de solo lectura, mutaciones exclusivamente mediante jobs en segundo plano, verificación de la fuente de verdad — se deriva de esta decisión. Véase [`docs/architecture.md`](architecture.md) para la justificación completa y los diagramas C4.

### 4.2 Proceso

La implementación siguió un ciclo iterativo orientado a capacidades:

1. Implementar la versión mínima viable de una capacidad (por ejemplo, "desplegar una release de Helm") de extremo a extremo, incluyendo el DocType, la tarea en segundo plano y el comportamiento del formulario.
2. Someter a prueba de estrés dicha capacidad bajo condiciones imperfectas del clúster (fallos de workers, workers obsoletos, fallos transitorios de sondas, Jobs con TTL expirado, pods colgados, PVCs faltantes) y reforzarla.
3. Registrar la decisión arquitectónica y las alternativas rechazadas en [`CHANGELOG.md`](../CHANGELOG.md).
4. Pasar a la siguiente capacidad.

Esto produjo un producto cuya superficie de capacidades (conectividad de clúster → releases de Helm → manifiestos sin procesar → sitios Frappe → copias de seguridad → herramientas de operador) es más amplia que el alcance inicial, manteniendo al mismo tiempo cada capacidad lo suficientemente robusta como para poder defenderse por sí sola. El inventario actual de capacidades y robustez se encuentra en [`docs/control-plane-state.md`](control-plane-state.md).

### 4.3 Estrategia de verificación

- **Tests unitarios / de integración** se ejecutan en CI en cada PR (`bench --site test_site run-tests --app kubeport`). El repositorio cuenta con 18 módulos de test que cubren descubrimiento, transiciones de estado de reconciliación, guardias de concurrencia del worker de Helm, validación de manifiestos, guardias de copia de seguridad/restauración, escenarios de simulación del ciclo de vida completo y disponibilidad de carga de trabajo por tipo.
- **Procedimientos de verificación funcional** para comportamientos que no pueden afirmarse de forma creíble mediante tests con mocks (por ejemplo, `bench new-site` contra un bench real, recolección de basura en cascada por `ownerReference`, suficiencia de RBAC). El procedimiento actual está documentado en [`docs/operator-guide.md`](operator-guide.md); la versión histórica está archivada en [`docs/history/frappe-site-smoke.md`](history/frappe-site-smoke.md).
- **Registro de decisiones arquitectónicas** en [`CHANGELOG.md`](../CHANGELOG.md) documenta qué se decidió, por qué y qué se rechazó, de modo que cada decisión de diseño sea auditable.

---

## 5. Resultados frente a objetivos

| # | Objetivo | Estado | Evidencia |
|---|---|---|---|
| O1 | Conectividad de clúster (3 modos de autenticación) | **Cumplido** | El DocType `Kubernetes Cluster` implementa autenticación por kubeconfig, token bearer y en clúster; la importación de kubeconfig desde el navegador normaliza los endpoints del servidor API solo locales; el endpoint de prueba de conexión ejercita la API real del clúster. |
| O2 | Ciclo de vida de releases de Helm | **Cumplido** | DocTypes `Helm Repository`, `Helm Chart`, `Helm Release`; catálogo de charts sincronizado diariamente; despliegue / actualización / reversión / desinstalación se ejecutan mediante jobs en segundo plano; `helm upgrade --install` idempotente; la salud de la release en vivo combina el estado de Helm con la disponibilidad de la carga de trabajo a partir de `helm get manifest`. |
| O3 | Despliegue de manifiestos sin procesar | **Cumplido (dentro del alcance)** | `Service Bundle` valida los manifiestos contra una lista de 17 tipos de recursos integrados y los aplica / elimina mediante server-side apply. Los CRDs y los recursos personalizados arbitrarios quedan fuera del alcance por diseño. |
| O4 | Ciclo de vida del sitio Frappe | **Cumplido** | `Frappe Site` orquesta `bench new-site`, `bench drop-site`, `bench migrate`, `bench backup`, `bench restore` mediante Jobs de Kubernetes clonados desde un pod de carga de trabajo bench en vivo; `Frappe Site Backup` es un DocType independiente para que los metadatos de copia de seguridad puedan sobrevivir a la fila del sitio fuente; las sondas de fuente de verdad del bench verifican los resultados en lugar de confiar en los códigos de salida del Job. |
| O5 | Invariantes de ingeniería de plataformas | **Cumplido** | La ruta de código de descubrimiento es de solo lectura y nunca persiste en MariaDB; cada mutación del clúster pasa por `frappe.enqueue(... queue="long")`; los workers en segundo plano llevan tokens de operación / sincronización por ejecución que se recomprueban antes de cualquier escritura de estado; un bucle de reconciliación de 5 minutos detecta la deriva y recupera operaciones obsoletas; un barrido de Jobs huérfanos elimina los Jobs a los que ninguna fila hace referencia. |

Los números empíricos que respaldan cada afirmación de "Cumplido" se resumen en §6 y se desarrollan completamente en [`docs/evaluation.md`](evaluation.md).

La tabla de propiedades de robustez en [`docs/control-plane-state.md`](control-plane-state.md) enumera las defensas específicas implementadas para cada invariante, incluyendo:

- tokens de operación por ejecución con recomprobación antes de las escrituras,
- aislamiento del kubeconfig por llamada en el wrapper del subproceso de Helm,
- validación de identidad del Job mediante la etiqueta `kubeport.io/frappe-site` antes de cualquier escritura de estado,
- sonda de fuente de verdad mediante pod-exec para la existencia del sitio,
- auxiliar `<archivo>.size` del lado del PVC para la verificación de la finalización de la copia de seguridad,
- puertas de confirmación tipadas para forzar la desinstalación y la cancelación destructiva,
- clasificador de disponibilidad de carga de trabajo compartido entre los workers de despliegue y la reconciliación.

---

## 6. Evaluación

Esta sección es el resumen empírico por objetivo de la evaluación. Cada fila empareja un objetivo de §3 con el número medido que sustenta la afirmación de "Cumplido" correspondiente en §5; la columna de fuente apunta al informe JSON bajo [`eval/results/`](../eval/results/) que generó el número. El capítulo completo — metodología, tablas por eje, MTTR por fallo, regresión de escala — se encuentra en [`docs/evaluation.md`](evaluation.md).

| # | Objetivo | Resultado medido | Fuente |
|---|---|---|---|
| O1 | Conectividad de clúster (3 modos de autenticación) | La fase de ruta dorada `setup_cluster_doc` supera la prueba de extremo a extremo contra un clúster k3d real, ejercitando la ruta de autenticación por kubeconfig a través del DocType `Kubernetes Cluster`. | [`eval/results/sample.json`](../eval/results/sample.json) (`make eval`) |
| O2 | Ciclo de vida de releases de Helm | Las fases de ruta dorada `setup_helm_repo`, `verify_chart`, `create_release`, `deploy_release` superan todas la prueba; la release alcanza `Deployed` y permanece en ese estado durante el resto de la ejecución. | [`eval/results/sample.json`](../eval/results/sample.json) (`make eval`) |
| O3 | Despliegue de manifiestos sin procesar (Service Bundle) | Los casos de transición de estado de aplicar / eliminar Service Bundle superan la prueba en la suite de tests unitarios. No forma parte del conjunto de pruebas de ruta dorada orientado al operador. | `kubeport/tests/test_reconciliation.py` |
| O4 | Ciclo de vida del sitio Frappe | Las fases de ruta dorada `create_site`, `migrate_site`, `backup_site`, `restore_site`, `drop_site` superan todas la prueba; las sondas de fuente de verdad del bench verifican cada transición. | [`eval/results/sample.json`](../eval/results/sample.json) (`make eval`) |
| O5 | Invariantes de ingeniería de plataformas | Las cuatro defensas de robustez documentadas se recuperan dentro del límite de operación obsoleta de 30 minutos (1800 s): `worker_kill_mid_helm_upgrade` MTTR 2,30 s, `job_ttl_expired_before_reconcile` 6,68 s, `pod_exec_timeout_during_site_probe` 9,63 s (1 ciclo diferido), `corrupt_archive_size_sidecar` 22,34 s con el archivo eliminado del PVC. El ciclo de reconciliación escala de forma sublineal hasta 1000 filas por tipo (media 10,46 s, pendiente de ley potencial ≈ 0,745) — aproximadamente 28 veces por debajo de la cadencia programada de 5 minutos. La línea de base comparativa (`kubectl` + `helm` sin procesar, mismo flujo de trabajo) requiere 24 comandos de shell distintos y 13 intervenciones del operador; Kubeport elimina ambos. | [`eval/results/sample-faults.json`](../eval/results/sample-faults.json) (`make eval-faults`); [`eval/results/sample-scaling.json`](../eval/results/sample-scaling.json) (`make eval-scaling`); [`eval/results/sample-baseline.json`](../eval/results/sample-baseline.json) y [`eval/results/comparison.md`](../eval/results/comparison.md) (`make eval-baseline`). |

Cada estado de "Cumplido" en §5 está ahora anclado a una medición en esta tabla o en el capítulo al que enlaza; la formulación anterior afirmaba el cumplimiento sin evidencia.

---

## 7. Limitaciones

Estas son decisiones deliberadas de alcance, no errores:

- **El descubrimiento de sitios Frappe se limita al chart oficial `erpnext`.** Ampliar la cobertura a otras variantes de chart bench es trabajo de diseño, no de implementación.
- **`Service Bundle` solo admite tipos de recursos integrados de Kubernetes.** Los CRDs, los recursos personalizados arbitrarios y las consideraciones sobre webhooks de admisión quedan fuera del alcance.
- **Los benches respaldados por Postgres no están soportados.** El campo `db_type` está bloqueado en `mariadb`; las rutas de código de postgres fueron eliminadas intencionalmente (sin compatibilidad hacia adelante).
- **El alcance del registro de imágenes de sitio es GHCR público.** No se expone ninguna interfaz de `imagePullSecret` en v1.
- **El almacenamiento de copias de seguridad son PVCs locales al namespace.** Las copias de seguridad programadas mediante cron y la retención por recuento/antigüedad por sitio se incluyeron en v1; los backends de almacenamiento de objetos, el cifrado en reposo, la restauración entre clústeres y la restauración con un nombre de sitio diferente quedan fuera del alcance.
- **Una guía de despliegue narrativa se incluye en v1** ([`docs/deploy.md`](deploy.md)) que cubre la topología, el fragmento de empaquetado con Helm-CLI, la matriz RBAC de verbos-recursos, las líneas de base de límites de recursos, los endpoints de métricas internas y el procedimiento de copia de seguridad del plano de control; el árbol RBAC de mínimos privilegios con kustomize en [`deploy/rbac/`](../deploy/rbac/) se valida mediante `make rbac-smoke`.

---

## 8. Trabajo futuro

El trabajo restante es de profundización; la base principal está en su lugar.

1. **Modelado de salud más amplio**: añadir sondas HTTP a nivel de aplicación y salud con conciencia de CRD donde las señales tienen semántica clara.
2. **Mayor profundidad en copias de seguridad**: las copias de seguridad programadas mediante cron y la retención por recuento/antigüedad por sitio se incluyeron en v1; el trabajo restante son los backends de almacenamiento de objetos, el cifrado en reposo, la restauración entre clústeres y la restauración con un nombre de sitio diferente (esto último bloqueado por la necesidad de un modelo de ruta de almacenamiento más rico).
3. **Mayor compatibilidad de charts**: expansión controlada del descubrimiento de bench más allá de `erpnext`.
4. **Tests de integración entre DocTypes**: mayor cobertura de flujos de trabajo para complementar la sólida cobertura por módulo ya existente.

---

## 9. Bibliografía

Las referencias se enumeran en orden de primera cita en §2 y están vinculadas a entradas BibTeX en [`docs/references.bib`](references.bib). El listado sigue el estilo numérico IEEE.

[1] B. Philips, "Introducing Operators: Putting Operational Knowledge into Software," CoreOS Engineering Blog, 2016.

[2] The Kubernetes Authors, "Operator Pattern," Kubernetes Project Documentation.

[3] B. Burns, B. Grant, D. Oppenheimer, E. Brewer, and J. Wilkes, "Borg, Omega, and Kubernetes," *ACM Queue*, vol. 14, no. 1, 2016.

[4] K. Hightower, B. Burns, and J. Beda, *Kubernetes: Up and Running*, 3rd ed. O'Reilly Media, 2022.

[5] A. Verma, L. Pedrosa, M. Korupolu, D. Oppenheimer, E. Tune, and J. Wilkes, "Large-scale cluster management at Google with Borg," in *Proc. 10th European Conf. on Computer Systems (EuroSys)*, 2015.

[6] OpenGitOps Working Group, "OpenGitOps Principles v1.0.0," Cloud Native Computing Foundation, 2021.

[7] W. Vogels, "Eventually Consistent," *Communications of the ACM*, vol. 52, no. 1, pp. 40–44, 2009.

[8] E. Brewer, "CAP Twelve Years Later: How the 'Rules' Have Changed," *IEEE Computer*, vol. 45, no. 2, pp. 23–29, 2012.

[9] The Frappe Authors, "Frappe Framework Documentation," Frappe Technologies.

[10] J. Dean and S. Ghemawat, "MapReduce: Simplified Data Processing on Large Clusters," *Communications of the ACM*, vol. 51, no. 1, pp. 107–113, 2008.

[11] The Helm Authors, "Helm: The Package Manager for Kubernetes," Helm Project Documentation.

[12] L. Lamport, "Time, Clocks, and the Ordering of Events in a Distributed System," *Communications of the ACM*, vol. 21, no. 7, pp. 558–565, 1978.

---

## 10. Documentos del repositorio

| Documento | Función |
|---|---|
| [`README.md`](../README.md) | Punto de entrada orientado al usuario e inicio rápido. |
| [`docs/architecture.md`](architecture.md) | Diagramas C4 de contexto / contenedor / componente, diagramas de secuencia, invariantes de diseño. |
| [`docs/operator-guide.md`](operator-guide.md) | Cómo usar Kubeport de extremo a extremo. |
| [`docs/control-plane-state.md`](control-plane-state.md) | Inventario de capacidades y robustez; brechas abiertas. |
| [`docs/codebase-summary.md`](codebase-summary.md) | Referencia de arquitectura a nivel de módulo. |
| [`docs/evaluation.md`](evaluation.md) | Capítulo de evaluación empírica — funcional, fiabilidad, línea de base, escala. |
| [`docs/references.bib`](references.bib) | Bibliografía BibTeX que respalda el listado de §9. |
| [`AGENTS.md`](../AGENTS.md) | Invariantes y patrones de implementación autorizados. |
| [`CHANGELOG.md`](../CHANGELOG.md) | Registro de decisiones arquitectónicas (qué / por qué / alternativas rechazadas). |
| [`CONTRIBUTING.md`](../CONTRIBUTING.md) | Configuración del entorno de desarrollo, flujo de trabajo de lint y tests. |
| [`SECURITY.md`](../SECURITY.md) | Política de divulgación de seguridad. |
| [`docs/history/`](history/) | Documentos de planificación archivados conservados para la trazabilidad de la tesis. |

---

## 11. Componentes del proyecto

Esta tesis describe el artefacto backend (Kubeport). El entregable completo del TFG comprende tres repositorios:

| Componente | Repositorio | Función |
|---|---|---|
| Backend / plano de control | [`EsderJ10/kubeport`](https://github.com/EsderJ10/kubeport) (este repositorio) | Aplicación Frappe, el artefacto técnico que describe este documento. |
| Landing page de marketing | [`1DAW-victorjim551/lp-KubePort`](https://github.com/1DAW-victorjim551/lp-KubePort) | Sitio público, desplegado en [`1daw-victorjim551.github.io/lp-KubePort`](https://1daw-victorjim551.github.io/lp-KubePort/). Desarrollado por Víctor Jiménez. |
| Paraguas del proyecto | [`EsderJ10/tfg`](https://github.com/EsderJ10/tfg) | Contenedor de desarrollo, notas de diseño, gestor de tareas. |