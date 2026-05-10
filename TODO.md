# Kubeport Roadmap — 9/10 → 10/10 Thesis

> Execution-ordered TODO for AI coding agents. Each item is self-contained: a fresh
> agent can pick it up cold from the linked files. Read the **Agent Preamble** first.
> When you finish a task, mark it `✅ DONE <YYYY-MM-DD> — <commit/PR>` in place; do
> not delete the entry.

---

## Agent Preamble (read before touching anything)

### Hard invariants (from `AGENTS.md` and `CLAUDE.md`)

1. **Desired state → MariaDB. Observed state → live cluster queries. Never mix.**
2. **All cluster mutations go through `frappe.enqueue(..., queue="long", enqueue_after_commit=True)`.** Never call Helm/K8s mutating APIs from the web thread.
3. **Discovery is read-only.** Never persist discovered state.
4. **Concurrency**: rotate per-run `operation_token` / `sync_token`; re-check before any state write. Never `doc.reload()` in a worker — use `frappe.db.get_value` / `db_set`.
5. **Form rendering**: external cluster data via `frappe.xcall` + client-side rendering, never `doc.onload`.
6. **Cluster scoping**: build clients via `get_k8s_api_client(cluster_name)`; no global state.
7. **Type annotations on every whitelisted API method** (`require_type_annotated_api_methods = True`).

### Style
- Tabs, double quotes, 110 cols, `ruff format` for Python, `prettier` for JS/CSS.
- ASCII unless the file already has non-ASCII.
- Default to no comments; only when the **why** is non-obvious.

### Toolchain
- **Bench runs inside the dev container, NOT on the host.** `bench` commands fail on the host. Sibling repo at `~/workspace/school/tfg` holds the dev container; container is often stopped between sessions.
- **Do not install tooling locally.** No `pip install`, `brew`, `apt`. Use the containerised lint:
  - `make fmt` — format in place
  - `make lint` — report
  - `make fix` — autofix + format
  - `make lint-check` — exact CI dry-run
- Pre-commit must be installed inside the bench checkout: `cd apps/kubeport && pre-commit install`.
- Tests: `bench --site <site> run-tests --app kubeport --doctype <DocType>` — only inside the dev container.

### Git hygiene
- **Conventional, single-line commit messages. No body. No `Co-Authored-By` trailer.** Match the existing `git log` style.
- Branch names follow: `fix/`, `feat/`, `chore/`, `refactor/`, `docs/`, `style/`, `ci/`, `test/`.
- Never amend; always create a new commit.
- Never use `--no-verify`.

### Documentation policy
- If a change affects discovery, tasks, or state semantics, update the relevant doc in `docs/` **in the same commit**.
- Docs map: `README.md` (entry), `AGENTS.md` (invariants), `CONTRIBUTING.md` (dev), `SECURITY.md` (disclosure), `docs/architecture.md` (C4 + invariants), `docs/operator-guide.md` (workflows), `docs/control-plane-state.md` (capability + gap inventory), `docs/codebase-summary.md` (per-module reference), `docs/thesis.md` (project framing), `CHANGELOG.md` (decision log).

---

## P0 — STABILIZATION (blocks every later milestone)

### TODO-01 — `fix/k8s-client-py314-drift` ✅ DONE 2026-05-10 — dcdb6a3

**Goal**: Make `kubeport/tests/test_k8s_client.py` pass against Python 3.14 + the current `kubernetes` client.

**Context**: `docs/control-plane-state.md` "Next Steps" line currently lists this as a pre-existing failure: `test_k8s_client (Python 3.14 / kubernetes-client API call signature drift)`. A thesis defense cannot ship with a red test in master.

**Approach**:
1. Run the test inside the dev container: `bench --site test_site run-tests --app kubeport --module kubeport.tests.test_k8s_client`.
2. Read the failure. Likely a kwarg renamed or a signature reordered between client versions consumed by `kubeport/utils/k8s_client.py` (180 lines, single file).
3. **Fix the consumer (`utils/k8s_client.py`)**, not the test, unless the test asserts behaviour the new client legitimately changed — then update the assertion and add a comment naming the upstream change.
4. Re-run the full reconciliation suite to confirm no regression: `bench --site test_site run-tests --app kubeport`.

**Acceptance criteria**:
- `test_k8s_client` passes locally and in CI (`.github/workflows/ci.yml`).
- No new failures elsewhere.
- `make lint-check` clean.

