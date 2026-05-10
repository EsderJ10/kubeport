# Baseline-comparison harness

A side-by-side rerun of the same 10-phase golden path executed by
[`eval/harness.py`](../harness.py), but driven entirely by raw
`kubectl`, `helm`, and `kubectl exec ... -- bench` — no Kubeport in
the loop.  The output JSON has the same schema as the main harness
report, plus per-phase `commands_issued` and `manual_steps` fields.
This is the empirical basis for the SOTA claim in
[`docs/thesis.md`](../../docs/thesis.md) §2.

## Layout

| Path | Purpose |
|---|---|
| `eval/baseline/run.sh` | In-container script.  Executes each phase with raw CLI tools; tallies wall-clock, command count, and manual steps; emits JSON between `RESULT_BEGIN` / `RESULT_END` markers. |
| `eval/baseline/host_driver.py` | Host-side wrapper.  Fetches the kubeconfig (rewriting `0.0.0.0` to the dev container's bridge gateway so kubectl in the container can reach the host's published k3d port), copies the script into the container, runs it, captures the JSON, writes `eval/results/baseline-<utc>.json`. |
| `eval/results/sample-baseline.json` | Reference run for thesis quoting. |
| `eval/results/comparison.md` | Side-by-side table summarising baseline vs Kubeport (`eval/results/sample-baseline.json` vs `eval/results/sample.json`). |

## Methodology

Same target cluster, same chart, same image, same dev container,
same per-site lifecycle the main harness's reuse-mode sample uses —
the only difference is whether the operator types CLI commands or
inserts/updates DocType rows.  The baseline does not boot a fresh
ERPNext deploy because that would dwarf the per-site numbers being
characterised; the comparison is about the per-site lifecycle (phases
6–10) plus the operator overhead Kubeport elides on phases 1–5.

`run.sh` runs against the existing `demo-bench` Helm release on the
`pacopepe` k3d cluster (same fixture as `eval/results/sample.json`),
creating a fresh `eval-baseline-<timestamp>.localhost` site so reruns
do not collide.

## Counting `commands_issued` and `manual_steps`

`commands_issued` counts the distinct `kubectl` / `helm` shell
invocations a phase triggers.  Wrapper functions in `run.sh`
(`k`, `h`) bump a per-phase counter once per call so the tally is
mechanical.  `kubectl wait` counts as one command even if it polls.

`manual_steps` counts the operator decisions or interventions the
phase needs that Kubeport hides behind a DocType.  Each tally is
documented inline in `run.sh` next to the call site, so the count is
auditable.  Concretely:

| Phase | Manual step | What Kubeport does instead |
|---|---|---|
| 1 `setup_cluster_doc` | Pick the right kubeconfig context | Stored on the `Kubernetes Cluster` row |
| 4 `create_release` | Hand-write a `values.yaml` | DocType validates + merges chart defaults |
| 5 `deploy_release` | Probe pod readiness manually | Reconciliation loop polls and writes `helm_status_detail` |
| 6 `create_site` | Resolve the bench pod by label; choose admin password; supply DB root creds | Pod resolution + per-Job Secret injection in `tasks/site_tasks.py` |
| 8 `backup_site` | Choose a host destination directory; learn the new file names from the bench backups dir | Wrapper script diffs before/after and pipes onto the `kubeport-backups` PVC |
| 9 `restore_site` | Match the archive set back to bench restore flags | `_bench_restore_command` extracts the tarball and assembles flags |
| ... | ... (resolve_pod tallies one extra manual step in every phase that uses it: 6, 7, 8, 9, 10) | DocType caches the pod selector |

## Phase mapping pitfalls

Two phases collapse against the raw-CLI baseline:

- `setup_cluster_doc` has no real CLI analogue — it's a single
  `kubectl config use-context` (or implicit current-context).  The
  one manual step we count is the operator deciding which context to
  point at.
- `create_release` is a pure DocType insert in Kubeport with no
  cluster I/O at all; the baseline analogue is hand-writing a
  `values.yaml`.  Zero commands, one manual step.

Both are kept in the table for row alignment with the main harness
schema; their low durations are honest, not padded.

## What the comparison is *not* about

Per-phase wall-clock can come out **faster** for the baseline in
some phases — `kubectl wait --for=condition=Ready` is more direct
than waiting on Kubeport's 5-minute reconciliation tick to observe
the same fact.  The thesis claim is operator productivity (commands +
manual steps), not raw single-shot speed.  `eval/results/comparison.md`
calls this out so a reviewer doesn't misread the table.

## Running

```bash
# From repo root, with the dev container up and the k3d cluster
# (default: pacopepe) running with the demo-bench release deployed.
make eval-baseline

# Or invoke the host driver directly with custom parameters:
python3 eval/baseline/host_driver.py \
    --k3d-cluster pacopepe \
    --release-name demo-bench \
    --namespace demo \
    --db-root-password changeit
```

Each invocation writes a fresh `eval/results/baseline-<utc>.json`.
Existing reports are never overwritten.

## Report schema

Top level — same shape as `eval/results/sample.json`:

```json
{
  "schema_version": 1,
  "generated_at":   "2026-05-10T18:00:00Z",
  "started_at":     "2026-05-10T17:50:00Z",
  "finished_at":    "2026-05-10T18:00:00Z",
  "duration_seconds": 600.0,
  "host_wall_seconds": 605.2,
  "context":   { ... non-secret arguments ... },
  "invocation":{ ... resolved invocation parameters ... },
  "phases":    [ { ... per-phase object ... } ],
  "summary": {
    "passed": 10,
    "failed": 0,
    "skipped": 0,
    "total":   10,
    "commands_issued_total": 32,
    "manual_steps_total":    13
  }
}
```

Per-phase object — same shape as the main harness, plus the two new
fields:

```json
{
  "phase": "create_site",
  "started_at":  "2026-05-10T17:53:00Z",
  "finished_at": "2026-05-10T17:55:30Z",
  "duration_seconds": 150.0,
  "timeout_seconds":  900,
  "status": "passed",
  "detail": "",
  "commands_issued": 2,
  "manual_steps":    3
}
```

## Cross-references

- Main harness: [`eval/README.md`](../README.md).
- Per-DocType state machines the operator orchestrates by hand:
  [`docs/control-plane-state.md`](../../docs/control-plane-state.md).
- SOTA framing this run informs:
  [`docs/thesis.md`](../../docs/thesis.md) §2.
