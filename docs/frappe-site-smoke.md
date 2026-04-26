# Frappe Site Provisioning — Smoke Test Procedure

Unit tests in this repo are fully mocked. Before merging a change that affects
Frappe Site provisioning (or before cutting a release), run through this
checklist against a real cluster and record the result in the PR description.

## Prerequisites

- A Kubernetes cluster (`kind`, `k3d`, or any real cluster). 1 CPU / 2 GiB is
  enough for the bench itself; `bench new-site --install-app=erpnext` wants
  closer to 2 CPU / 4 GiB.
- An ERPNext Helm release installed via `Helm Release` in Kubeport, running
  and reporting `Deployed`.
- `kubectl` access to that cluster for out-of-band observation.

## Scenarios

Each scenario is a single human-observable check. Run them in order — they
share the same bench release.

### 1. Happy path

1. Create a `Frappe Site` with `site_name = smoke-1.example.com`,
   `bench_release = <your-erpnext-release>`, `install_apps = erpnext`,
   `admin_password` = anything, `db_root_password` or `db_root_secret` set.
2. Click **Create Site**.
3. Observe `status` transitions `Draft → In Progress → Active` within 10 min.
4. From `kubectl`, confirm the Job was created and cleaned up:
   ```
   kubectl get jobs -n <bench-ns> -l kubeport.io/frappe-site=<release-slug>-smoke-1.example.com
   ```
5. Exec into the bench pod and confirm the site is usable:
   ```
   kubectl exec -n <bench-ns> <scheduler-pod> -- bench --site smoke-1.example.com list-apps
   ```
   Exit code must be 0.

**Expected**: Row is `Active`, site is reachable, creds Secret is gone (GC'd
with the Job).

### 2. Recreate (Force)

1. On the `smoke-1.example.com` row (now `Active`), tick **Force Create**, save.
2. Click **Recreate Site (Force)**.
3. Confirm a new Job appears (different name — token-suffix differs).
4. Confirm the old creds Secret is gone and a new one exists, then is GC'd
   when the new Job finishes.
5. Row should transition back to `Active`.

**Expected**: New Job name, new Secret, end-state `Active`, old Secret
eventually removed.

### 3. Cancel during `In Progress`

1. Create a second site `smoke-2.example.com` with `install_apps = erpnext,hrms`
   (slow install).
2. While status is `In Progress` (Job running), click **Cancel Creation**.
3. Confirm the Job disappears within a few seconds:
   ```
   kubectl get job -n <bench-ns> <job-name>    # NotFound
   ```
4. Confirm the creds Secret is also gone.

**Expected**: Row is `Failed` with `status_detail = "Cancelled by ..."`, no
cluster resources remain.

### 4. Delete-while-`In Progress`

1. Create `smoke-3.example.com` with `install_apps = erpnext`.
2. While status is `In Progress`, delete the `Frappe Site` row.
3. Confirm the Job and creds Secret are gone within a few seconds.

**Expected**: No stranded Job or Secret.

### 5. Worker-crash recovery (orphan-Job sweep)

This is the primary check for AC-M2 of the audit plan.

1. Start a site create. In a parallel shell, SIGKILL the RQ worker process
   immediately after the Job is applied to the cluster but before the row's
   `creation_job_name` is populated. (Easiest repro: set a breakpoint / sleep
   after `apply_resource(job_manifest)` in a dev image, then kill the worker.)
2. Observe:
   - Row remains `In Progress` with empty `creation_job_name`.
   - Labeled Job exists in-cluster:
     ```
     kubectl get jobs -n <bench-ns> -l app.kubernetes.io/managed-by=kubeport,kubeport.io/frappe-site
     ```
3. Wait up to 5 minutes for the reconciler tick.

**Expected**: `_sweep_orphan_site_jobs` deletes the orphan Job (and, via
ownerRef GC, the creds Secret). Operator log contains
`Sweeping orphan Frappe Site Job '...'`.

### 6. TTL-expired recovery

1. Temporarily lower `_JOB_TTL_SECONDS` to 60 (`kubeport/tasks/site_tasks.py`)
   and lower the scheduler cron to every 30 seconds (`hooks.py`). Restart
   bench.
2. Create `smoke-6.example.com`. Wait for the Job to complete **and** for TTL
   cleanup (Job disappears) **before** the next reconciler tick reads it.
3. Let the reconciler run.

**Expected**: Reconciler's 404 branch fires and the row transitions to
`Active` via the ground-truth probe (or `Failed` if the site genuinely does
not exist).

### 7. activeDeadlineSeconds (hung pod)

1. Create a site against a bench whose DB is unreachable (e.g. stop the
   MariaDB pod). The Job pod will loop on DB connect.
2. Wait for `_JOB_ACTIVE_DEADLINE_SECONDS` (30 min default).

**Expected**: K8s terminates the Job with a `DeadlineExceeded` condition,
`job.status.failed = 1`. The next reconciler tick transitions the row to
`Failed` with pod-log detail.

### 8. Transient bench restart during reconciliation

1. Create `smoke-8.example.com`. During its `In Progress`, trigger a rolling
   restart of the bench Deployment (so pods are briefly unavailable).
2. While the rollout is in progress, the reconciler should attempt the bench
   probe on the Job-failed or 404 branch.

**Expected**: The probe returns `SITE_PROBE_UNKNOWN` and the row is **not**
finalized this tick. On the next tick after the rollout completes, it
transitions to `Active` or `Failed` based on ground truth.

## What cannot be credibly asserted from the unit tests

- Kubernetes admission accepting the emitted Job manifest. Unit tests only
  check the dict shape.
- `bench new-site --force` behavior across Frappe major versions.
- Real `ownerReference` cascade GC on Job delete.
- RBAC sufficiency for the control-plane service account.

These all require the procedure above on a real cluster.

## Reporting

In the PR description, note:

- Cluster kind (kind / k3d / EKS / etc.) and K8s version.
- ERPNext chart version and bench image tag.
- Which scenarios passed and any deviation.
- If Postgres support is re-enabled later: add a scenario 9 mirroring
  scenario 1 against a Postgres-backed bench.