**Files**: `kubeport/utils/k8s_client.py`, `kubeport/tests/test_k8s_client.py`.

---

### TODO-02 — `fix/reconcile-service-bundles-test` ✅ DONE 2026-05-10 — a592928

**Goal**: Make the failing reconcile-service-bundles test pass.

**Context**: Same `control-plane-state.md` line lists `test_reconcile_service_bundles` as needing investigation independent of Frappe Site work.

**Approach**:
1. Run: `bench --site test_site run-tests --app kubeport --module kubeport.tests.test_reconciliation` and isolate the failing case.
2. Diagnose root cause. Service Bundle reconciliation lives in `kubeport/tasks/reconciliation.py` (1925 lines — search for `service_bundle` / `reconcile_service_bundles`). Resource existence check is in `kubeport/utils/k8s_resources.py`.
3. Fix the underlying issue. Do not silence the test, do not add `@skip`, do not weaken the assertion.

**Acceptance criteria**:
- All `test_reconciliation.py` tests pass in CI.
- Root cause documented in the commit message body (one line: e.g., `fix: service bundle reconcile mishandles 404 from custom resource list`).

**Files**: `kubeport/tasks/reconciliation.py`, `kubeport/utils/k8s_resources.py`, `kubeport/tests/test_reconciliation.py`.

---

### TODO-03 — `chore/strip-known-failures-disclaimer`

**Goal**: Remove the "Pre-existing test failures" disclaimer once TODO-01 and TODO-02 are merged.

**Approach**:
- Edit `docs/control-plane-state.md`: delete the line `6. **Pre-existing test failures**: ...` under `## Next Steps`.
- No other doc changes; TODO-01/02 commits already covered the code.

**Acceptance criteria**:
- No remaining mention of "pre-existing test failures" in `docs/`.
- CI green.

**Depends on**: TODO-01, TODO-02.

**Files**: `docs/control-plane-state.md`.

---

## P1 — EVALUATION HARNESS (the empirical chapter that earns most of the 9→10 delta)

### TODO-04 — `feat/eval-harness`

**Goal**: A reproducible end-to-end evaluation harness that boots an ephemeral k3d cluster and runs the golden Kubeport workflow, emitting a machine-readable report.

**Why this matters**: `docs/thesis.md` §5 currently claims "Met" for each objective without quantitative evidence. This harness is the source of the numbers that will populate §6 Evaluación.

