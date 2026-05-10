# Plan F — Application / Stack Abstraction

## Intent

Today, deploying "ERPNext for tenant X" requires the operator to stitch three documents
together by hand: pick a `Helm Release`, then create one or more `Frappe Site` rows linked to
it, then optionally apply a `Service Bundle` for any namespace-scoped extras (cron, secrets,
ingress overrides). Each is its own form, each with its own status, and the relationship is
implicit (foreign keys + naming conventions).

Plan F introduces an `Application` DocType that owns the relationship explicitly: one
`Application` row aggregates one Helm Release + zero-or-more Frappe Sites + zero-or-more
Service Bundles, exposes a single deploy / uninstall / health story, and provides a single
"is this whole thing healthy?" answer rolled up from its components.

The cycle's goal is **one-click full-stack provisioning + a coherent top-level health view**.
After this cycle, an operator who wants ERPNext for a tenant fills in one form, clicks one
button, and watches one status indicator settle.

This plan is the largest of the three on the table. It is also the highest architectural risk:
it introduces a new top-level abstraction that every existing surface (UI, reconciliation, API,
discovery) has to reckon with. The plan is deliberately conservative about scope — Applications
are **read-mostly aggregators with a thin orchestration layer** in this cycle, not a full
reimplementation of release/site logic.

Out of scope for this cycle (deferred): templated Application catalogs ("App Templates"),
multi-cluster Applications, parameterized child generation (e.g. "create 5 sites from this
template"), promotion across environments (dev → stg → prod), versioned Application releases.

## Architectural fit

Per `AGENTS.md` invariants:

- The `Application` DocType is **desired state at the aggregate level**. It does **not**
  duplicate the desired state of its children. Rather, an Application owns *links* to its
  children plus a small set of orchestration fields (status, last-deploy timestamp, operation
  token).
- `Application` operations enqueue background jobs that operate on its **children's** existing
  task functions. Application's task layer is a thin orchestrator — it calls
  `helm_tasks.install_or_upgrade_release`, `site_tasks.create_site_task`, etc. via Frappe's
  enqueue with appropriate dependency ordering.
- Application has its own `operation_token` for the orchestrator-level operation; children
  retain their own tokens for their individual operations. The orchestrator never bypasses the
  child's tokenization.
- Health is **computed**, not persisted: an Application's effective health is read on demand
  by aggregating its children's live status (DocType field reads only, no cluster calls at the
  Application layer — the children already do that).

## Scope

### New DocType

**`Application`** (top-level, not a child). Fields:

| Field | Type | Notes |
|---|---|---|
| `application_name` | Data | Operator-set, immutable. |
| `cluster` | Link → Kubernetes Cluster | Required, immutable. Single-cluster only this cycle. |
| `namespace` | Data | The shared namespace for this Application's resources. |
| `helm_release` | Link → Helm Release | Required, 1:1. Created on first deploy if blank. |
| `service_bundles` | Table → Application Service Bundle | 0..n links to `Service Bundle` rows. |
| `frappe_sites` | Table → Application Frappe Site | 0..n links to `Frappe Site` rows. |
| `status` | Select | `Draft`, `Deploying`, `Active`, `Degraded`, `Failed`, `Uninstalling`. |
| `operation_type` | Select | `Deploy`, `Uninstall`. |
| `operation_token` | Data, hidden, read-only | Same pattern as other DocTypes. |
| `operation_started_at` | Datetime | For staleness check. |
| `last_deployed_at` | Datetime | Set on successful end-to-end deploy. |
| `health_summary` | Small Text | One-line rollup of children's status, refreshed on form load. |

Two new child DocTypes (lightweight join tables): `Application Service Bundle` and
`Application Frappe Site`, each with one `Link` field and a `display_order` integer.

`autoname`: `<cluster>/<application_name>`. `validate`: `namespace` immutable after first deploy
(child resources may already be there); `cluster` immutable always.

`on_trash`: refuses any non-`Draft` row with the standard message — the operator must call
`uninstall_application` first (which itself uninstalls children in the right order, see below).

### Whitelisted methods (`Application` controller)

- `deploy_application() -> dict` — orchestrator-level. Rotates `operation_token`. Plans the
  deploy graph: Helm Release first, then Service Bundles, then Frappe Sites (sites depend on
  the bench release being up). Enqueues a single chain task that drives each step.
