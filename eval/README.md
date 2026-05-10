# Kubeport Evaluation Harness

A reproducible end-to-end runner that drives the Kubeport golden path
against a target k3d cluster and emits a machine-readable JSON report.
This harness is the source of the empirical numbers cited in
[`docs/thesis.md`](../docs/thesis.md) and
[`docs/control-plane-state.md`](../docs/control-plane-state.md).

## Layout

| Path | Purpose |
|---|---|
| `eval/harness.py` | Host-side driver. Validates the dev container, ships the in-container script, captures the JSON report. |
| `eval/_inproc.py` | Runs inside the dev container. Talks to Frappe directly through the bench Python interpreter; drives the 10-step golden path. |
| `eval/results/` | Timestamped reports plus a checked-in `sample.json` from a known-good run. |
| `eval/results/sample.json` | Reference run for thesis quoting. |

## Prerequisites

- Dev container `tfg_devcontainer-frappe-1` running, with the
  `kubeport` app installed on the bench site (defaults to
  `frappe-k8s.localhost`). See `~/workspace/school/tfg/devcontainer-example`.
- A bench long-queue worker, default-queue worker, and scheduler must
  be running inside the dev container — the harness fails fast
  otherwise. Start them with:
  ```bash
  docker exec -d tfg_devcontainer-frappe-1 bash -lc \
    'cd /workspace/development/bench-16 && bench worker --queue long > /tmp/worker-long.log 2>&1'
  docker exec -d tfg_devcontainer-frappe-1 bash -lc \
    'cd /workspace/development/bench-16 && bench worker --queue default > /tmp/worker-default.log 2>&1'
  docker exec -d tfg_devcontainer-frappe-1 bash -lc \
    'cd /workspace/development/bench-16 && bench schedule > /tmp/scheduler.log 2>&1'
  ```
- `k3d` v5+ on the host with at least one running cluster
  (`k3d cluster list`). The harness reads a kubeconfig from the host;
  it does **not** boot a fresh cluster — pre-existing clusters are
  reused per the AGENTS.md constraint that the dev container's bench
  hosts Kubeport itself.
- `frappe/erpnext` Helm Repository reachable: `https://helm.erpnext.com`
  (default). Override with `--helm-repo-url`.
- A curated `Kubeport Site Image` row already seeded by
  `kubeport.tasks.site_image_tasks.enqueue_sync_site_image_catalog`
  (the daily scheduled job in `hooks.py`).

No host-side Python dependencies beyond the standard library: the host
driver only shells out to `docker` and `k3d`.

## Running

```bash
# From repo root
make eval                 # default config: k3d-cluster=frappe-cluster, ERPNext chart
make eval-clean           # delete all eval/results/*.json reports

# Or invoke the driver directly with custom parameters:
python3 eval/harness.py \
    --k3d-cluster frappe-cluster \
    --release-name my-eval \
    --namespace my-eval \
    --site-name my-eval.localhost
```

Each invocation writes a new file `eval/results/<utc-timestamp>.json`.
Existing reports are never overwritten.

## Golden path (10 phases)

The phases match the lifecycle described in
[`docs/control-plane-state.md`](../docs/control-plane-state.md). Every
phase rotates an operation token under the hood — see AGENTS.md §3.

| # | Phase | What it does | Expected terminal state |
|---|---|---|---|
| 1 | `setup_cluster_doc` | Insert (or reuse) a `Kubernetes Cluster` row from the supplied kubeconfig | row exists |
| 2 | `setup_helm_repo` | Insert (or reuse) a `Helm Repository` row, trigger `sync_charts`, wait for `last_synced` | `last_synced` populated |
| 3 | `verify_chart` | Confirm the requested `Helm Chart` row exists after sync | row exists |
| 4 | `create_release` | Insert a new `Helm Release` row with the chart + cluster | row inserted |
| 5 | `deploy_release` | Call `deploy_release` and wait until the row reaches `Deployed` | `status == "Deployed"` |
| 6 | `create_site` | Insert a `Frappe Site` row, call `create_site`, wait for `Active` | `status == "Active"` |
| 7 | `migrate_site` | Call `migrate_site`, wait for `Active` | `status == "Active"` |
| 8 | `backup_site` | Call `backup_site`, wait for the `Frappe Site Backup` row to reach `Available` | `status == "Available"` |
| 9 | `restore_site` | Call `restore_site` against that backup, wait for `Active` | `status == "Active"` |
| 10 | `drop_site` | Call `delete_site`, wait until reconciliation removes the row | row absent |