**Scope**:
1. New top-level directory: `eval/`.
2. `eval/Makefile` (or extend root `Makefile` with `make eval`) that:
   - Boots a fresh k3d cluster (if one is not already present, it will reuse the dev container's bench site for Kubeport itself).
   - Drives the golden path through the Kubeport REST/Frappe API (use `frappe.client.get_list` etc. via HTTP), or via a `bench execute` helper, **not** by simulating clicks:
     1. Create a `Kubernetes Cluster` row pointing at the k3d cluster.
     2. Create a `Helm Repository` row for the curated `frappe/erpnext` repo.
     3. Trigger chart sync; wait until `Helm Chart` rows appear.
     4. Create a `Helm Release` for ERPNext using the curated `Kubeport Site Image`.
     5. Wait until release reaches `Deployed`.
     6. Create a `Frappe Site` linked to the release; wait until `Active`.
     7. Trigger `bench migrate`; wait until back to `Active`.
     8. Trigger backup; wait for `Frappe Site Backup` row to reach `Available`.
     9. Trigger restore from that backup; wait for `Active`.
     10. Drop the site; confirm row deletion.
   - Per-phase wall-clock timing recorded.
   - Emits `eval/results/<utc-timestamp>.json` with `{ phase, started_at, finished_at, duration_seconds, status }` per phase and an overall summary.
3. A reference `eval/results/sample.json` checked in with one good run for thesis quoting.
4. `eval/README.md` explaining setup (k3d version, prerequisites), the golden path, and how to interpret the JSON.

**Constraints**:
- Must run inside the existing dev container; no host-side installs (see Preamble).
- Must not require network access to private registries — uses only the curated public GHCR Site Image.
- Idempotent: rerunning produces a new timestamped report; never overwrites previous reports.

**Acceptance criteria**:
- `make eval` from a clean dev container produces a JSON report whose every phase is `status: "passed"`.
- `eval/results/sample.json` is committed and matches the schema documented in `eval/README.md`.
- Total runtime < 25 min on a stock developer laptop.

**Files**: new `eval/` tree, root `Makefile`.

---

### TODO-05 — `feat/eval-fault-injection`

**Goal**: Empirically validate the robustness defenses listed in `docs/control-plane-state.md` §Robustness Properties.

**Depends on**: TODO-04 (reuses the harness).

**Scope**: Implement these four scenarios as `eval/faults/<name>.py` scripts driven by `make eval-faults`:

| Scenario | Inject | Expected behaviour | Measure |
|---|---|---|---|
| `worker_kill_mid_helm_upgrade` | `kill -9` the RQ long worker after `helm upgrade --install` is invoked but before status writes back | Stale-operation reconciler recovers the row within 30 min; final status `Deployed`; spec hash matches desired | MTTR (start of inject → row reaches `Deployed`) |
| `job_ttl_expired_before_reconcile` | Force-delete the operation Job before reconciliation tick reads it | Reconciliation falls back to ground-truth bench probe; row reaches correct terminal state, not `Failed` by default | Final status, time to terminal |
| `pod_exec_timeout_during_site_probe` | Inject a sleep into the pod-exec call wrapping `bench list-apps` | `_probe_site_state` returns `unknown`; row stays `In Progress` for that tick; next tick recovers | Number of ticks deferred; final status |
| `corrupt_archive_size_sidecar` | Truncate the `<archive>.size` file mid-flight on the `kubeport-backups` PVC | PVC probe returns `missing`/`unknown`; backup row is marked `Failed`, archive trash cleanup runs, archive file removed from PVC | Final status, archive file presence on PVC |

**Output**: `eval/results/faults-<utc-timestamp>.json` with `{ scenario, injected_at, recovered_at, mttr_seconds, expected, observed, passed }` per row.

**Acceptance criteria**:
- All four scenarios pass end-to-end.
- MTTR for `worker_kill_mid_helm_upgrade` ≤ 30 min (matches the documented stale-op recovery window).
- `eval/README.md` updated with a "Fault scenarios" table mapping each scenario to the defended invariant in `docs/control-plane-state.md`.

**Files**: `eval/faults/`, `eval/results/`, `eval/README.md`, root `Makefile` (add `eval-faults` target).

---

### TODO-06 — `feat/eval-baseline-comparison`

**Goal**: Side-by-side comparison of the same workflow run with raw `kubectl + helm` vs. through Kubeport. Validates the SOTA claim in `docs/thesis.md` §2.

**Depends on**: TODO-04.

**Scope**:
1. `eval/baseline/run.sh` — bash script that performs the same 10-step workflow with raw `kubectl` and `helm` only (no Kubeport).
2. Records: total wall-clock per phase, distinct shell commands issued, manual interventions required (counted as `manual_steps`).
3. Emits `eval/results/baseline-<timestamp>.json` matching the harness schema plus `commands_issued` and `manual_steps` fields.
4. `eval/baseline/README.md` documents the comparison methodology — same target cluster, same chart, same image, otherwise no Kubeport at all.

**Acceptance criteria**:
- `make eval-baseline` produces a JSON report.
- `eval/results/sample-baseline.json` checked in.
- A `eval/results/comparison.md` summarises Kubeport vs. baseline in a Markdown table (phase, baseline-seconds, kubeport-seconds, commands, manual-steps).

**Files**: `eval/baseline/`, `eval/results/`, root `Makefile`.

---

### TODO-07 — `feat/eval-scaling`

**Goal**: Characterise how Kubeport scales with N persisted rows.

**Depends on**: TODO-04.

**Scope**:
1. `eval/scaling/seed.py` — bulk-creates N synthetic Helm Release / Service Bundle / Frappe Site rows in `Deployed` / `Active` state without touching a real cluster (use a mock cluster fixture for read paths). Use `frappe.db.bulk_insert` to avoid hook overhead.
2. Run reconciliation N ∈ {1, 10, 100, 1000} times, measure tick latency.
3. Sample helm-subprocess latency from the existing logs across the harness run (TODO-04) — extract via a structured-log post-processor in `eval/scaling/extract_latencies.py`.
4. Emit `eval/results/scaling.json` and plot to `eval/results/scaling-tick-latency.png` using matplotlib (matplotlib is already available in the dev container — confirm before adding a dep).

**Acceptance criteria**:
- `make eval-scaling` produces JSON + PNG.
- Tick-latency growth shape (linear, log-linear) is annotated in the JSON `regression` field.

**Files**: `eval/scaling/`, root `Makefile`.

---

### TODO-08 — `docs/thesis-evaluation-chapter`

**Goal**: A real "Evaluación" chapter in the thesis grounded in TODO-04..07 outputs.

**Depends on**: TODO-04, TODO-05, TODO-06, TODO-07.

**Scope**:
1. New `docs/evaluation.md` — full chapter with subsections: Functional, Reliability under fault injection, Comparative baseline, Scaling envelope. Each subsection cites the `eval/results/*.json` it draws from.
2. Update `docs/thesis.md`:
   - Insert §6 "Evaluación" between current §5 and §6 (renumber Limitations → §7, Future Work → §8, References → §9).
   - The new §6 is a 1-page summary table per objective citing the measured number; full discussion lives in `docs/evaluation.md`.
3. Update `README.md` documentation map and `CLAUDE.md` doc index to list `docs/evaluation.md`.

**Acceptance criteria**:
- Every quantitative claim in `docs/evaluation.md` cites a path under `eval/results/`.
- `docs/thesis.md` §5 "Met" claims now reference §6 measurements rather than asserting unevidenced.

**Files**: `docs/evaluation.md`, `docs/thesis.md`, `README.md`, `CLAUDE.md`.

---

## P2 — FORMAL & SECURITY FRAMING (cheap thesis-writing wins)

### TODO-09 — `docs/formal-invariants`

**Goal**: Restate the four design invariants as numbered Safety / Liveness / Eventual-Consistency properties with code-level witnesses.

**Scope**:
1. Rewrite `docs/architecture.md` §3 ("Key Design Invariants"). Replace each prose invariant with a numbered property of the form:
   - `P1 (Safety)`: <statement> — Witness: `<file>:<line>`.
   - `P2 (Liveness)`: <statement> — Witness: `<file>:<line>`.
   - `P3 (Eventual Consistency)`: <statement> — Witness: `<file>:<line>`.
2. New `docs/fault-model.md`:
   - Enumerated tolerated faults (worker crash, Job TTL expiry before reconcile, pod-exec transient failure, hash collision on Job names, stale operation lock, orphan Job, kubeconfig API timeout).
   - For each: defended-by mechanism (token rotation, `activeDeadlineSeconds`, three-state probe, label validation, stale-op reconciler, orphan sweep), code-level witness, and recovery time bound.
3. Cross-link from `docs/architecture.md` and `AGENTS.md`.

**Acceptance criteria**:
- Every property in `architecture.md` §3 has a `<file>:<line>` witness that resolves on `HEAD`.
- `docs/fault-model.md` enumerates ≥ 7 faults, each with a witness and a documented recovery upper bound.

**Files**: `docs/architecture.md`, `docs/fault-model.md` (new), `AGENTS.md`, `README.md` (doc map).

---

### TODO-10 — `docs/threat-model`

**Goal**: A full trust-boundary analysis. Currently `SECURITY.md` exists but no boundary map.

**Scope**:
1. New `docs/threat-model.md` with:
   - **Trust-boundary diagram** (Mermaid): Operator → Frappe Web → RQ Worker → Helm subprocess → Kubeconfig at rest → Cluster API → Bench pod-exec.
   - **Boundary table**: per boundary, identify the trust direction, the data crossing it, the mitigations (CSRF on whitelisted endpoints, kubeconfig encryption-at-rest via Frappe encrypted fields, scoped K8s API client, RBAC scope on the in-cluster service account, allowlisted resource kinds in Service Bundle, allowlisted commands in Kubernetes Command).
   - **Threat catalog** (STRIDE per boundary, abbreviated).
   - **Justification of `Kubernetes Command` Delete allowlist** (Pod, Job, ConfigMap only — explain why Secret/PVC/Deployment/StatefulSet are excluded).
2. Cross-link from `SECURITY.md`.

**Acceptance criteria**:
- Every whitelisted endpoint (`kubeport/api/*.py`) and every privileged worker call (`kubeport/tasks/*.py`) appears in the boundary table.
- STRIDE catalog has ≥ 1 entry per boundary, with mitigation linked to code.

**Files**: `docs/threat-model.md` (new), `SECURITY.md`, `README.md` (doc map).

---

### TODO-11 — `docs/sota-bibliography`

**Goal**: Replace the 6-row product table in `docs/thesis.md` §2 with a real CS-research SOTA section + bibliography.

**Scope**:
1. Expand `docs/thesis.md` §2:
   - Keep the existing product comparison table.
   - Add prose subsections: "Operator pattern and reconciliation loops", "Desired-state vs observed-state in declarative systems", "Background-execution patterns in business platforms".
   - Each subsection cites primary sources from the bibliography.
2. New `docs/references.bib` (BibTeX) with **at least 12 primary references**, suggested set:
   - Burns et al., "Borg, Omega, and Kubernetes", ACM Queue 2016.
   - Verma et al., "Large-scale cluster management at Google with Borg", EuroSys 2015.
   - Brewer, "Kubernetes: The Surprisingly Affordable Platform for Global Companies".
   - Hightower, Burns, Beda, "Kubernetes Up & Running" (controller chapter).
   - Helm 3 design proposal (`helm/community` repo).
   - GitOps whitepaper (Weaveworks).
   - Lamport, "Time, Clocks, and the Ordering of Events" (eventual consistency).
   - Vogels, "Eventually Consistent" CACM 2009.
   - Brewer, CAP theorem.
   - Frappe Framework documentation (canonical URL).
   - Dean & Ghemawat, "MapReduce" (background work decomposition rationale).
   - The K8s controller manifesto / operator pattern paper (Red Hat / CoreOS).
3. Add `docs/thesis.md` §9 "Bibliografía" listing the references in IEEE or ACM style.

**Acceptance criteria**:
- ≥ 12 entries in `docs/references.bib`.
- Each citation in `docs/thesis.md` resolves to a `.bib` entry.
- `docs/thesis.md` §2 prose subsections each cite ≥ 2 references.

**Files**: `docs/thesis.md`, `docs/references.bib` (new), `README.md` (doc map).

---

## P3 — CS-MAJOR-GRADE DIFFERENTIATORS

### TODO-12 — `test/property-fsm-frappe-site`

**Goal**: Hypothesis-based property tests for the `Frappe Site` finite state machine.

**Context**: The state machine is documented at `docs/control-plane-state.md` §Frappe Site Provisioning: `Draft → In Progress → Active | Failed`, plus `Active → Migrating → Active | Failed` and `Active|Failed → Deleting → [doc deleted] | Failed`. Property tests turn this from prose into a checked invariant.

**Scope**:
1. New `kubeport/tests/test_property_fsm_frappe_site.py`.
2. Use `hypothesis.stateful.RuleBasedStateMachine`. Rules: `create`, `cancel`, `migrate`, `backup`, `restore`, `drop`, `tick_reconciliation`. Use mock K8s clients (existing fixtures in `kubeport/tests/`) so the test is hermetic.
3. Invariants:
   - `inv_no_terminal_inflight`: after a finite sequence terminating in a `tick_reconciliation`, the row is never simultaneously in an in-flight status and `operation_token` rotated by a concurrent worker.
   - `inv_transitions_documented`: every observed transition appears in the documented arrow list (encode the arrows as a constant in the test).
   - `inv_archive_outlives_site`: dropping a `Frappe Site` while it has an `Available` `Frappe Site Backup` leaves the backup row intact.
4. Run ≥ 1000 examples in CI (set `@settings(max_examples=1000)`).

**Acceptance criteria**:
- Test passes with `max_examples=1000` in `.github/workflows/ci.yml`.
- All three invariants checked.
- Hypothesis is added to dev dependencies, not runtime — confirm `pyproject.toml` doesn't pull it into the install.

**Files**: `kubeport/tests/test_property_fsm_frappe_site.py` (new), `pyproject.toml`.

---

### TODO-13 — `test/property-fsm-helm-release`

**Goal**: Same as TODO-12, for the Helm Release FSM.

**Context**: `Draft → In Progress → Deployed | Degraded | Failed → Uninstalling → Draft` (`docs/control-plane-state.md` §Helm Release Management).

**Scope**:
1. New `kubeport/tests/test_property_fsm_helm_release.py`.
2. Rules: `deploy`, `upgrade`, `rollback`, `uninstall`, `force_uninstall`, `tick_reconciliation`.
3. Invariants:
   - `inv_dependent_site_blocks_uninstall`: an uninstall while a linked `Frappe Site` is `Active` always blocks unless `force=True`.
   - `inv_failed_cannot_be_directly_deleted`: deleting a `Failed` row outside of `Draft` is rejected.
   - `inv_spec_hash_monotonic`: `last_applied_spec_hash` only changes after a successful deploy/upgrade/rollback.

**Acceptance criteria**:
- Same as TODO-12: 1000 examples, all invariants checked, hermetic.

**Files**: `kubeport/tests/test_property_fsm_helm_release.py` (new).

**Depends on**: TODO-12 (share the Hypothesis dev-dep wiring).

---

### TODO-14 — `feat/internal-observability`

**Goal**: Make Kubeport itself measurable. Counters and histograms surfaced on the existing operator workspace dashboard, plus correlation-ID-threaded structured logs.

**Scope**:
1. New module `kubeport/utils/metrics.py`:
   - In-memory counters (process-local) for `reconcile_ticks_total`, `stale_ops_recovered_total`, `orphan_jobs_swept_total`.
   - A small histogram type for helm subprocess latency, queryable via percentile.
   - Optional: persist a daily aggregate row to a new `Kubeport Metric Sample` doctype if it fits — only if it doesn't violate desired/observed split (it doesn't: this is internal observed state of Kubeport itself, not of the cluster). Defer if it stretches the milestone.
