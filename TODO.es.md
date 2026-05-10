# Hoja de ruta de Kubeport — Tesis 9/10 → 10/10

> Lista de tareas ordenada por ejecución para agentes de IA. Cada elemento es autocontenido: un agente nuevo puede retomarlo desde cero a partir de los ficheros enlazados. Lee primero el **Preámbulo del agente**.
> Cuando termines una tarea, márcala como `✅ DONE <YYYY-MM-DD> — <commit/PR>` en su lugar; no elimines la entrada.

---

## Preámbulo del agente (leer antes de tocar nada)

### Invariantes estrictos (de `AGENTS.md` y `CLAUDE.md`)

1. **Estado deseado → MariaDB. Estado observado → consultas en vivo al clúster. Nunca mezclarlos.**
2. **Todas las mutaciones del clúster van a través de `frappe.enqueue(..., queue="long", enqueue_after_commit=True)`.** Nunca llamar a las APIs mutantes de Helm/K8s desde el hilo web.
3. **El descubrimiento es de solo lectura.** Nunca persistir el estado descubierto.
4. **Concurrencia**: rotar `operation_token` / `sync_token` por ejecución; verificar de nuevo antes de cualquier escritura de estado. Nunca usar `doc.reload()` en un worker — usar `frappe.db.get_value` / `db_set`.
5. **Renderizado de formularios**: datos externos del clúster mediante `frappe.xcall` + renderizado en el cliente, nunca en `doc.onload`.
6. **Alcance de clúster**: construir clientes mediante `get_k8s_api_client(cluster_name)`; sin estado global.
7. **Anotaciones de tipo en todos los métodos de API públicos** (`require_type_annotated_api_methods = True`).

### Estilo
- Tabuladores, comillas dobles, 110 columnas, `ruff format` para Python, `prettier` para JS/CSS.
- Solo ASCII salvo que el fichero ya contenga caracteres no ASCII.
- Sin comentarios por defecto; solo cuando el **porqué** no sea evidente.

### Cadena de herramientas
- **Bench se ejecuta dentro del contenedor de desarrollo, NO en el host.** Los comandos `bench` fallan en el host. El repositorio hermano en `~/workspace/school/tfg` contiene el contenedor de desarrollo; el contenedor suele estar parado entre sesiones.
- **No instalar herramientas localmente.** Sin `pip install`, `brew`, `apt`. Usar el linter del contenedor:
  - `make fmt` — formatear en local
  - `make lint` — reportar
  - `make fix` — corregir automáticamente y formatear
  - `make lint-check` — ejecución exacta en modo CI
- Pre-commit debe instalarse dentro del checkout de bench: `cd apps/kubeport && pre-commit install`.
- Tests: `bench --site <site> run-tests --app kubeport --doctype <DocType>` — solo dentro del contenedor de desarrollo.

### Higiene de Git
- **Mensajes de commit convencionales, en una sola línea. Sin cuerpo. Sin tráiler `Co-Authored-By`.** Seguir el estilo del `git log` existente.
- Los nombres de rama siguen el esquema: `fix/`, `feat/`, `chore/`, `refactor/`, `docs/`, `style/`, `ci/`, `test/`.
- Nunca usar amend; crear siempre un commit nuevo.
- Nunca usar `--no-verify`.

### Política de documentación
- Si un cambio afecta al descubrimiento, las tareas o la semántica de estado, actualizar el documento correspondiente en `docs/` **en el mismo commit**.
- Mapa de documentos: `README.md` (entrada), `AGENTS.md` (invariantes), `CONTRIBUTING.md` (desarrollo), `SECURITY.md` (divulgación), `docs/architecture.md` (C4 + invariantes), `docs/operator-guide.md` (flujos de trabajo), `docs/control-plane-state.md` (inventario de capacidades y brechas), `docs/codebase-summary.md` (referencia por módulo), `docs/thesis.md` (marco del proyecto), `CHANGELOG.md` (registro de decisiones).

---

## P0 — ESTABILIZACIÓN (bloquea todos los hitos posteriores)

### TODO-01 — `fix/k8s-client-py314-drift` ✅ DONE 2026-05-10 — dcdb6a3

**Objetivo**: Hacer que `kubeport/tests/test_k8s_client.py` pase con Python 3.14 + el cliente `kubernetes` actual.

**Contexto**: La sección "Próximos pasos" de `docs/control-plane-state.md` lista esto como un fallo preexistente: `test_k8s_client (Python 3.14 / kubernetes-client API call signature drift)`. No se puede presentar la defensa de tesis con un test en rojo en master.

