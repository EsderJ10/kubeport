# Contributing to Kubeport

Kubeport is a Frappe app. Contributions follow standard Frappe app conventions plus a few invariants specific to this project (see [`AGENTS.md`](AGENTS.md) for the full list).

---

## Development environment

Kubeport is developed inside a Frappe Bench. The supported workflow is a dev container, which provides Frappe v16, MariaDB, Redis, Helm, and `kubectl` pre-installed.

A minimal manual setup looks like:

```bash
# Inside an existing Frappe Bench checkout
bench get-app kubeport <repository-url> --branch main
bench install-app kubeport
bench --site <site> migrate
```

Required runtime dependencies on the bench host:

- Python ≥ 3.14
- Helm 3 on `PATH`
- A reachable Kubernetes cluster (kubeconfig, bearer token, or in-cluster service account) for any feature that exercises real cluster I/O

---

## Branching and pull requests

- The default branch is `main`. All changes land via pull request against `main`.
- Feature branches should use a topic-style prefix: `feat/...`, `fix/...`, `chore/...`, `refactor/...`, `docs/...`.
- Keep PRs reviewable: one cohesive change per PR, with the `CHANGELOG.md` updated in the same commit when the change is architecturally meaningful.

### Commit messages

Commit messages follow the existing project style: a single-line conventional summary, no body, no `Co-Authored-By` trailer. Examples from `git log`:

```
fix: integrate main and resolve CI lint and reconciliation test failures
refactor: remove obsolete migration patch tests
ci: enforce test job gating now that bench-in-CI is green
```

### CHANGELOG entries

`CHANGELOG.md` is an **architecture decision log**, not a release notes file. Add an entry when the PR records a design decision worth preserving (a chosen approach, the rejected alternatives, the reason). Bug fixes and refactors that do not change architecture do not require an entry. The format is documented at the top of `CHANGELOG.md`.

---

## Code style

| Concern | Rule | Tool |
|---|---|---|
| Python indentation | tabs | `ruff format` |
| Python quotes | double | `ruff format` |
| Python line length | 110 chars | `ruff` |
| Python target | 3.14 | `pyproject.toml` (`target-version = "py314"`) |
| JS / CSS formatting | prettier defaults | `prettier` |
| JS linting | repo `.eslintrc` | `eslint` |
| Type annotations | required on every `@frappe.whitelist()` method | `require_type_annotated_api_methods = True` in `hooks.py` |
| DocType field types | `frappe.types.DF` | `export_python_type_annotations = True` in `hooks.py` |

Pre-commit runs the formatters and linters automatically. Install it once per checkout:

```bash
cd apps/kubeport
pre-commit install
```

The pre-commit ruff version is pinned in `.pre-commit-config.yaml`. The CI lint job (`.github/workflows/ci.yml`) pins to the same version. If you bump one, bump the other (and `docker-compose.lint.yml` — see below).

### Containerised lint (no host install)

If you do not want ruff on the host, use the bundled compose stack — it pins the same image and version as CI:

```bash
make fmt         # format in place
make lint        # report findings
make fix         # auto-fix + format
make lint-check  # exact CI dry-run (format --check + check)
```

`make` shells out to `docker compose -f docker-compose.lint.yml`. Files written by the container are owned by your host UID. If you bump ruff, bump it in `.github/workflows/ci.yml`, `.pre-commit-config.yaml`, **and** `docker-compose.lint.yml`.

A companion workflow (`.github/workflows/lint-autofix.yml`) runs `ruff check --fix` + `ruff format` on every PR and commits the result back to the PR branch, so mechanically fixable drift never blocks the `Lint` check.

---

## Tests

Run the full Kubeport suite from a working bench:

```bash
bench --site <site> run-tests --app kubeport
```

Run a single DocType's tests:

```bash
bench --site <site> run-tests --app kubeport --doctype "Helm Release"
```

When the bench environment is unavailable, prefer focused unit tests and syntax checks near the changed module rather than a full bench run.

### Test conventions

- Default to `IntegrationTestCase`. Use `UnitTestCase` only for pure, isolated logic.
- When you add or change a whitelisted API, add a test close to the changed module.
- For changes that affect Frappe Site provisioning, run the manual smoke procedure documented in §10 of [`docs/operator-guide.md`](docs/operator-guide.md) and record the result in the PR description before merging.

### CI

Every PR runs `.github/workflows/ci.yml`:

- `lint` job: `ruff format --check` + `ruff check` against the whole repo.
- `test` job: bootstraps a Frappe v16 bench against MariaDB 10.6 and Redis 7 service containers, installs `kubeport`, and runs `bench --site test_site run-tests --app kubeport`.

Both jobs are required to pass before merge.

---

## Architectural invariants you must respect

These are non-negotiable. The full list is in [`AGENTS.md`](AGENTS.md). The short version:

1. **Desired state lives in MariaDB. Observed state is queried live.** Discovery code paths must never write to MariaDB.
2. **All cluster mutations run through `frappe.enqueue(..., queue="long")`.** No `helm` or cluster-mutating `kubernetes-client` calls from the web request thread.
3. **Background workers re-check the document's `operation_token` (or `sync_token`) before any state write.** A stale worker must never overwrite a newer operation.
4. **State writes in workers are targeted (`db_set` / `frappe.db.set_value`).** Never `doc.reload()` in a worker — it races concurrent updates from the form.
5. **External cluster data is fetched async by the form (`frappe.xcall`), not via `doc.onload`.**
6. **K8s API clients are scoped per cluster via `get_k8s_api_client(cluster_name)`.** No shared global client state between requests.
7. **Backup archives are independent of the source site.** A backup row in `Available` must remain restorable even after the source `Frappe Site` row is deleted.

A change that blurs any of these is a design change, not a bug fix — open a discussion in the PR before landing it.

---

## Documentation

When a change affects discovery, background-task behaviour, or desired-state semantics, update the relevant document **in the same commit**:

| Document | Update when |
|---|---|
| [`README.md`](README.md) | The user-facing feature surface changes (added or removed capabilities). |
| [`docs/architecture.md`](docs/architecture.md) | A new layer, container, sequence, or invariant is introduced. |
| [`docs/operator-guide.md`](docs/operator-guide.md) | An operator-visible workflow changes (new fields, new buttons, new lifecycle states). |
| [`docs/control-plane-state.md`](docs/control-plane-state.md) | Capability surface or robustness defences change. |
| [`docs/codebase-summary.md`](docs/codebase-summary.md) | A module's responsibility or boundary changes. |
| [`AGENTS.md`](AGENTS.md) | An invariant changes — rare. |
| [`CHANGELOG.md`](CHANGELOG.md) | Any architecturally meaningful decision. |

For documentation-only PRs, the `lint` CI job still runs (markdown is not linted today, but Python files are).

---

## Reporting bugs and proposing features

Open an issue with:

- A clear reproduction (commands, DocType operations, observed vs. expected).
- The relevant cluster context (Kubernetes version, chart version, auth mode).
- For backups, sites, or Helm releases: the row's status, `operation_token`, and `operation_job_name` if applicable.

For security issues, follow [`SECURITY.md`](SECURITY.md) instead — do not open a public issue.