2. Wire counters into the existing reconciliation loop at `kubeport/tasks/reconciliation.py` and the helm subprocess wrapper at `kubeport/utils/helm.py`.
3. Add a `correlation_id` parameter (UUID4) generated at enqueue time, threaded through `frappe.enqueue` kwargs, included in every log line emitted by the worker via a `frappe.logger().bind` style helper.
4. Surface metrics on the existing operator workspace (`kubeport/kubeport/workspace/...`) as new number cards reading from `kubeport/api/dashboard.py`.

**Constraints**:
- Don't add Prometheus or external metrics deps. Keep it Frappe-native.
- Don't violate the desired/observed invariant: any persisted metric is metadata about Kubeport itself, never about the cluster.

**Acceptance criteria**:
- `frappe.local` log of one operation contains the same `correlation_id` from web → enqueue → worker.
- Operator workspace shows live counters that increment under load.
- Tests in `kubeport/tests/test_api_dashboard.py` extended to cover the new metrics.

**Files**: `kubeport/utils/metrics.py` (new), `kubeport/tasks/reconciliation.py`, `kubeport/utils/helm.py`, `kubeport/api/dashboard.py`, `kubeport/kubeport/workspace/`, `kubeport/tests/test_api_dashboard.py`.

---

### TODO-15 — `feat/chaos-ci`