**Enfoque**:
1. Ejecutar el test dentro del contenedor de desarrollo: `bench --site test_site run-tests --app kubeport --module kubeport.tests.test_k8s_client`.
2. Leer el fallo. Probablemente un kwarg renombrado o una firma reordenada entre versiones del cliente consumido por `kubeport/utils/k8s_client.py` (180 líneas, fichero único).
3. **Corregir el consumidor (`utils/k8s_client.py`)**, no el test, salvo que el test afirme un comportamiento que el nuevo cliente haya cambiado legítimamente — en ese caso, actualizar la aserción y añadir un comentario indicando el cambio en upstream.
4. Volver a ejecutar la suite completa de reconciliación para confirmar que no hay regresiones: `bench --site test_site run-tests --app kubeport`.

**Criterios de aceptación**:
- `test_k8s_client` pasa en local y en CI (`.github/workflows/ci.yml`).
- Sin nuevos fallos en otras partes.
- `make lint-check` sin errores.

**Ficheros**: `kubeport/utils/k8s_client.py`, `kubeport/tests/test_k8s_client.py`.

---

### TODO-02 — `fix/reconcile-service-bundles-test` ✅ DONE 2026-05-10 — a592928

**Objetivo**: Hacer que el test fallido de reconciliación de service bundles pase.

**Contexto**: La misma línea de `control-plane-state.md` lista `test_reconcile_service_bundles` como pendiente de investigación, independientemente del trabajo sobre Frappe Site.

**Enfoque**:
1. Ejecutar: `bench --site test_site run-tests --app kubeport --module kubeport.tests.test_reconciliation` y aislar el caso fallido.
2. Diagnosticar la causa raíz. La reconciliación de Service Bundle se encuentra en `kubeport/tasks/reconciliation.py` (1925 líneas — buscar `service_bundle` / `reconcile_service_bundles`). La comprobación de existencia de recursos está en `kubeport/utils/k8s_resources.py`.
3. Corregir el problema subyacente. No silenciar el test, no añadir `@skip`, no debilitar la aserción.

**Criterios de aceptación**:
- Todos los tests de `test_reconciliation.py` pasan en CI.
- Causa raíz documentada en el cuerpo del mensaje de commit (una línea: p. ej., `fix: service bundle reconcile mishandles 404 from custom resource list`).

**Ficheros**: `kubeport/tasks/reconciliation.py`, `kubeport/utils/k8s_resources.py`, `kubeport/tests/test_reconciliation.py`.

---

### TODO-03 — `chore/strip-known-failures-disclaimer` ✅ DONE 2026-05-10 — 61eca03

**Objetivo**: Eliminar el aviso de "fallos de test preexistentes" una vez que TODO-01 y TODO-02 estén fusionados.

**Enfoque**:
- Editar `docs/control-plane-state.md`: eliminar la línea `6. **Pre-existing test failures**: ...` bajo `## Next Steps`.
- Sin otros cambios en la documentación; los commits de TODO-01/02 ya cubrieron el código.

**Criterios de aceptación**:
- Sin mención alguna a "fallos de test preexistentes" en `docs/`.
- CI en verde.

**Depende de**: TODO-01, TODO-02.

**Ficheros**: `docs/control-plane-state.md`.

---

## P1 — CONJUNTO DE EVALUACIÓN (el capítulo empírico que aporta la mayor parte del delta 9→10)

### TODO-04 — `feat/eval-harness` ✅ DONE 2026-05-10 — 65d7c8f

**Objetivo**: Un conjunto de evaluación de extremo a extremo reproducible que arranca un clúster k3d efímero y ejecuta el flujo de trabajo dorado de Kubeport, emitiendo un informe legible por máquina.

**Por qué importa**: El §5 de `docs/thesis.md` afirma actualmente "Cumplido" para cada objetivo sin evidencia cuantitativa. Este conjunto es la fuente de los números que poblarán el §6 Evaluación.

**Alcance**:
1. Nuevo directorio de primer nivel: `eval/`.
2. `eval/Makefile` (o extensión del `Makefile` raíz con `make eval`) que:
   - Arranca un clúster k3d limpio (si no hay uno ya presente, reutilizará el site bench del contenedor de desarrollo para el propio Kubeport).
   - Ejecuta el flujo dorado a través de la API REST/Frappe de Kubeport (usar `frappe.client.get_list` etc. por HTTP), o mediante un helper `bench execute`, **no** simulando clics:
     1. Crear una fila `Kubernetes Cluster` apuntando al clúster k3d.
     2. Crear una fila `Helm Repository` para el repositorio curado `frappe/erpnext`.
     3. Disparar la sincronización de charts; esperar hasta que aparezcan filas `Helm Chart`.
     4. Crear una `Helm Release` para ERPNext usando la `Kubeport Site Image` curada.
     5. Esperar hasta que la release alcance el estado `Deployed`.
     6. Crear un `Frappe Site` vinculado a la release; esperar hasta `Active`.
     7. Disparar `bench migrate`; esperar hasta volver a `Active`.
     8. Disparar una copia de seguridad; esperar a que la fila `Frappe Site Backup` alcance `Available`.
     9. Disparar la restauración desde esa copia de seguridad; esperar hasta `Active`.
     10. Eliminar el site; confirmar la eliminación de la fila.
   - Tiempo de reloj de pared registrado por fase.
   - Emite `eval/results/<utc-timestamp>.json` con `{ phase, started_at, finished_at, duration_seconds, status }` por fase y un resumen global.