- `uninstall_application(force: bool = False) -> dict` — reverse-order: Frappe Sites delete,
  Service Bundles delete, Helm Release uninstall. The `force` flag flows through to the
  child uninstalls (typed-confirmation dialog at the UI level for Application-scope force).
- `get_application_health() -> dict` — read-only, called on form open. Returns a dict
  `{overall_status, child_statuses: [...], last_refreshed_at}` by reading each child's `status`
  field directly (`frappe.db.get_value`) — no live cluster calls at this layer; children's own
  reconciliation handles that.

### New task (`kubeport/tasks/application_tasks.py`)

- **`deploy_application_task(application_docname, operation_token, ...)`**:
  - Resolves the Application's children and ordering.
  - For each step, calls the **child's** existing whitelisted task function via
    `frappe.enqueue`, then **waits for the child's status to terminalize** (Active / Failed /
    Deployed / Degraded) by polling the child row's status with a 10-minute per-step timeout
    and a token-rotation guard.
  - Token-guarded writeback at every transition: re-checks the Application's `operation_token`
    before any update, drops writeback silently on supersession.
  - Sets the Application's status to `Active` when every child is in a terminal-success state
    (`Deployed` for releases, `Active` for sites, applied for bundles).
  - Sets status to `Failed` on any child failure; subsequent steps are not attempted.
  - Sets to `Degraded` if every child is terminal-success but at least one is `Degraded`.

- **`uninstall_application_task(application_docname, operation_token, force, ...)`**: mirror in
  reverse. Idempotent on children already in `Draft` / not-found.

### Reconciliation

- New tick branch: `_reconcile_applications()` in `kubeport/tasks/reconciliation.py`.
  - Recomputes `health_summary` and `status` from children for `Active` / `Degraded` /
    `Deploying` rows. No cluster calls at this layer; children's own reconciliation already
    runs against the cluster.
  - Recovers stuck `Deploying` / `Uninstalling` rows older than
    `_APPLICATION_OPERATION_STALE_SECONDS = 1800` by re-evaluating children: if every child is
    terminal, the Application also terminalizes; otherwise, status detail records "stuck on
    <child docname>" and the row is left for operator action.

### Discovery integration (small but load-bearing)

- `kubeport/api/discovery.py:get_cluster_discovery` returns each live release annotated with
  whether it has a tracking `Helm Release`. Extend the annotation to also report whether the
  release belongs to an `Application` (so the discovery UI can offer "Adopt as Application"
  in addition to the existing "Adopt as Helm Release").
- `adopt_helm_release` gets a sibling `adopt_release_as_application` that creates a fresh
  `Application` row with the Helm Release link populated and an empty Frappe Sites table,
  baselined as `Active` if the release is healthy.

### UI (`application.js`)

- A single status indicator at the top of the form, color-coded the same way as Helm Release
  (green / yellow / red / blue / orange).
- A child-table panel for each of the three child types, with inline status pills per row and
  a "Open" button that navigates to the child's form.
- "Deploy", "Uninstall", "Re-evaluate Health" buttons on the form (the third is a free
  refresh that re-runs `get_application_health`).
- Realtime listener for `application_status_update` and for each child's status update channel
  — re-renders the child-table pills without a full reload.

### Tests

- `test_application.py` (new): autoname; immutability of cluster/namespace; deploy gate (refuses
  to enqueue while another op is in flight); `get_application_health` rollup logic for every
  combination of child statuses (every "all green", "one degraded", "one failed", "one missing"
  case); on_trash rules.
- `test_application_tasks.py` (new): orchestrator step ordering on deploy; reverse ordering on
  uninstall; token-rotation guard at every transition; per-step timeout; "child fails" path
  stops the chain and marks the Application `Failed`.
- `test_reconciliation.py`: new branch coverage for `_reconcile_applications`, including stuck
  recovery and rollup recomputation.
- `test_discovery.py`: `adopt_release_as_application` smoke test.
- `test_helm_release.py` and `test_frappe_site.py` regression: child operations enqueued by the
  Application orchestrator do not bypass the child's own tokenization (a manual op on the child
  while the Application is mid-deploy is correctly recognized as a supersession on the child
  level).

## When implementation is fulfilled

The cycle is done when **all** of the following hold:

1. **One-click full-stack deploy works on a real cluster.** From a fresh `Draft` Application
   row populated with a Helm Release link (chart `erpnext`), one Service Bundle, and two
   Frappe Site rows: a single click on `Deploy` drives the row to `Active` with all children in
   their healthy terminal states. Verified inside the dev container on a real cluster.