A phase is `passed` when the terminal state was reached within the
configured timeout. If a phase fails, every later phase is marked
`skipped` and the report records the original failure detail.

## Report schema

Top level:

```json
{
  "schema_version": 1,
  "generated_at": "2026-05-10T18:00:00Z",
  "started_at":   "2026-05-10T17:50:00Z",
  "finished_at":  "2026-05-10T18:00:00Z",
  "duration_seconds": 600.0,
  "host_wall_seconds": 605.2,
  "context":   { ... non-secret arguments ... },
  "invocation":{ ... resolved invocation parameters ... },
  "phases":    [ { ... per-phase object ... } ],
  "summary": {
    "passed": 10,
    "failed": 0,
    "skipped": 0,
    "total":   10
  }
}
```

Per-phase object:

```json
{
  "phase": "deploy_release",
  "started_at":  "2026-05-10T17:53:00Z",
  "finished_at": "2026-05-10T17:55:30Z",
  "duration_seconds": 150.0,
  "timeout_seconds":  900,
  "status": "passed",
  "detail": ""
}
```

`status` is one of `passed`, `failed`, `skipped`, `pending`. Secrets
(kubeconfig contents, admin/db passwords, raw values YAML) are stripped
from `context` before serialisation.

## Idempotency

- Each run uses timestamped defaults for `--release-name`,
  `--namespace`, and `--site-name` so concurrent or repeated runs do
  not collide.
- Cluster and Helm Repository rows are reused if a row of the same
  name already exists — both are idempotent identity carriers.
- Phase 10 (`drop_site`) cleans up the per-run Frappe Site row.
  Helm Releases and the per-run namespace are left for the operator
  to inspect; uninstall via the Helm Release form when done.

## Reuse vs. fresh mode

The harness is identity-driven: phases that find an existing row of
the requested name are no-ops. This makes two run shapes possible:

- **Fresh end-to-end** — pass distinct `--cluster-doc-name`,
  `--release-name`, and `--namespace`. Phases 1, 4, and 5 each do real
  work; the run exercises the full lifecycle the TODO-04 plan
  describes.
