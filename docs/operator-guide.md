# Kubeport — Operator Guide

This guide walks an operator (a user holding the `System Manager` role in Frappe) through every workflow Kubeport supports, in the order an operator typically performs them.

For installation see the [`README.md`](../README.md). For the architecture behind these workflows see [`docs/architecture.md`](architecture.md). For invariants and patterns see [`AGENTS.md`](../AGENTS.md).

---

## 0. Prerequisites

- A running Frappe / ERPNext bench with the `kubeport` app installed (see the README install steps).
- Helm 3 on the bench host's `PATH`.
- Network reachability from the bench host (or pod) to the Kubernetes API server you plan to register.
- Operator's Frappe user has the `System Manager` role.

All workflows below assume the operator has the Kubeport workspace open in the Desk (`/app/kubeport-operations`).

---

## 1. Connect a Kubernetes Cluster

Pick the auth mode that matches your environment:

| Mode | When to use |
|---|---|
| **kubeconfig** | You already have a working kubeconfig on your laptop and want to import it through the browser. |
| **bearer token** | A central platform team issues a bearer token and a CA certificate for the cluster. |
| **in-cluster** | The bench itself runs inside the target cluster (the service-account token is auto-mounted). |

### 1.1 Steps (kubeconfig mode)

1. Open **Kubernetes Cluster** → **New**.
2. Fill in `Cluster Name` and switch `Auth Method` to `kubeconfig`.
3. Click the kubeconfig file picker and select your local kubeconfig. The browser parses contexts and lists them; pick one.
4. If the picked context's `server:` is `127.0.0.1`, `0.0.0.0`, or `localhost`, Kubeport rewrites it to the bench container's default gateway IP automatically (otherwise the bench cannot reach the API server). The original endpoint is shown alongside the rewritten one.
5. Optionally toggle **Skip TLS verification** for self-signed dev clusters. **Never** enable this in production.
6. Save.
7. Click **Test Connection**. The form posts to the live API and shows a success / failure message.

### 1.2 Steps (bearer token mode)

1. As above, but choose `Auth Method = bearer token`.
2. Fill in the API server URL, the bearer token, and the CA certificate.
3. The CA certificate is required unless **Skip TLS verification** is toggled — bearer-token without a CA is not allowed by default.
4. Save and **Test Connection**.

### 1.3 Steps (in-cluster mode)

1. Choose `Auth Method = in-cluster`.
2. Save. There are no other fields — Kubeport reads the pod-mounted service-account token at use time.

---

## 2. Register a Helm Repository

1. Open **Helm Repository** → **New**.
2. Fill in `Repository Name` (used as the Helm `helm repo add` name) and `URL`.
3. Optionally set `Include Patterns` (comma-separated globs) to filter which charts are indexed — useful when a repo ships hundreds of charts and you only want a handful.
4. Save.
5. The first save enqueues a chart sync onto the `long` queue. Watch the row's `Sync Status` field; it transitions to `Synced` when the background worker finishes.
6. Subsequent syncs run daily on the scheduler. You can also force a sync from the form.

`Helm Chart` and `Helm Chart Version` rows appear automatically as the sync runs. Their `default_values` cache is refreshed when the latest version of a chart changes.

---

## 3. Deploy a Helm Release

