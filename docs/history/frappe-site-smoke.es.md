# Frappe Site Provisioning — Procedimiento de Smoke Test

Los tests unitarios de este repo estan totalmente mockeados. Antes de fusionar un cambio que
afecte a la provision de Frappe Site (o antes de sacar una release), recorre esta checklist
contra un cluster real y registra el resultado en la descripcion del PR.

## Requisitos previos

- Un cluster de Kubernetes (`kind`, `k3d` o cualquier cluster real). 1 CPU / 2 GiB basta para
  el bench; `bench new-site --install-app=erpnext` pide algo mas cercano a 2 CPU / 4 GiB.
- Un Helm release de ERPNext instalado via `Helm Release` en Kubeport, en ejecucion y con
  estado `Deployed`.
- Acceso `kubectl` a ese cluster para observacion externa.

## Escenarios

Cada escenario es una comprobacion observable por una persona. Ejecutalos en orden: comparten
el mismo bench release.

### 1. Camino feliz

1. Crea un `Frappe Site` con `site_name = smoke-1.example.com`,
   `bench_release = <your-erpnext-release>`, `install_apps = erpnext`,
   `admin_password` = cualquier cosa, `db_root_password` o `db_root_secret` configurado.
2. Haz clic en **Create Site**.
3. Observa que `status` pasa de `Draft → In Progress → Active` en menos de 10 min.
4. Desde `kubectl`, confirma que el Job se creo y luego fue limpiado:
   ```
   kubectl get jobs -n <bench-ns> -l kubeport.io/frappe-site=<release-slug>-smoke-1.example.com
   ```
5. Entra en el pod del bench y confirma que el site es usable:
   ```
   kubectl exec -n <bench-ns> <scheduler-pod> -- bench --site smoke-1.example.com list-apps
   ```
   El codigo de salida debe ser 0.

**Esperado**: la fila queda en `Active`, el site responde, y el Secret de credenciales desaparece
(GC junto con el Job).

### 2. Recrear (Force)

1. En la fila `smoke-1.example.com` (ahora `Active`), marca **Force Create** y guarda.
2. Haz clic en **Recreate Site (Force)**.
3. Confirma que aparece un Job nuevo (nombre distinto: cambia el sufijo del token).
4. Confirma que el Secret viejo de credenciales desaparece y que aparece uno nuevo, y luego se
   borra por GC cuando termina el nuevo Job.
5. La fila debe volver a `Active`.

**Esperado**: nuevo nombre de Job, nuevo Secret, estado final `Active`, y el Secret antiguo se
elimina con el tiempo.

### 3. Cancelar durante `In Progress`

1. Crea un segundo site `smoke-2.example.com` con `install_apps = erpnext,hrms`
   (instalacion lenta).
2. Mientras el estado sea `In Progress` (Job en ejecucion), haz clic en **Cancel Creation**.
3. Confirma que el Job desaparece en unos pocos segundos:
   ```
   kubectl get job -n <bench-ns> <job-name>    # NotFound
   ```
4. Confirma que el Secret de credenciales tambien desaparece.

**Esperado**: la fila queda en `Failed` con `status_detail = "Cancelled by ..."`, y no queda
ningun recurso en el cluster.

### 4. Borrar mientras esta `In Progress`

1. Crea `smoke-3.example.com` con `install_apps = erpnext`.
2. Mientras el estado sea `In Progress`, borra la fila de `Frappe Site`.
3. Confirma que el Job y el Secret de credenciales desaparecen en unos pocos segundos.

**Esperado**: no quedan Job ni Secret colgados.

### 5. Recuperacion tras crash del worker (barrido de Job huerfano)

Esta es la comprobacion principal para AC-M2 del plan de auditoria.

1. Inicia la creacion de un site. En una shell paralela, envia SIGKILL al proceso del worker RQ
   justo despues de que el Job se aplique al cluster pero antes de que se rellene el
   `creation_job_name` de la fila. (La reproduccion mas simple: pon un breakpoint / sleep despues
   de `apply_resource(job_manifest)` en una imagen de desarrollo y luego mata el worker.)
2. Observa:
   - La fila sigue en `In Progress` con `creation_job_name` vacio.
   - Existe un Job etiquetado en el cluster:
     ```
     kubectl get jobs -n <bench-ns> -l app.kubernetes.io/managed-by=kubeport,kubeport.io/frappe-site
     ```
3. Espera hasta 5 minutos al tick del reconciler.

**Esperado**: `_sweep_orphan_site_jobs` elimina el Job huerfano (y, via GC de ownerRef, el Secret
de credenciales). El log del operador contiene `Sweeping orphan Frappe Site Job '...'`.

### 6. Recuperacion de TTL expirado

1. Baja temporalmente `_JOB_TTL_SECONDS` a 60 (`kubeport/tasks/site_tasks.py`) y el cron del
   scheduler a cada 30 segundos (`hooks.py`). Reinicia bench.
2. Crea `smoke-6.example.com`. Espera a que el Job termine **y** a que la limpieza por TTL
   (el Job desaparece) ocurra **antes** de que el siguiente tick del reconciler lo lea.
3. Deja correr el reconciler.

**Esperado**: se ejecuta la rama 404 del reconciler y la fila pasa a `Active` mediante la
proba de verdad real (o `Failed` si el site realmente no existe).

### 7. `activeDeadlineSeconds` (pod colgado)

1. Crea un site contra un bench cuya DB no sea alcanzable (por ejemplo, para el pod de MariaDB).
   El pod del Job quedara en bucle intentando conectarse a la DB.
2. Espera `_JOB_ACTIVE_DEADLINE_SECONDS` (30 min por defecto).

**Esperado**: K8s termina el Job con una condicion `DeadlineExceeded`, y
`job.status.failed = 1`. El siguiente tick del reconciler pasa la fila a `Failed` con detalle del
log del pod.

### 8. Reinicio transitorio del bench durante la reconciliacion

1. Crea `smoke-8.example.com`. Durante su estado `In Progress`, lanza un rolling restart del
   Deployment del bench (asi los pods quedan brevemente no disponibles).
2. Mientras el rollout esta en curso, el reconciler deberia intentar la proba del bench en la
   rama de Job fallido o 404.

**Esperado**: la proba devuelve `SITE_PROBE_UNKNOWN` y la fila **no** se finaliza en este tick.
En el siguiente tick, cuando el rollout termine, pasa a `Active` o `Failed` segun la verdad real.

## Lo que no se puede afirmar con credibilidad desde los tests unitarios

- Que Kubernetes admita el manifest de Job emitido. Los tests unitarios solo comprueban la forma
  del dict.
- El comportamiento de `bench new-site --force` entre versiones mayores de Frappe.
- La GC real en cascada de `ownerReference` al borrar un Job.
- La suficiencia de RBAC para el service account del control plane.

Todo eso requiere el procedimiento anterior sobre un cluster real.

## Informe

En la descripcion del PR, anota:

- Tipo de cluster (kind / k3d / EKS / etc.) y version de K8s.
- Version del chart de ERPNext y tag de la imagen del bench.
- Que escenarios pasaron y cualquier desviacion.
- Si luego se reactivase soporte para Postgres: agrega un escenario 9 que replique el escenario 1
  contra un bench respaldado por Postgres.
