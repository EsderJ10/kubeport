# Kubeport vs raw `kubectl + helm` — golden-path comparison

Side-by-side measurement of the same 10-phase Frappe-on-Kubernetes
workflow run twice against the same target cluster, same chart, same
image: once through Kubeport's DocTypes
([`eval/results/sample.json`](sample.json)) and once with raw
`kubectl` / `helm` / `kubectl exec ... -- bench`
([`eval/results/sample-baseline.json`](sample-baseline.json)).
Methodology and counting rules:
[`eval/baseline/README.md`](../baseline/README.md).

| Phase | Kubeport (s) | Baseline (s) | Baseline commands | Baseline manual steps |
|---|---:|---:|---:|---:|
| `setup_cluster_doc` | 0.5 | 0.2 | 1 | 1 |
| `setup_helm_repo`   | 0.7 | 1.4 | 2 | 0 |
| `verify_chart`      | 0.0 | 12.7 | 1 | 0 |
| `create_release`    | 0.0 | 0.2 | 0 | 1 |
| `deploy_release`    | 0.0 | 1.5 | 2 | 1 |
| `create_site`       | 570.9 | 174.4 | 2 | 3 |
| `migrate_site`      | 176.1 | 43.3 | 2 | 1 |
| `backup_site`       | 244.2 | 9.1 | 7 | 3 |
| `restore_site`      | 472.4 | 21.7 | 5 | 2 |
| `drop_site`         | 245.2 | 5.5 | 2 | 1 |
| **Totals**          | **1710.0** | **273.9** | **24** | **13** |

Kubeport-side numbers are from the reuse-mode reference run on
2026-05-10 ([`sample.json`](sample.json)); baseline numbers are from
the matching reuse-mode run on the same cluster + namespace + release
([`sample-baseline.json`](sample-baseline.json)).  Kubeport's
operator-facing interaction is ~1 DocType save or 1 button click per
phase, with the validation, pod-resolution, archive-handling, and
flag-assembly work captured in code rather than in the operator's
hands — the Kubeport side has no `commands` or `manual_steps` to
report.

## Reading the table

**Per-phase wall-clock is not the headline.**  The baseline finishes
the per-site lifecycle (phases 6–10) in 254s versus Kubeport's 1709s.
That gap is structural, not operator productivity: Kubeport routes
each per-site action through a Kubernetes Job and waits on the
5-minute reconciliation tick to observe the result, whereas the
baseline runs `kubectl exec ... -- bench …` synchronously and reads
the exit code directly.  Kubeport pays this cost on purpose — the
async + reconciliation model is what survives worker crashes and Job
TTL expiry (see `docs/control-plane-state.md` §Robustness Properties)
— but the wall-clock column is an accounting difference, not an
operator-time difference.

**The headline is operator overhead.**  The baseline column totals
**24 shell commands and 13 manual interventions** for a single site
lifecycle on an already-deployed bench; Kubeport elides both into
form-driven workflows.  Each `manual_steps` increment is auditable in
[`eval/baseline/run.sh`](../baseline/run.sh) — every `bump_manual`
call has an inline comment naming the operator intervention it
represents (e.g. composing a `values.yaml`, resolving the bench pod
by label, choosing a host destination directory for the backup
archive set, matching the archive set back to the right
`bench restore` flags).  The 24-command count is mechanical: each
wrapped `kubectl` / `helm` invocation bumps a per-phase counter at
its single call site.

## Reproducing

```bash
# Kubeport side (reuse mode, see eval/README.md):
make eval

# Baseline side (reuse mode, see eval/baseline/README.md):
make eval-baseline
```

Both runs target the demo `pacopepe` k3d cluster with the
`demo-bench` ERPNext release in the `demo` namespace.  Each run
produces a fresh timestamped report; this comparison is sourced from
the checked-in `sample.json` / `sample-baseline.json`.