3. Un `eval/results/sample.json` de referencia incluido en el repositorio con una ejecución correcta para citar en la tesis.
4. `eval/README.md` explicando la configuración (versión de k3d, requisitos previos), el flujo dorado y cómo interpretar el JSON.

**Restricciones**:
- Debe ejecutarse dentro del contenedor de desarrollo existente; sin instalaciones en el host (ver Preámbulo).
- Debe poder reutilizarse; al volver a ejecutarse produce un nuevo informe con marca de tiempo; nunca sobreescribe informes anteriores.
- No debe requerir acceso de red a registros privados — usa únicamente la imagen de site pública curada en GHCR.

**Criterios de aceptación**:
- `make eval` desde un contenedor de desarrollo limpio produce un informe JSON cuya cada fase tiene `status: "passed"`.
- `eval/results/sample.json` está incluido en el repositorio y coincide con el esquema documentado en `eval/README.md`.
- Tiempo total de ejecución < 25 min en un portátil de desarrollador estándar.

**Ficheros**: nuevo árbol `eval/`, `Makefile` raíz.

---

### TODO-05 — `feat/eval-fault-injection` ✅ DONE 2026-05-10 — 4ee574b/752d1b8/53cbfb4/f97de5f

> Nota de cierre: Los cuatro escenarios pasan bajo `make eval-faults`
> (avance rápido) — ver `eval/results/sample-faults.json`. Una muestra
> en tiempo real (sin `--fast-forward`) es el seguimiento natural: TODO-04
> ya cubre el fixture de modo reutilización, y el trabajo restante es una
> ejecución de ~35 min con `make eval-faults-real`. El andamiaje también
> descubrió un bug latente (`fix: stale helm reconciliation reads
> values field via dict subscript not attribute`, c9cb8be), que es
> exactamente para lo que sirve la inyección de fallos.

**Objetivo**: Validar empíricamente las defensas de robustez listadas en `docs/control-plane-state.md` §Propiedades de robustez.

**Depende de**: TODO-04 (reutiliza el conjunto de evaluación).

**Alcance**: Implementar estos cuatro escenarios como scripts `eval/faults/<nombre>.py` ejecutados por `make eval-faults`:

| Escenario | Inyección | Comportamiento esperado | Medición |
|---|---|---|---|
| `worker_kill_mid_helm_upgrade` | `kill -9` al worker RQ largo tras invocar `helm upgrade --install` pero antes de que el estado vuelva a escribirse | El reconciliador de operaciones obsoletas recupera la fila en menos de 30 min; estado final `Deployed`; el hash de especificación coincide con el estado deseado | MTTR (inicio de inyección → fila alcanza `Deployed`) |
| `job_ttl_expired_before_reconcile` | Forzar el borrado del Job de operación antes de que el tick de reconciliación lo lea | La reconciliación recurre a la sonda ground-truth de bench; la fila alcanza el estado terminal correcto, no `Failed` por defecto | Estado final, tiempo hasta terminal |
| `pod_exec_timeout_during_site_probe` | Inyectar un sleep en la llamada pod-exec que envuelve `bench list-apps` | `_probe_site_state` devuelve `unknown`; la fila permanece en `In Progress` durante ese tick; el siguiente tick recupera | Número de ticks diferidos; estado final |
| `corrupt_archive_size_sidecar` | Truncar el fichero `<archive>.size` a mitad de vuelo en el PVC `kubeport-backups` | La sonda PVC devuelve `missing`/`unknown`; la fila de backup se marca como `Failed`, se ejecuta la limpieza de archivos basura, el fichero de archivo se elimina del PVC | Estado final, presencia del fichero de archivo en el PVC |

**Salida**: `eval/results/faults-<utc-timestamp>.json` con `{ scenario, injected_at, recovered_at, mttr_seconds, expected, observed, passed }` por fila.

**Criterios de aceptación**:
- Los cuatro escenarios pasan de extremo a extremo.
- MTTR para `worker_kill_mid_helm_upgrade` ≤ 30 min (coincide con la ventana de recuperación de operaciones obsoletas documentada).
- `eval/README.md` actualizado con una tabla de "Escenarios de fallo" que mapea cada escenario al invariante defendido en `docs/control-plane-state.md`.

**Ficheros**: `eval/faults/`, `eval/results/`, `eval/README.md`, `Makefile` raíz (añadir objetivo `eval-faults`).

---

### TODO-06 — `feat/eval-baseline-comparison` ✅ DONE 2026-05-10 — 4ad9a4b

**Objetivo**: Comparación lado a lado del mismo flujo de trabajo ejecutado con `kubectl + helm` puro frente a Kubeport. Valida la afirmación sobre el estado del arte en `docs/thesis.md` §2.