**Goal**: One CI job that empirically demonstrates recovery under a realistic fault.

**Depends on**: TODO-04 (eval harness), TODO-05 (fault scenarios).

**Scope**:
1. New `.github/workflows/chaos.yml`:
   - Spins up k3d on a GitHub-hosted runner.
   - Boots a Frappe v16 bench (reuse the `test` job pattern in `.github/workflows/ci.yml`).
   - Runs the `worker_kill_mid_helm_upgrade` scenario from TODO-05.
   - Asserts the Helm Release row reaches `Deployed` within the documented stale-op recovery window.
2. Required-status on PR (after one green run; ship with `continue-on-error: true` on the first iteration, mirroring the pattern in CHANGELOG `2026-05-09 — Code-quality cleanup pass and CI test gating`).

**Acceptance criteria**:
- Workflow runs on PR.
- One scenario passes end-to-end.
- Documented in `docs/evaluation.md` as the basis for the fault-injection §.

**Files**: `.github/workflows/chaos.yml` (new), `docs/evaluation.md`.

---

## P4 — PRODUCTION READINESS

### TODO-16 — `docs/deploy-guide`

**Goal**: Operator-deployable guide. `docs/control-plane-state.md` §Open Gaps "Operator Documentation" lists this as missing.

**Scope**:
1. New `docs/deploy.md` covering:
   - Topology: Kubeport-in-cluster vs. Kubeport-out-of-cluster, when to choose each.
   - In-cluster auth setup with the SA / Role / RoleBinding ship in TODO-17.
   - Helm binary packaging (Dockerfile snippet adding `helm` to a Frappe bench image; the kubernetes-client wheel matrix on Python 3.14).
   - Resource limits (CPU/RAM recommendations for web, RQ long worker, scheduler).
   - Monitoring: where to scrape the metrics from TODO-14.
   - Control-plane backup: how to back up the Frappe site running Kubeport itself.
