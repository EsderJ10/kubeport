# Archived planning documents

This directory holds documents that captured planning, design exploration, or
procedural detail at a specific point in the project's life. They are kept for
**traceability** — they record how decisions were considered before they were
taken — but they are no longer authoritative for current behaviour.

For the current state of the system, read instead:

- [`docs/control-plane-state.md`](../control-plane-state.md) — current
  capabilities, robustness defences, and open gaps.
- [`docs/operator-guide.md`](../operator-guide.md) — how to use the system
  end-to-end, including the smoke-test procedure that supersedes the historical
  `frappe-site-smoke.md` here.
- [`docs/architecture.md`](../architecture.md) — C4 diagrams and design
  invariants.
- [`CHANGELOG.md`](../../CHANGELOG.md) — architecture decision log.

## Index

| File | What it captured | Status |
|---|---|---|
| `plan-a-site-backup-restore.md` | Initial design exploration for backup / restore | Implemented; superseded by current code in `kubeport/tasks/site_tasks.py` and the backup section of the operator guide. |
| `plan-b-observability-drilldown.md` | Initial design for the in-form observability panels | Implemented; superseded by `kubeport/api/observability.py` and the Helm Release form behaviour. |
| `plan-f-application-abstraction.md` | Exploration of a higher-level "application" abstraction over Helm releases | Not implemented; kept for future work consideration. |
| `frappe-site-smoke.md` | Manual pre-release smoke procedure | Superseded by §10 of `docs/operator-guide.md`. |