**Depende de**: TODO-04.

**Alcance**:
1. `eval/baseline/run.sh` — script bash que realiza el mismo flujo de 10 pasos con `kubectl` y `helm` puros (sin Kubeport).
2. Registra: tiempo de reloj de pared total por fase, comandos de shell distintos emitidos, intervenciones manuales requeridas (contadas como `manual_steps`).
3. Emite `eval/results/baseline-<timestamp>.json` siguiendo el esquema del conjunto de evaluación más los campos `commands_issued` y `manual_steps`.
4. `eval/baseline/README.md` documenta la metodología de comparación — mismo clúster objetivo, mismo chart, misma imagen, sin Kubeport en absoluto.

**Criterios de aceptación**:
- `make eval-baseline` produce un informe JSON.
- `eval/results/sample-baseline.json` incluido en el repositorio.
- Un `eval/results/comparison.md` resume Kubeport frente a la línea base en una tabla Markdown (fase, segundos-baseline, segundos-kubeport, comandos, pasos-manuales).

**Ficheros**: `eval/baseline/`, `eval/results/`, `Makefile` raíz.

---

### TODO-07 — `feat/eval-scaling` ✅ DONE 2026-05-10 — c4bbc37

**Objetivo**: Caracterizar cómo escala Kubeport con N filas persistidas.

**Depende de**: TODO-04.

**Alcance**:
1. `eval/scaling/seed.py` — crea en masa N filas sintéticas de Helm Release / Service Bundle / Frappe Site en estado `Deployed` / `Active` sin tocar un clúster real (usar un fixture de clúster simulado para las rutas de lectura). Usar `frappe.db.bulk_insert` para evitar la sobrecarga de hooks.
2. Ejecutar la reconciliación N ∈ {1, 10, 100, 1000} veces, medir la latencia de tick.
3. Muestrear la latencia del subproceso helm desde los logs existentes a lo largo de la ejecución del conjunto de evaluación (TODO-04) — extraer mediante un postprocesador de logs estructurados en `eval/scaling/extract_latencies.py`.
4. Emitir `eval/results/scaling.json` y representar gráficamente en `eval/results/scaling-tick-latency.png` usando matplotlib (matplotlib ya está disponible en el contenedor de desarrollo — confirmar antes de añadir una dependencia).

**Criterios de aceptación**:
- `make eval-scaling` produce JSON + PNG.
- La forma de crecimiento de la latencia de tick (lineal, log-lineal) está anotada en el campo `regression` del JSON.

**Ficheros**: `eval/scaling/`, `Makefile` raíz.

---

### TODO-08 — `docs/thesis-evaluation-chapter` ✅ DONE 2026-05-10 — 72e4fdd

**Objetivo**: Un capítulo real de "Evaluación" en la tesis basado en los resultados de TODO-04..07.

**Depende de**: TODO-04, TODO-05, TODO-06, TODO-07.

**Alcance**:
1. Nuevo `docs/evaluation.md` — capítulo completo con subsecciones: Funcional, Fiabilidad bajo inyección de fallos, Línea base comparativa, Envolvente de escalado. Cada subsección cita el `eval/results/*.json` del que extrae los datos.
2. Actualizar `docs/thesis.md`:
   - Insertar §6 "Evaluación" entre el §5 actual y el §6 (renumerar Limitaciones → §7, Trabajo Futuro → §8, Referencias → §9).
   - El nuevo §6 es una tabla resumen de 1 página por objetivo citando el número medido; la discusión completa vive en `docs/evaluation.md`.
3. Actualizar el mapa de documentación de `README.md` y el índice de documentos de `CLAUDE.md` para incluir `docs/evaluation.md`.

**Criterios de aceptación**:
- Cada afirmación cuantitativa en `docs/evaluation.md` cita una ruta bajo `eval/results/`.
- Las afirmaciones "Cumplido" del §5 de `docs/thesis.md` ahora referencian las mediciones del §6 en lugar de afirmarlo sin evidencia.

**Ficheros**: `docs/evaluation.md`, `docs/thesis.md`, `README.md`, `CLAUDE.md`.

---

## P2 — ENCUADRE FORMAL Y DE SEGURIDAD (mejoras baratas para la tesis)

### TODO-09 — `docs/formal-invariants` ✅ DONE 2026-05-10 — 790fd0b

**Objetivo**: Reformular los cuatro invariantes de diseño como propiedades numeradas de Seguridad / Vivacidad / Consistencia eventual con testigos a nivel de código.

**Alcance**:
1. Reescribir `docs/architecture.md` §3 ("Invariantes clave de diseño"). Reemplazar cada invariante en prosa con una propiedad numerada de la forma:
   - `P1 (Seguridad)`: <enunciado> — Testigo: `<fichero>:<línea>`.
   - `P2 (Vivacidad)`: <enunciado> — Testigo: `<fichero>:<línea>`.
   - `P3 (Consistencia eventual)`: <enunciado> — Testigo: `<fichero>:<línea>`.