1. Open **Helm Release** → **New**.
2. Pick the target `Cluster`, `Namespace`, and `Release Name`. The triple is the row's identity — matches real Helm scope.
3. Pick `Chart` from the synced catalogue. The form fetches the chart's default `values.yaml` for reference.
4. Edit `Values` (YAML). The form validates the YAML on save.
5. **(For ERPNext / Frappe charts)** Pick a `Site Image` from the catalogue (or leave it empty to use the chart's default image). When a Site Image is selected:
   - `image.repository`, `image.tag`, and `image.pullPolicy` are injected into Helm values.
   - When the Site Image has a recorded digest, the tag is rendered as `tag@sha256:…` so Kubernetes pulls the exact image bytes.
   - Manual `values.image.*` overrides are rejected to keep the source of truth unambiguous.
6. Save.
7. Click **Deploy**. The row transitions `Draft → In Progress → Deployed | Degraded | Failed`.
8. The form loads workload-readiness asynchronously: each unready row opens an in-place observability panel with three views — pod logs, scoped Kubernetes events, and rollout context for Deployment / StatefulSet / DaemonSet.

### 3.1 Upgrade

Edit values or chart version, save, click **Deploy** again. Same idempotent `helm upgrade --install` runs through a background worker. The row's `pending_changes` flag is set whenever the desired spec differs from the last successful apply.

### 3.2 Rollback

The form exposes the live Helm release history. Pick a revision and click **Rollback**. A successful rollback updates the saved desired values and chart version to match the selected live revision (so the row no longer shows pending changes).

### 3.3 Uninstall

Click **Uninstall**. Normal uninstall is **blocked** when a linked `Frappe Site` row is in an active or in-flight state — uninstalling the bench while a site exists would orphan bench-side data.

For the operator-override path, click **Force Uninstall** and type the typed-confirmation string. This bypasses the linked-site guard.

A `Failed` Helm Release cannot be deleted directly (it might still own cluster resources); uninstall is the supported cleanup path.

### 3.4 Ingress (Frappe charts)

By default the Frappe/ERPNext chart deploys with `ingress.enabled=false`, so the release is only reachable from inside the cluster (`kubectl port-forward` for ad-hoc access). The Helm Release form exposes structured ingress fields that render the chart's `ingress.*` values for you:

- **Enable Ingress** — toggles the rendering. When unchecked the form fields have no effect.
- **Hostname** — required when ingress is enabled. Becomes `ingress.hosts[0].host`. The path is hard-coded to `/` with `pathType: ImplementationSpecific`. If Kubeport sees a LoadBalancer ingress-controller IP, it suggests `<release-name>.<ip>.nip.io` for local/dev clusters without a real domain.
- **Ingress Class** — `ingress.className`. Kubeport suggests the cluster's default `IngressClass`, or the only detected class when there is exactly one. Leave blank to let the cluster choose its default.
- **cert-manager ClusterIssuer** — optional. Kubeport suggests a detected ready `ClusterIssuer` when cert-manager is present. When set, Kubeport renders the `cert-manager.io/cluster-issuer` annotation and a `tls` block referencing secret `<release-name>-tls` (cert-manager creates the secret on first reconcile). Leave blank for HTTP-only.

These fields apply only to Frappe charts (matched by chart name containing "frappe" or "erpnext"). For other charts, configure ingress via the raw `Values` YAML.

**Escape hatch.** If the raw `Values` YAML already contains an `ingress` key, Kubeport leaves it alone — the form fields are a convenience layer, not a lock-in. Use this for advanced configurations like multi-host SAN certs, custom annotations, or alternate path types.

Ingress suggestions are live, read-only cluster discovery. They are not persisted anywhere except the Helm Release fields you explicitly save, and the fields remain editable when nothing is detected.

After saving with ingress changes, **Pending Changes** lights up. Click **Preview Diff** before deploying to see the `Ingress/<release>` resource being added (or its `tls` block changing) in the desired vs live diff. After deployment, the Release Status area shows **Reachable at** once the live Ingress reports a load-balancer address; until then it shows that Kubeport is waiting for the address.

---

## 4. Apply Raw Manifests (Service Bundle)

When you need to deploy something that isn't packaged as a Helm chart — a small `ConfigMap`, a `CronJob`, an `Ingress`:

1. Open **Service Bundle** → **New**.
2. Pick the target `Cluster` and `Namespace`.
3. Paste a multi-document YAML (`---`-separated) into `Manifest`.
4. Save. The form validates each document against the [allowlist of supported kinds](../AGENTS.md#supported-resource-kinds-service-bundle): `Pod`, `Service`, `Deployment`, `ConfigMap`, `Secret`, `Namespace`, `Ingress`, `PersistentVolumeClaim`, `StatefulSet`, `DaemonSet`, `Job`, `CronJob`, `ServiceAccount`, `ClusterRole`, `ClusterRoleBinding`, `Role`, `RoleBinding`. CRDs are out of scope by design.
5. Click **Apply**. Server-side apply runs on the `long` queue.
6. Click **Delete** to remove the bundle's resources. The bundle row enters `Deleting` state until cleanup finishes.

The reconciler checks resource existence every 5 minutes and flags missing-but-expected resources as `Degraded`.

---

## 5. Manage Frappe Sites on a Bench

A `Frappe Site` row is desired state for one Frappe site on a `Helm Release` bench. Behind the scenes, every operation is a Kubernetes Job whose pod template is cloned from a live bench workload pod.

### 5.1 Create a site

1. Open **Frappe Site** → **New**.
2. Pick `Bench Release` (the `Helm Release` row).
3. Fill in `Site Name` (must be a hostname-style label: lowercase alphanumerics, `.`, `-`, `_`, starting and ending alphanumeric).
4. Fill in `Admin Password` and either `DB Root Password` (plaintext) or `DB Root Secret` (a Kubernetes Secret reference).
5. Optional: list `Apps` to install (`erpnext`, `hrms`, etc.).
6. Save and click **Create Site**.
7. Status transitions: `Draft → In Progress → Active | Failed`.

Credentials are written to a per-Job Kubernetes Secret, owner-referenced to the Job so they are GC'd alongside the Job's TTL cleanup. They are **never** placed in plaintext env vars.

### 5.2 Migrate a site

On an `Active` row, click **Migrate**. A `bench migrate` Job is submitted. Status transitions: `Active → Migrating → Active | Failed`. No credentials Secret is needed — bench reads them from `site_config.json`.

If the migrate Job exits non-zero but the site remains functional (a known false-negative case for `bench migrate`), the row recovers to `Active` on the next reconciliation tick.

### 5.3 Cancel an in-flight operation

While a site is `In Progress`, `Migrating`, or `Deleting`, click **Cancel**. The operation token is rotated, the row is marked `Failed`, and the K8s Job is best-effort deleted (`propagation_policy=Background` cleans up the Secret via owner-reference GC).

Cancelling a `Migrating` operation requires typed-confirmation because killing `bench migrate` mid-run can leave MariaDB schema changes half-applied.

### 5.4 Drop a site

On an `Active` or `Failed` row, click **Delete Site**. A `bench drop-site --no-backup --force` Job is submitted. On a confirmed-missing bench probe, the `Frappe Site` row itself is removed. On a still-present probe, the row lands in `Failed` with the Job logs in `status_detail` so the operator can retry.

Direct row deletion of an `Active` row is **refused** — it would orphan the real site on the bench PVC. Use **Delete Site** instead.

---

## 6. Back Up and Restore a Frappe Site

Backups are first-class: their metadata is a standalone DocType (`Frappe Site Backup`) and survives the deletion of the source `Frappe Site` row, so an "Available" archive can be used to restore even after the source row is gone.

### 6.1 Take a backup

1. From a `Frappe Site` form, click **Backup**.
2. The worker ensures a namespace-local `kubeport-backups` PVC exists, mounts it, and runs `bench backup --with-files` in a cloned bench Job.
3. The result is a tar archive on the PVC. A `<archive>.size` sidecar file is written **only** on a fully-flushed success.
4. The row transitions `Pending → In Progress → Available | Failed`. `Available` is finalised only after a probe pod confirms the size sidecar exists.

### 6.2 Restore from a backup

1. From a `Frappe Site Backup` row in `Available`, click **Restore**.
2. The worker mounts the same backup PVC, extracts the archive, and runs `bench restore --force` (with public/private file archives if present).
3. Status transitions: `Available → Restoring → Available | Failed`. A failed restore returns the backup row to `Available` (the archive remains usable for a later retry) but marks the target site `Failed`.

### 6.3 Backup lifecycle is decoupled from the site row

- An archive's lifecycle is independent of the source site's bench PVC.
- Trashing a `Frappe Site Backup` row enqueues archive deletion on the PVC (even on `Failed` rows, to clean up partial-write archives).
- Cancelling a parent `Frappe Site` cascades to in-flight backups: it rotates each backup's `operation_token`, marks them `Failed`, and enqueues cluster cleanup.

### 6.4 Schedule backups and configure retention

`Frappe Site` exposes three optional fields that turn manual backups into a hands-off cycle:

- **Backup Schedule (cron)** — five-field cron expression, e.g. `0 2 * * *` for daily at 02:00. Empty disables scheduling.
- **Retention: Max Backups** — keep at most N `Available` backups; older ones are auto-trashed. `0` disables count-based retention.
- **Retention: Max Age (days)** — auto-trash any `Available` backup older than this many days. `0` disables age-based retention.

How it runs:

1. Every reconciliation tick (`*/5 * * * *`), the scheduler checks each `Active` site that has a non-empty schedule.
2. If the next firing computed from the previous run (or the row's `creation`, the first time) is in the past, the tick advances the marker to `now()` then enqueues one backup via the same path as the **Backup Now** button.
3. After the schedule pass, retention pruning trashes any `Available` rows that exceed either the count or age cap. Trashing a row triggers the existing PVC archive cleanup, so the archive is removed alongside the metadata.

Edge cases worth knowing:

- **Catch-up after downtime.** Long downtime (e.g., the bench was off for a week) triggers exactly **one** catch-up backup, not a flood — the marker advance bounds croniter's next-run computation.
- **Manual + scheduled overlap.** If you click **Backup Now** between the scheduler's read and its enqueue, the scheduler sees the in-flight backup and skips without enqueueing a duplicate. The marker still advances by one slot, which only delays the next scheduled run by one slot.
- **Retention only touches `Available` rows.** `Failed` rows stay for diagnostics; `In Progress` and `Restoring` rows are protected by the doctype's own delete guard.
- **Granularity.** The minimum useful cron resolution is 5 minutes — finer cron expressions still fire, but at most once per reconciliation tick.

---

## 7. Live Discovery

From a `Kubernetes Cluster` row, the form renders live discovery tables:

- **Helm releases** in the cluster (cluster-wide).
- **Frappe sites** on releases identified as Frappe benches (currently the official `erpnext` chart).

Discovery is **read-only**: it never persists rows into MariaDB.

To turn a discovered Helm release into a tracked `Helm Release` row, click **Track** on the release. Kubeport creates the row by adopting the release's identity (`cluster/namespace/release_name`). Discovery never silently creates rows.

---

## 8. Operator Tools

### 8.1 Kubernetes Command

A deliberate operator-tools doctype for ad-hoc cluster work. The kind allowlist is fixed:

| Operation | Allowlisted kinds |
|---|---|
| Get / List | `Pod`, `Job`, `Secret`, `ConfigMap`, `Service`, `Deployment`, `StatefulSet`, `PersistentVolumeClaim` |
| Delete | `Pod`, `Job`, `ConfigMap` only |

Destructive changes to the dangerous quartet (`Secret`, `PVC`, `Deployment`, `StatefulSet`) must go through the proper controllers — not through this doctype.

Delete requires a typed `confirm_destructive` field, enforced both at form save and again at execute.

### 8.2 Kubernetes Command Audit Log

Append-only. Every execute (success or failure) writes one row. `System Manager` has read access; the doctype is never written from the UI. The audit row is decoupled from the source `Kubernetes Command` row so the audit trail survives row deletion.

### 8.3 Site Image Catalogue

Curated rows are seeded from `kubeport/site_images/catalog.json` by the daily sync. Operators can also register their own pre-built public GHCR images alongside curated rows. Only curated rows can be marked `is_default`. Curated rows cannot be deleted (mark them `Deprecated` instead); user rows can be deleted only when no `Helm Release` references them.

---

## 9. Reconciliation and Drift

The 5-minute reconciliation tick runs unattended. It will:

- Recover `Degraded` Helm releases to `Deployed` when workloads become ready again.
- Mark `Deployed` Helm releases as `Degraded` when workloads become unready.
- Recover stale `In Progress` / `Uninstalling` Helm operations after 30 minutes by checking live Helm status.
- Mark `Service Bundle` rows `Degraded` when expected resources are missing.
- Finalise in-flight `Frappe Site` and `Frappe Site Backup` rows after a ground-truth probe; defer when the probe returns `unknown`.
- Sweep orphan Kubeport-managed Jobs not referenced by any `Frappe Site` / `Frappe Site Backup` row (Jobs younger than one tick are skipped to avoid racing a worker).

If a row stays in `Degraded` indefinitely, that is a real cluster problem, not a Kubeport problem — open the in-form readiness drilldown to see which resource is unready.

---

## 10. Smoke-Test Procedure (Pre-Release)

Mocked unit tests cannot credibly assert real-cluster behaviour: admission acceptance of emitted Job manifests, `bench new-site --force` behaviour across Frappe majors, real `ownerReference` cascade GC on Job delete, RBAC sufficiency for the control-plane service account.

Before merging a change that touches Frappe Site provisioning (or before cutting a release), run through the following on a real cluster (`kind`, `k3d`, or a real cluster). Each scenario is a single human-observable check; share the same bench release across them.

1. **Happy path**: create `smoke-1.example.com` with `install_apps=erpnext`. Expect `Draft → In Progress → Active` within ~10 min. Confirm via `kubectl exec` that `bench --site smoke-1.example.com list-apps` returns 0 and that the creds Secret was GC'd with the Job.
2. **Recreate (Force)**: tick `Force Create` and click **Recreate Site (Force)**. Confirm a new Job and Secret are created and the old Secret is GC'd.
3. **Cancel during In Progress**: start `smoke-2.example.com` with a slow `install_apps=erpnext,hrms`. While `In Progress`, click **Cancel**. Confirm the Job and Secret are gone within seconds.
4. **Delete-while-In-Progress**: start `smoke-3.example.com`. Delete the row mid-`In Progress`. Confirm no stranded Job or Secret.
5. **Worker-crash recovery (orphan-Job sweep)**: start a site, then SIGKILL the RQ worker after the Job is applied but before `operation_job_name` is recorded. Wait one reconciler tick. The orphan sweep should delete the Job; operator log contains `Sweeping orphan Frappe Site Job '...'`.
6. **TTL-expired recovery**: lower `_JOB_TTL_SECONDS` (`kubeport/tasks/site_tasks.py`) and the scheduler cron temporarily. Create a site so the Job is GC'd before the next reconciler read. Expect the 404 branch to fire and the row to transition via the ground-truth probe.
7. **`activeDeadlineSeconds` (hung pod)**: create a site against a bench whose DB is unreachable (e.g., scale MariaDB to 0). After `_JOB_ACTIVE_DEADLINE_SECONDS`, expect `DeadlineExceeded` and the next reconciler tick to mark the row `Failed` with pod-log detail.
8. **Transient bench restart during reconciliation**: start a site, then trigger a rolling restart of the bench Deployment so pods are briefly unavailable. Expect the probe to return `SITE_PROBE_UNKNOWN` and the row not to be finalised this tick; the next tick after rollout completes finalises it.

In the PR description record: cluster kind, K8s version, ERPNext chart version, bench image tag, which scenarios passed, and any deviation.

---

## 11. Troubleshooting

### Discovery shows releases but no sites

1. Confirm the release is the official `erpnext` chart — no other bench charts are recognised in v1.
2. Check the target namespace for **running** workload pods. Infra pods (MariaDB, Valkey) are excluded by design.
3. Inspect the `Helm Release` readiness drilldown for scoped logs, events, rollout context, and PVC state.

```bash
kubectl get pods -n <namespace>
kubectl describe pod -n <namespace> <pod-name>
kubectl get pvc -n <namespace>
kubectl get events -n <namespace> --sort-by=.lastTimestamp
```

### A Helm Release is stuck `In Progress` or `Uninstalling`

It will be auto-recovered after 30 minutes by the reconciler, which checks live Helm status and adjusts the row's state. If you want to recover faster, you can re-trigger the operation once the previous worker is confirmed dead.

### A Frappe Site stays `In Progress` after the Job is gone

The reconciler will pick it up on the next tick. If the Job was TTL-cleaned before reconciliation read it, the probe-based recovery branch finalises the row.

### A backup keeps reporting `In Progress` after the Job exited

The probe pod that mounts the backup PVC and reads the `<archive>.size` sidecar is the source of truth. If `<archive>.size` is missing, the backup is **not** considered `Available` — that is intentional. Inspect the Job logs and the PVC contents.