2. Cross-link from `README.md` install section.

**Acceptance criteria**:
- A reviewer can follow `docs/deploy.md` end-to-end on a fresh cluster without consulting external docs.
- Includes a "Quick smoke" section pointing at `make eval` (TODO-04).

**Files**: `docs/deploy.md` (new), `README.md`, `CLAUDE.md` (doc map).

---

### TODO-17 — `feat/rbac-manifests`

**Goal**: Ship least-privilege Kubernetes RBAC manifests for the in-cluster auth mode.

**Scope**:
1. New `deploy/rbac/`:
   - `serviceaccount.yaml` — `kubeport` SA in the namespace where Kubeport runs.
   - `role.yaml` / `clusterrole.yaml` — minimum verbs Kubeport actually needs (audit `kubeport/utils/k8s_resources.py` and `kubeport/tasks/*.py` for the actual API calls; do not over-grant).
   - `rolebinding.yaml` / `clusterrolebinding.yaml`.
   - `kustomization.yaml` for `kubectl apply -k deploy/rbac/`.
2. `deploy/rbac/README.md` documenting:
   - Which verbs were granted and why (cite the API call site for each).
   - Namespaced vs. cluster-scoped: prefer namespaced where possible; cluster-scoped only for `Namespace`, `StorageClass` discovery, `ClusterRole/ClusterRoleBinding` Service-Bundle apply.