2. **One-click full-stack uninstall works.** From an `Active` Application: one click on
   `Uninstall` (with typed force confirmation if any child requires it) drives the row to
   `Draft` with every child in its terminal-removed state (sites' rows deleted, release's row
   in `Draft`, bundle's row in `Draft`).
3. **Health rollup is correct for every cell of the matrix.** Every combination of children's
   statuses produces the documented Application-level status. Direct table-driven test.
4. **Tokenization is layered correctly.** A direct test where the operator clicks `Deploy` on
   an Application, then mid-deploy manually clicks `Deploy` on the underlying Helm Release:
   the Application's worker recognizes its own token has rotated *or* the child's child-level
   token has changed underneath it, and drops its writeback for that step. The child's manual
   deploy proceeds independently.
5. **No duplication of desired state.** Confirmed by inspection: the Application row contains
   only links + orchestration fields. Chart name, chart version, values YAML, site name, db
   credentials are read from the children at all times. The `Application` form does not
   surface fields that already live on its children; it links to them.
6. **Reconciliation tick covers the new DocType.** Direct test for the
   `_reconcile_applications` branch including stuck-recovery and rollup-recomputation paths.
7. **All test suites green.** New `test_application.py` and `test_application_tasks.py` plus
   the regression suites listed above. No new skipped tests. No regressions in
   `test_release_health` / `test_discovery` / `test_helm_tasks` / `test_reconciliation` /
   `test_site_tasks`.
8. **Docs updated in the same cycle.** `README.md` (new feature line + diagram of the layering
   if helpful), `docs/codebase-summary.md` (new module pointers, updated layer diagram),
   `docs/control-plane-state.md` (new section under "Capabilities"), `CHANGELOG.md` (ADR-style
   entry), `AGENTS.md` (new invariants: "Application is read-mostly", "Application
   orchestration never bypasses child tokenization", "Application desired state is links
   plus orchestration metadata only").
9. **No new TODO/FIXME/skip markers** in any file changed by the cycle.

## Critical files

- New: `kubeport/kubeport/doctype/application/` (DocType + controller + JS),
  `kubeport/kubeport/doctype/application_service_bundle/`,
  `kubeport/kubeport/doctype/application_frappe_site/`, `kubeport/tasks/application_tasks.py`,
  `kubeport/tests/test_application.py`, `kubeport/tests/test_application_tasks.py`.
- Modify: `kubeport/tasks/reconciliation.py` (new tick branch + staleness constant),
  `kubeport/api/discovery.py` (annotation + new adopt method),
  `kubeport/hooks.py` (asset paths if needed),
  `AGENTS.md` (new invariants),
  `kubeport/tests/test_reconciliation.py` and `test_discovery.py` (new coverage).

## Risks and mitigations

- **Architectural risk: Application becomes a half-built aggregate.** The cycle's discipline
  ("read-mostly aggregator with thin orchestration") is the mitigation. Resist the temptation
  to add Application-level chart-version pinning, values overrides, or templated child
  generation in this cycle; those are templated-catalog territory and belong to a follow-up.
- **Token-layering bugs.** The failure mode is "Application worker writes back stale child
  status because it polled before the child's token rotated." Mitigation: every Application
  worker writeback re-reads both the Application's token *and* the child's token before
  acting, with a direct test for the race.
- **Reconciliation cost.** Rollup-recomputation runs every tick across every Application. At
  scale this is an N×M read. Mitigation: rollup uses a single batched `frappe.db.sql` per tick
  rather than per-row reads. Acceptable for hundreds of Applications; revisit at thousands.
- **UI complexity drift.** Application form risks duplicating the Helm Release form. Mitigation:
  the form does not surface child fields directly — every child detail is a click-through.
  The Application form is intentionally sparse.

## Known follow-ups (explicitly deferred)

- **App Templates / Catalog**: parameterized Application templates that can be instantiated
  with a few inputs. The natural next cycle if this one lands clean.
- **Multi-cluster Applications**: a single Application spanning resources in more than one
  cluster (cross-cluster bench + central observability stack).
- **Promotion**: a "promote to stg" / "promote to prod" workflow that copies an Application's
  desired spec to another cluster with environment-specific overrides.
- **Versioned Application releases**: track Application-level deployments as versioned
  artifacts with rollback at the Application layer, separate from Helm rollback.
- **Parameterized child generation**: "create N sites from this template" rather than each
  site as an explicit child row.
