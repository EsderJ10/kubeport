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

## Cross-references

- Invariants the harness exercises: AGENTS.md §Design Invariants.
- Per-DocType state machines: `docs/control-plane-state.md`.
- Extended fault scenarios on top of this harness:
  upcoming TODO-05 in `TODO.md`.