2. Nuevo `docs/fault-model.md`:
   - Fallos tolerados enumerados (caída del worker, expiración de TTL del Job antes de reconciliar, fallo transitorio de pod-exec, colisión de hash en nombres de Job, bloqueo de operación obsoleta, Job huérfano, timeout de API de kubeconfig).
   - Para cada uno: mecanismo de defensa (rotación de token, `activeDeadlineSeconds`, sonda de tres estados, validación de etiquetas, reconciliador de operaciones obsoletas, barrido de huérfanos), testigo a nivel de código y límite superior de tiempo de recuperación.
3. Enlace cruzado desde `docs/architecture.md` y `AGENTS.md`.

**Criterios de aceptación**:
- Cada propiedad en `architecture.md` §3 tiene un testigo `<fichero>:<línea>` que se resuelve en `HEAD`.
- `docs/fault-model.md` enumera ≥ 7 fallos, cada uno con un testigo y un límite superior de recuperación documentado.

**Ficheros**: `docs/architecture.md`, `docs/fault-model.md` (nuevo), `AGENTS.md`, `README.md` (mapa de documentos).

---

### TODO-10 — `docs/threat-model` ✅ DONE 2026-05-10 — e9b1735

**Objetivo**: Un análisis completo de fronteras de confianza. Actualmente existe `SECURITY.md` pero no hay mapa de fronteras.

**Alcance**:
1. Nuevo `docs/threat-model.md` con:
   - **Diagrama de fronteras de confianza** (Mermaid): Operador → Web Frappe → Worker RQ → Subproceso Helm → Kubeconfig en reposo → API del clúster → Pod-exec de Bench.
   - **Tabla de fronteras**: por cada frontera, identificar la dirección de confianza, los datos que la cruzan y las mitigaciones (CSRF en endpoints públicos, cifrado del kubeconfig en reposo mediante campos cifrados de Frappe, cliente K8s con alcance, RBAC en la cuenta de servicio dentro del clúster, tipos de recursos permitidos en Service Bundle, comandos permitidos en Kubernetes Command).
   - **Catálogo de amenazas** (STRIDE por frontera, abreviado).
   - **Justificación de la lista de permitidos de eliminación en `Kubernetes Command`** (solo Pod, Job, ConfigMap — explicar por qué se excluyen Secret/PVC/Deployment/StatefulSet).
2. Enlace cruzado desde `SECURITY.md`.

**Criterios de aceptación**:
- Cada endpoint público (`kubeport/api/*.py`) y cada llamada de worker privilegiada (`kubeport/tasks/*.py`) aparece en la tabla de fronteras.
- El catálogo STRIDE tiene ≥ 1 entrada por frontera, con mitigación enlazada al código.

**Ficheros**: `docs/threat-model.md` (nuevo), `SECURITY.md`, `README.md` (mapa de documentos).

---

### TODO-11 — `docs/sota-bibliography` ✅ DONE 2026-05-10 — 5f38eea

**Objetivo**: Reemplazar la tabla de 6 filas de productos en `docs/thesis.md` §2 con una sección real de estado del arte en investigación CS + bibliografía.

**Alcance**:
1. Ampliar `docs/thesis.md` §2:
   - Conservar la tabla de comparación de productos existente.
   - Añadir subsecciones en prosa: "Patrón operador y bucles de reconciliación", "Estado deseado frente a estado observado en sistemas declarativos", "Patrones de ejecución en segundo plano en plataformas de negocio".
   - Cada subsección cita fuentes primarias de la bibliografía.
2. Nuevo `docs/references.bib` (BibTeX) con **al menos 12 referencias primarias**, conjunto sugerido:
   - Burns et al., "Borg, Omega, and Kubernetes", ACM Queue 2016.
   - Verma et al., "Large-scale cluster management at Google with Borg", EuroSys 2015.
   - Brewer, "Kubernetes: The Surprisingly Affordable Platform for Global Companies".
   - Hightower, Burns, Beda, "Kubernetes Up & Running" (capítulo de controladores).
   - Propuesta de diseño de Helm 3 (repositorio `helm/community`).
   - Libro blanco de GitOps (Weaveworks).
   - Lamport, "Time, Clocks, and the Ordering of Events" (consistencia eventual).
   - Vogels, "Eventually Consistent" CACM 2009.
   - Brewer, teorema CAP.
   - Documentación del framework Frappe (URL canónica).
   - Dean & Ghemawat, "MapReduce" (justificación de la descomposición del trabajo en segundo plano).
   - El manifiesto del controlador K8s / artículo sobre el patrón operador (Red Hat / CoreOS).
3. Añadir `docs/thesis.md` §9 "Bibliografía" listando las referencias en estilo IEEE o ACM.

