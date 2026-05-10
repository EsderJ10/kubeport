# CLAUDE.md

Quick-reference for Claude Code sessions. For the full set of rules, design invariants, and implementation patterns, see [`AGENTS.md`](AGENTS.md).

## Project

Kubeport is a Frappe app that acts as a Kubernetes control plane. **Desired state → MariaDB (DocTypes). Observed state → live cluster queries. Never mix them.**

## Commands

```bash
# Formatting and linting (host install)
ruff format                  # Python formatting (tabs, double quotes, 110 chars)
ruff                         # Python linting
prettier                     # JS/CSS formatting
eslint                       # JavaScript linting

# Containerised lint (no host install — uses docker-compose.lint.yml)
make fmt                     # format in place
make lint                    # report findings
make fix                     # auto-fix + format
make lint-check              # exact CI dry-run

# Pre-commit (required)
cd apps/kubeport && pre-commit install

# Tests (requires running Frappe bench + site)
bench --site <site> run-tests --app kubeport --doctype <DocType>
```

## Code Style

- **Indentation**: tabs
- **Quotes**: double quotes
- **Type annotations**: required on all whitelisted API methods (`frappe.types.DF` for DocType fields)

## Architecture at a Glance

| Layer | Path | Notes |
|---|---|---|
| DocTypes | `kubeport/kubeport/doctype/` | Desired state in MariaDB |
| API | `kubeport/api/` | Read-only whitelisted endpoints |
| Utilities | `kubeport/utils/` | Stateless K8s + Helm helpers |
| Tasks | `kubeport/tasks/` | All cluster-mutating work (background jobs) |
| Scheduled | `hooks.py` | `*/5 * * * *` → reconciliation, daily → repo sync |

## Critical Rules (see AGENTS.md for details)

1. **All cluster mutations go through background jobs** — never from the web thread.
2. **Discovery is read-only** — never persist discovered state to MariaDB.
3. **Use operation/sync tokens** — background workers must re-check tokens before acting.
4. **Targeted field updates in tasks** — use `db_set` / `frappe.db.set_value`, not `doc.reload()`.
5. **Async form rendering** — use `frappe.xcall` + client-side rendering for external data, not `doc.onload`.

## Documentation

| File | Purpose |
|---|---|
| `README.md` | Entry point: capability summary, architecture sketch, install, doc map |
| `AGENTS.md` | Authoritative invariants and implementation patterns |
| `CONTRIBUTING.md` | Dev setup, lint/test workflow, PR conventions |
| `SECURITY.md` | Disclosure policy and trust-boundary notes |
| `docs/threat-model.md` | Trust-boundary diagram, STRIDE catalogue, endpoint × boundary mapping |
| `docs/thesis.md` | Project framing: problem, SOTA, objectives, results, future work |
| `docs/architecture.md` | C4 diagrams, runtime sequences, design invariants |
| `docs/operator-guide.md` | End-to-end operator workflows + smoke procedure |
| `docs/control-plane-state.md` | Capabilities, robustness defences, open gaps |
| `docs/codebase-summary.md` | Module-level architecture reference |
| `docs/evaluation.md` | Empirical evaluation chapter (functional, reliability, baseline, scaling) |
| `docs/history/` | Archived planning documents (see its README) |
| `eval/README.md` | End-to-end evaluation harness (`make eval`) |
| `CHANGELOG.md` | Architecture decision log |

Update docs in the same commit when changes affect discovery, tasks, or state semantics.
