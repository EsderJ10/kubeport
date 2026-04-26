# CLAUDE.md

Quick-reference for Claude Code sessions. For the full set of rules, design invariants, and implementation patterns, see [`AGENTS.md`](AGENTS.md).

## Project

Kubeport is a Frappe app that acts as a Kubernetes control plane. **Desired state → MariaDB (DocTypes). Observed state → live cluster queries. Never mix them.**

## Commands

```bash
# Formatting and linting
ruff format                  # Python formatting (tabs, double quotes, 110 chars)
ruff                         # Python linting
prettier                     # JS/CSS formatting
eslint                       # JavaScript linting

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
| `README.md` | Shipped feature set (keep aligned with reality) |
| `AGENTS.md` | Full agent rules, invariants, and patterns |
| `docs/control-plane-state.md` | Capabilities, gaps, robustness notes |
| `docs/codebase-summary.md` | Module-level architecture reference |
| `CHANGELOG.md` | Architecture decision log |

Update docs in the same commit when changes affect discovery, tasks, or state semantics.