**Criterios de aceptación**:
- ≥ 12 entradas en `docs/references.bib`.
- Cada cita en `docs/thesis.md` se resuelve en una entrada `.bib`.
- Las subsecciones en prosa del §2 de `docs/thesis.md` citan cada una ≥ 2 referencias.

**Ficheros**: `docs/thesis.md`, `docs/references.bib` (nuevo), `README.md` (mapa de documentos).

---

## P3 — DIFERENCIADORES DE NIVEL CARRERA CS

### TODO-12 — `test/property-fsm-frappe-site` ✅ DONE 2026-05-10 — a1ac296

**Objetivo**: Tests de propiedades basados en Hypothesis para la máquina de estados finitos de `Frappe Site`.

**Contexto**: La máquina de estados está documentada en `docs/control-plane-state.md` §Aprovisionamiento de Frappe Site: `Draft → In Progress → Active | Failed`, más `Active → Migrating → Active | Failed` y `Active|Failed → Deleting → [doc eliminado] | Failed`. Los tests de propiedades convierten esto de prosa en un invariante verificado.

**Alcance**:
1. Nuevo `kubeport/tests/test_property_fsm_frappe_site.py`.
2. Usar `hypothesis.stateful.RuleBasedStateMachine`. Reglas: `create`, `cancel`, `migrate`, `backup`, `restore`, `drop`, `tick_reconciliation`. Usar clientes K8s simulados (fixtures existentes en `kubeport/tests/`) para que el test sea hermético.
3. Invariantes:
   - `inv_no_terminal_inflight`: tras una secuencia finita que termina en `tick_reconciliation`, la fila nunca está simultáneamente en un estado en vuelo y con `operation_token` rotado por un worker concurrente.
   - `inv_transitions_documented`: cada transición observada aparece en la lista de flechas documentada (codificar las flechas como una constante en el test).
   - `inv_archive_outlives_site`: eliminar un `Frappe Site` que tiene un `Frappe Site Backup` en estado `Available` deja la fila de backup intacta.
4. Ejecutar ≥ 1000 ejemplos en CI (establecer `@settings(max_examples=1000)`).

**Criterios de aceptación**:
- El test pasa con `max_examples=1000` en `.github/workflows/ci.yml`.
- Los tres invariantes están verificados.
- Hypothesis se añade a las dependencias de desarrollo, no de ejecución — confirmar que `pyproject.toml` no lo incluye en la instalación.

**Ficheros**: `kubeport/tests/test_property_fsm_frappe_site.py` (nuevo), `pyproject.toml`.

---

### TODO-13 — `test/property-fsm-helm-release` ✅ DONE 2026-05-10 — f7c9130

**Objetivo**: Igual que TODO-12, para la FSM de Helm Release.

**Contexto**: `Draft → In Progress → Deployed | Degraded | Failed → Uninstalling → Draft` (`docs/control-plane-state.md` §Gestión de Helm Release).

**Alcance**:
1. Nuevo `kubeport/tests/test_property_fsm_helm_release.py`.
2. Reglas: `deploy`, `upgrade`, `rollback`, `uninstall`, `force_uninstall`, `tick_reconciliation`.
3. Invariantes:
   - `inv_dependent_site_blocks_uninstall`: una desinstalación mientras un `Frappe Site` vinculado está `Active` siempre se bloquea salvo que `force=True`.
   - `inv_failed_cannot_be_directly_deleted`: eliminar una fila `Failed` fuera de `Draft` se rechaza.
   - `inv_spec_hash_monotonic`: `last_applied_spec_hash` solo cambia tras un deploy/upgrade/rollback exitoso.

**Criterios de aceptación**:
- Igual que TODO-12: 1000 ejemplos, todos los invariantes verificados, hermético.

**Ficheros**: `kubeport/tests/test_property_fsm_helm_release.py` (nuevo).

**Depende de**: TODO-12 (compartir el cableado de dependencia de desarrollo de Hypothesis)

---

### TODO-14 — `feat/internal-observability` ✅ DONE 2026-05-10 — 4f477e0

**Objetivo**: Hacer que Kubeport sea medible internamente. Contadores e histogramas expuestos en el panel de control del operador existente, más logs estructurados enlazados por ID de correlación.

**Alcance**:
1. Nuevo módulo `kubeport/utils/metrics.py`:
   - Contadores en memoria (locales al proceso) para `reconcile_ticks_total`, `stale_ops_recovered_total`, `orphan_jobs_swept_total`.
   - Un tipo de histograma pequeño para la latencia del subproceso helm, consultable por percentil.
   - Opcional: persistir un agregado diario en una nueva doctype `Kubeport Metric Sample` si encaja — solo si no viola la separación deseado/observado (no la viola: es el estado observado interno de Kubeport, no del clúster). Aplazar si alarga el hito.