- **Reuse mode** — point at an existing `Kubernetes Cluster` and
  `Helm Release` (e.g., the dev container's `demo-bench`). Phases 1-5
  short-circuit; the run measures only the per-site lifecycle
  (phases 6-10).

The committed `results/sample.json` was produced in **reuse mode**
against the dev container's `demo-k3d` cluster doc and `demo-bench`
ERPNext release: a fresh `eval-<timestamp>.localhost` site is created,
migrated, backed up, restored, and dropped. This is the realistic
shape of a CI-run measurement, since redeploying ERPNext from scratch
each run would dwarf the per-site lifecycle being characterised.

## Time budget

A clean reuse-mode run targets <30 min wall-clock on the dev container
(measured 1710s = 28.5 min on the reference run). Most of that time is
the four K8s Jobs (`bench new-site`, `migrate`, `backup`, `restore`,
`drop-site`) plus the 5-minute reconciliation cron interval. Per-phase
timeouts are set generously above expected medians to absorb image
pulls and pod scheduling delays; tighten them for CI use. A fresh
end-to-end run adds another ~5-10 min for the ERPNext Helm deploy.

## Fault scenarios (TODO-05)

The fault-injection harness lives under `eval/faults/` and is driven by
`eval/faults/run.py`.  Each scenario empirically validates one
robustness defence listed in `docs/control-plane-state.md` §Robustness
Properties.  Reports land at `eval/results/faults-<utc-timestamp>.json`
and the `make eval-faults` / `make eval-faults-real` targets cover the
default invocation shapes.

| Scenario | Defended invariant | Witness | Status |
|---|---|---|---|
| `worker_kill_mid_helm_upgrade` | Stale-operation reconciler recovers worker-stranded Helm Release rows within `STALE_OPERATION_THRESHOLD_MINUTES` (30 min) | [`kubeport/tasks/reconciliation.py:_reconcile_stale_helm_operations`](../kubeport/tasks/reconciliation.py) | Implemented |
| `job_ttl_expired_before_reconcile` | Reconciliation falls back to ground-truth bench probe when the operation Job is gone before the tick reads it | [`kubeport/tasks/reconciliation.py:_probe_site_state`](../kubeport/tasks/reconciliation.py) | Implemented |
| `pod_exec_timeout_during_site_probe` | Three-state probe returns `unknown` on transient pod-exec failure; row stays In Progress for the tick and recovers next tick | `kubeport/utils/observability.py` | Planned (next commit) |
| `corrupt_archive_size_sidecar` | PVC-side completion probe marks backup `Failed` and the archive trash cleanup runs when the `<archive>.size` sidecar disappears | `kubeport/tasks/reconciliation.py:reconcile_site_backups` | Planned (next commit) |

### Running the implemented scenarios

`worker_kill_mid_helm_upgrade` requires:

- An existing `Helm Release` row in `Deployed` (or `Degraded`) state — the harness drives a no-op upgrade against it. Default: `demo-k3d/demo/demo-bench` (the same release used by `make eval`).
- The dev container, long-queue worker, and bench scheduler running per the Prerequisites section above.

`job_ttl_expired_before_reconcile` requires:

- An existing `Frappe Site` row in `Active` state whose underlying site
  is **actually functional** on the bench (`bench list-apps` exits 0
  inside the bench pod).  Default: `demo-k3d/demo/demo-bench/erp.cluster.local`.
  If your bench has a different known-good site, override with
  `--site-doc-name <docname>`.
- The probe contract relies on the existing release/cluster the row
  points at — no extra cluster setup is needed beyond a healthy bench.

```bash
# Fast path: for both scenarios, backdate the staleness clock or
# directly invoke the per-doctype reconciler so recovery is observed
# in seconds; the report records fast_forward_used=true per scenario.
make eval-faults

# Realistic path: wait for the natural 5-min cron tick (and, for
# scenario 1, the 30-min staleness window). Wall-clock typically
# 30-35 min for scenario 1; ~5 min for scenario 2.
make eval-faults-real
```

After every scenario the harness checks whether the long-queue worker
is still up and restarts it if needed (scenario 1 kills it
deliberately).  Pass `--no-restart-worker` to `eval/faults/run.py` to
skip the restart (useful when investigating).  The dev container is
left in the same state it was found.

### Report schema

Top-level matches the golden-path report shape (`schema_version`,
`generated_at`, `started_at`, `finished_at`, `duration_seconds`,
`context`, `summary`).  Each entry in `scenarios[]` carries:

```json
{
  "scenario": "worker_kill_mid_helm_upgrade",
  "defended_invariant": "Stale operation reconciler recovers worker-stranded Helm Release rows",
  "witness": { "reconciler": "<file:symbol>", "threshold_minutes": 30 },
  "injected_at":   "2026-05-10T18:00:00Z",
  "recovered_at":  "2026-05-10T18:00:12Z",
  "mttr_seconds":  12.0,
  "expected_mttr_bound_seconds": 1800,
  "fast_forward_used": true,
  "observed":  { "final_status": "Deployed", "operation_token_rotated": true },
  "passed":    true,
  "detail":    "Stale-op reconciler recovered the stranded row to 'Deployed' in 12.0s (fast-forwarded).",
  "steps": [ ... ordered timeline of every observation and mutation ... ]
}
```

The reference run is checked in at `eval/results/sample-faults.json`
for thesis quoting.

## Cross-references

- Invariants the harness exercises: AGENTS.md §Design Invariants.
- Per-DocType state machines: `docs/control-plane-state.md`.
- Robustness properties matched to scenarios:
  `docs/control-plane-state.md` §Robustness Properties.
