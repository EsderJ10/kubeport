# Política de seguridad

## Comunicar una vulnerabilidad

Si crees haber encontrado una vulnerabilidad de seguridad en Kubeport, **no abras un issue público en GitHub**. En su lugar, envía un informe privado al mantenedor del proyecto:

- Email: `jcorfer910@g.educaand.es`
- Asunto: `[Kubeport security] <descripción breve>`

Por favor, incluye:

- Una descripción clara de la vulnerabilidad.
- Una reproducción (comandos, operaciones de DocType, fragmentos de manifiesto o condiciones del clúster).
- La versión afectada (SHA del commit o etiqueta).
- El impacto observado y cualquier mitigación sugerida.

Puedes esperar un acuse de recibo en pocos días hábiles. Se prefiere la divulgación coordinada — por favor, da al mantenedor un plazo razonable para publicar una corrección antes de hacer públicos los detalles.

## Alcance

En alcance:

- La propia aplicación Frappe de Kubeport (paquete `kubeport/`, hooks, DocTypes, endpoints de API, tareas en segundo plano, trabajos programados).
- El flujo de trabajo de publicación de imágenes de site (`.github/workflows/publish-site-image.yml`) y el script de catálogo (`scripts/update_site_catalog.py`).
- El catálogo predeterminado de imágenes de site (`kubeport/site_images/catalog.json`).

Fuera de alcance:

- Vulnerabilidades en Frappe / ERPNext, el cliente Python de Kubernetes, la CLI de Helm u otras dependencias — repórtalas a sus respectivos mantenedores.
- Configuración incorrecta del clúster en el entorno propio del operador (RBAC, políticas de red, credenciales de extracción de imágenes).
- Denegación de servicio mediante acciones legítimamente autenticadas de System Manager.

## Modelo de confianza y fronteras conocidas

Para el catálogo STRIDE por frontera, el mapeo endpoint × frontera y la justificación de la lista de operaciones destructivas permitidas, consulta [`docs/threat-model.md`](docs/threat-model.md).

Kubeport está diseñado para ser operado por usuarios con el rol `System Manager` de Frappe. Las siguientes son fronteras de confianza deliberadas que los operadores deben conocer:

- **Las credenciales del clúster** almacenadas en las filas de `Kubernetes Cluster` (contenido del kubeconfig, tokens de portador, certificados CA) residen en MariaDB. Cualquier persona que pueda leer esas filas desde la base de datos puede actuar como el clúster.
- **La autenticación por token de portador** sin certificado CA se rechaza por defecto. El bypass TLS exclusivo para desarrollo está explícitamente nombrado como "solo para desarrollo" — nunca lo actives en producción.
- **Las operaciones de Helm** son ejecutadas por un binario `helm` externo al proceso en el host de bench. Comprometer ese binario o su `PATH` compromete el plano de control.
- **`Service Bundle`** valida los manifiestos contra una lista fija de tipos de recursos integrados. Los CRDs y los recursos personalizados arbitrarios no están soportados de forma intencionada.
- **`Kubernetes Command`** es el único doctype que expone operaciones ad-hoc sobre el clúster. La eliminación está restringida a `Pod`, `Job` y `ConfigMap`; los cambios destructivos sobre `Secret`, `PVC`, `Deployment` y `StatefulSet` deben pasar por sus controladores dedicados.
- **`Kubernetes Command Audit Log`** es de solo adición y sobrevive a la eliminación de filas. Rótalo y archívalo según tu política de retención.
- **Las filas del catálogo de imágenes de site** pueden ser `is_curated=1` (gestionadas por la sincronización diaria del catálogo) o `is_curated=0` (registradas por el usuario). El conjunto curado está anclado por digest. Las filas de usuario pueden crearse sin digest y desplegarse por etiqueta — esa es una elección explícita del operador.
- **Las credenciales de Frappe Site** fluyen hacia Kubernetes a través de Secrets por Job referenciados como propietarios del Job (de modo que se eliminan con el TTL del Job). Nunca se escriben como variables de entorno en texto plano en la especificación del Pod.

## Recomendaciones de hardening

- Restringe el rol `System Manager` únicamente a los operadores autorizados a gestionar los clústeres conectados.
- Ejecuta el bench bajo una cuenta de servicio de Kubernetes dedicada cuyo RBAC sea el mínimo requerido por las tareas de Kubeport (releases de Helm, Jobs en los namespaces objetivo, los tipos de recursos de la lista permitida de `Service Bundle`, exec en pods de bench).
- Monta únicamente los kubeconfigs / tokens que necesite el bench; no coloques credenciales de clústeres no relacionados en MariaDB.
- Mantén el binario de Helm en `PATH` anclado a una versión conocida; considera empaquetarlo con la imagen de bench para evitar la deriva en la cadena de suministro.
- Monitoriza el `Kubernetes Command Audit Log` en busca de actividad de eliminación inesperada.