2. Conectar los contadores al bucle de reconciliación existente en `kubeport/tasks/reconciliation.py` y al wrapper del subproceso helm en `kubeport/utils/helm.py`.
3. Añadir un parámetro `correlation_id` (UUID4) generado en el momento de encolar, propagado a través de los kwargs de `frappe.enqueue`, incluido en cada línea de log emitida por el worker mediante un helper estilo `frappe.logger().bind`.
4. Exponer las métricas en el espacio de trabajo del operador existente (`kubeport/kubeport/workspace/...`) como nuevas tarjetas numéricas que leen de `kubeport/api/dashboard.py`.

**Restricciones**:
- No añadir Prometheus ni dependencias externas de métricas. Mantenerlo nativo de Frappe.
- No violar el invariante deseado/observado: cualquier métrica persistida es metadato sobre el propio Kubeport, nunca sobre el clúster.

**Criterios de aceptación**:
- El log de `frappe.local` de una operación contiene el mismo `correlation_id` desde web → encolar → worker.
- El espacio de trabajo del operador muestra contadores en vivo que se incrementan bajo carga.
- Los tests en `kubeport/tests/test_api_dashboard.py` ampliados para cubrir las nuevas métricas.

**Ficheros**: `kubeport/utils/metrics.py` (nuevo), `kubeport/tasks/reconciliation.py`, `kubeport/utils/helm.py`, `kubeport/api/dashboard.py`, `kubeport/kubeport/workspace/`, `kubeport/tests/test_api_dashboard.py`.

---

### TODO-15 — `feat/chaos-ci` ✅ DONE 2026-05-10 — 68c6224

**Objetivo**: Un job de CI que demuestre empíricamente la recuperación bajo un fallo realista.

**Depende de**: TODO-04 (conjunto de evaluación), TODO-05 (escenarios de fallo).

**Alcance**:
1. Nuevo `.github/workflows/chaos.yml`:
   - Arranca k3d en un runner alojado en GitHub.
   - Inicia un bench Frappe v16 (reutilizar el patrón del job `test` en `.github/workflows/ci.yml`).
   - Ejecuta el escenario `worker_kill_mid_helm_upgrade` de TODO-05.
   - Afirma que la fila Helm Release alcanza `Deployed` dentro de la ventana de recuperación de operaciones obsoletas documentada.
2. Estado requerido en PR (tras una ejecución en verde; publicar con `continue-on-error: true` en la primera iteración, siguiendo el patrón del CHANGELOG `2026-05-09 — Limpieza de calidad de código y gating de tests en CI`).

**Criterios de aceptación**:
- El flujo de trabajo se ejecuta en PR.
- Un escenario pasa de extremo a extremo.
- Documentado en `docs/evaluation.md` como base de la sección de inyección de fallos.

**Ficheros**: `.github/workflows/chaos.yml` (nuevo), `docs/evaluation.md`.

---

## P4 — PREPARACIÓN PARA PRODUCCIÓN

### TODO-16 — `docs/deploy-guide` ✅ DONE 2026-05-10 — 5b2ac82

**Objetivo**: Guía desplegable por el operador. `docs/control-plane-state.md` §Brechas abiertas "Documentación del operador" lo lista como ausente.

**Alcance**:
1. Nuevo `docs/deploy.md` que cubre:
   - Topología: Kubeport dentro del clúster frente a Kubeport fuera del clúster, cuándo elegir cada opción.
   - Configuración de autenticación dentro del clúster con la SA / Role / RoleBinding incluida en TODO-17.
   - Empaquetado del binario Helm (fragmento de Dockerfile que añade `helm` a una imagen de bench Frappe; la matriz de wheels de kubernetes-client en Python 3.14).
   - Límites de recursos (recomendaciones de CPU/RAM para web, worker RQ largo, planificador).
   - Monitorización: dónde recoger las métricas de TODO-14.
   - Copia de seguridad del plano de control: cómo hacer copia de seguridad del site Frappe que ejecuta el propio Kubeport.
2. Enlace cruzado desde la sección de instalación de `README.md`.

**Criterios de aceptación**:
- Un revisor puede seguir `docs/deploy.md` de extremo a extremo en un clúster nuevo sin consultar documentación externa.
- Incluye una sección "Verificación rápida" que apunta a `make eval` (TODO-04).

**Ficheros**: `docs/deploy.md` (nuevo), `README.md`, `CLAUDE.md` (mapa de documentos).

---

### TODO-17 — `feat/rbac-manifests` ✅ DONE 2026-05-10 — 032106e

**Objetivo**: Publicar manifiestos Kubernetes RBAC de mínimo privilegio para el modo de autenticación dentro del clúster.

**Alcance**:
1. Nuevo `deploy/rbac/`:
   - `serviceaccount.yaml` — SA `kubeport` en el namespace donde se ejecuta Kubeport.
   - `role.yaml` / `clusterrole.yaml` — verbos mínimos que Kubeport realmente necesita (auditar `kubeport/utils/k8s_resources.py` y `kubeport/tasks/*.py` para las llamadas API reales; no conceder permisos en exceso).
   - `rolebinding.yaml` / `clusterrolebinding.yaml`.
   - `kustomization.yaml` para `kubectl apply -k deploy/rbac/`.