3. Smoke: `make rbac-smoke` runs `kubectl auth can-i` for each call site against the bound SA.

**Acceptance criteria**:
- `kubectl apply -k deploy/rbac/` is sufficient for a freshly created Kubeport bench in the same cluster to operate with `auth_mode: in_cluster`.
- README has a verb-justification matrix (verb, resource, justification path).

**Files**: `deploy/rbac/` (new tree), root `Makefile` (add `rbac-smoke`).

**Depends on**: TODO-16 (the deploy guide will reference these manifests).

---

## P5 — DEPTH FEATURES (pick 1–2; demonstrating closure beats listing five aspirations)

These close gaps already named in `docs/control-plane-state.md` §Open Gaps. **Pick one or two**, do them well, list the others as future work.

### TODO-18 — `feat/helm-diff-preview` (recommended pick)

**Goal**: A "Preview" button on `Helm Release` form that shows the diff between live state and the next render before deploying.

**Scope**:
1. New whitelisted endpoint `kubeport.api.helm_diff.preview_release(name: str) -> dict`.
2. Implementation: render values via the existing `kubeport/tasks/helm_tasks.py` machinery, call `helm template` (read-only, OK from web thread because it's local-only — confirm by reading the Helm CLI docs: `helm template` does not contact the cluster), then diff against `helm get manifest` output. Use a small Python diff helper, no new system dep.
3. Form UI: button beside Deploy that opens a panel showing the diff.

**Acceptance criteria**:
- Preview reports an empty diff after a successful deploy when no values change.
- Tests added in `kubeport/tests/test_helm_tasks.py` covering value-only changes and chart-version changes.

**Files**: `kubeport/api/helm_diff.py` (new), `kubeport/kubeport/doctype/helm_release/helm_release.{js,py}`, `kubeport/tests/test_helm_tasks.py`.

---

### TODO-19 — `feat/site-health-surface`

**Goal**: Mirror the Helm Release observability panel on `Frappe Site`.

**Context**: Closes the gap in `docs/control-plane-state.md` §Open Gaps "no per-Frappe-Site health surface".

**Scope**: Site-scoped pod logs, scoped events, bench-pod readiness — reusing helpers from `kubeport/utils/observability.py` (576 lines, already designed for resource-scope reuse).

**Acceptance criteria**: Site form renders three sub-views (logs, events, rollout) that respect the same staleness/auth model as the existing Helm Release panel.

---

### TODO-20 — `feat/scheduled-backups`

**Goal**: Operator-scheduled backups via cron on `Frappe Site`.

**Scope**: New cron-string field on `Frappe Site`; reconciliation tick enqueues a new `Frappe Site Backup` row when the cron is due. Integrate with retention (max-N rows or max-age).

**Acceptance criteria**: A site with `backup_schedule = "0 2 * * *"` produces one new `Available` backup per day; old backups beyond retention are auto-trashed (which already cleans up the PVC archive per `2026-05-04` CHANGELOG entry).

---

## Maintenance hygiene (run before every commit)

```bash
make fmt           # format
make lint-check    # exact CI dry-run
# inside dev container only:
bench --site test_site run-tests --app kubeport
```

If any of these fail, fix before committing. Never `--no-verify`.