2. `deploy/rbac/README.md` documentando:
   - Qué verbos se concedieron y por qué (citar el punto de llamada API para cada uno).
   - Con alcance de namespace frente a alcance de clúster: preferir el de namespace donde sea posible; alcance de clúster solo para el descubrimiento de `Namespace`, `StorageClass`, y la aplicación de `ClusterRole/ClusterRoleBinding` en Service Bundle.
3. Verificación: `make rbac-smoke` ejecuta `kubectl auth can-i` para cada punto de llamada contra la SA vinculada.

**Criterios de aceptación**:
- `kubectl apply -k deploy/rbac/` es suficiente para que un bench Kubeport recién creado en el mismo clúster opere con `auth_mode: in_cluster`.
- El README tiene una matriz de justificación de verbos (verbo, recurso, ruta de justificación).

**Ficheros**: árbol `deploy/rbac/` (nuevo), `Makefile` raíz (añadir `rbac-smoke`).

**Depende de**: TODO-16 (la guía de despliegue referenciará estos manifiestos).

---

## P5 — FUNCIONALIDADES EN PROFUNDIDAD (elegir 1–2; demostrar cierre vale más que listar cinco aspiraciones)

Estas cierran brechas ya nombradas en `docs/control-plane-state.md` §Brechas abiertas. **Elige una o dos**, hazlas bien, lista las demás como trabajo futuro.

### TODO-18 — `feat/helm-diff-preview` ✅ DONE 2026-05-10 — 84c9f56

**Objetivo**: Un botón "Vista previa" en el formulario `Helm Release` que muestra la diferencia entre el estado en vivo y el siguiente renderizado antes de desplegar.

**Alcance**:
1. Nuevo endpoint público `kubeport.api.helm_diff.preview_release(name: str) -> dict`.
2. Implementación: renderizar valores mediante la maquinaria existente de `kubeport/tasks/helm_tasks.py`, llamar a `helm template` (solo lectura, permitido desde el hilo web porque es solo local — confirmar leyendo la documentación de la CLI de Helm: `helm template` no contacta el clúster), luego comparar con la salida de `helm get manifest`. Usar un pequeño helper de diff en Python, sin nueva dependencia de sistema.
3. Interfaz del formulario: botón junto a Desplegar que abre un panel mostrando la diferencia.

**Criterios de aceptación**:
- La vista previa reporta una diferencia vacía tras un despliegue exitoso cuando no cambian los valores.
- Tests añadidos en `kubeport/tests/test_helm_tasks.py` cubriendo cambios solo de valores y cambios de versión de chart.

**Ficheros**: `kubeport/api/helm_diff.py` (nuevo), `kubeport/kubeport/doctype/helm_release/helm_release.{js,py}`, `kubeport/tests/test_helm_tasks.py`.

---

### TODO-19 — `feat/site-health-surface` ✅ DONE 2026-05-10 — 57f8aea

**Objetivo**: Reflejar el panel de observabilidad de Helm Release en `Frappe Site`.

**Contexto**: Cierra la brecha en `docs/control-plane-state.md` §Brechas abiertas "sin superficie de salud por Frappe Site".

**Alcance**: Logs de pods con alcance de site, eventos con alcance, preparación del pod bench — reutilizando helpers de `kubeport/utils/observability.py` (576 líneas, ya diseñados para reutilización con alcance de recurso).

**Criterios de aceptación**: El formulario de site renderiza tres subvistas (logs, eventos, rollout) que respetan el mismo modelo de antigüedad/autenticación que el panel de Helm Release existente.

---

### TODO-20 — `feat/scheduled-backups` ✅ DONE 2026-05-10 — a1f1d26

**Objetivo**: Copias de seguridad programadas por el operador mediante cron en `Frappe Site`.

**Alcance**: Nuevo campo de cadena cron en `Frappe Site`; el tick de reconciliación encola una nueva fila `Frappe Site Backup` cuando el cron es debido. Integrar con retención (máximo N filas o antigüedad máxima).

**Criterios de aceptación**: Un site con `backup_schedule = "0 2 * * *"` produce una nueva copia de seguridad `Available` al día; las copias antiguas más allá de la retención se eliminan automáticamente (lo que ya limpia el archivo en el PVC según la entrada del CHANGELOG `2026-05-04`).

---

## Higiene de mantenimiento (ejecutar antes de cada commit)

```bash
make fmt           # formatear
make lint-check    # ejecución exacta en modo CI
# solo dentro del contenedor de desarrollo:
bench --site test_site run-tests --app kubeport
```

Si alguno de estos falla, corregir antes de hacer commit. Nunca usar `--no-verify`.