# Plan B — Helm Release Observability Drilldown

## Intent

The Helm Release form already shows per-resource readiness (rendered manifest walked against the
live cluster, eight built-in workload kinds). When a release lands `Degraded`, the operator can
see **which** resource is failing — but nothing about **why**. The next click ("show me the
logs") leads off-platform: `kubectl logs`, `kubectl describe`, `kubectl rollout history`.

Plan B closes that "why?" gap. After this cycle, every unready row in the readiness drilldown is
one click away from the data that explains it: pod logs (tailed), Kubernetes events for the
resource, and the rollout timeline for `Deployment` / `StatefulSet` / `DaemonSet`.

The cycle's goal is **operators stay in Kubeport for diagnosis**. No new DocTypes, no new
cluster-mutating paths. Read-only observability extending the existing readiness walker.

Out of scope for this cycle (deferred): pod metrics (CPU/memory), real-time log streaming over
WebSocket, full cluster event timeline (we only fetch events scoped to a specific resource),
log-search / grep across pods, exec-into-pod from the UI.

## Architectural fit

Per `AGENTS.md` and `CLAUDE.md`:

- Discovery is read-only and never persisted to MariaDB. Logs / events / rollout data are
  observed state, fetched on demand.
- API endpoints live in `kubeport/api/` and are whitelisted with type annotations.
- Form rendering uses `frappe.xcall` for external data (no `doc.onload` for live-cluster reads).
- Subprocess timeouts are conservative (30s default, matching `kubeport/utils/helm.py`).

This cycle does **not** add background jobs — every read is a synchronous K8s API call from the
web thread. That's acceptable because: payloads are bounded (log tail length, event count,
revision count), and the operator is interactively waiting on a panel they just opened.

## Scope

### New utility module

**`kubeport/utils/observability.py`** — stateless helpers, K8s API only:

- `get_pod_logs(cluster, namespace, pod, container=None, tail_lines=200, previous=False) -> str`
  - Calls `CoreV1Api.read_namespaced_pod_log` with `_request_timeout=20.0`.
  - Caps `tail_lines` at 2000 server-side. Truncates payload at 256 KB before returning to keep
    the form responsive.
  - `previous=True` fetches the previous container's logs (post-restart). Surfaced as a
    "Previous container" toggle in the UI for crash-looped containers.
- `list_resource_events(cluster, namespace, kind, name, limit=20) -> list[dict]`
  - Calls `CoreV1Api.list_namespaced_event` with `field_selector="involvedObject.name=<name>,
    involvedObject.kind=<kind>"`.
  - Returns the most-recent `limit` events, normalized to `{type, reason, message, count,
    first_seen, last_seen, source}`.
  - Defensive: K8s 1.25+ uses `events.k8s.io/v1`; fall back to `core/v1` if the v1 events API
    returns empty (some managed clusters lag).
- `get_rollout_history(cluster, namespace, kind, name, limit=10) -> list[dict]`
  - For `Deployment`: lists `ControllerRevision`s + the matching `ReplicaSet`s, returns one row
    per revision with `revision`, `created_at`, `change_cause` (from
    `kubernetes.io/change-cause` annotation), `current` (bool), `image_summary` (joined
    container-image strings).
  - For `StatefulSet` / `DaemonSet`: same `ControllerRevision`-based view.
  - For other kinds: returns empty list (gracefully unsupported, the UI hides the panel).
- `list_pods_for_resource(cluster, namespace, kind, name) -> list[dict]`
  - Resolves the pods belonging to a `Deployment` / `StatefulSet` / `DaemonSet` / standalone
    `Pod` by walking the owner-reference chain (Deployment → ReplicaSet → Pod;
    StatefulSet/DaemonSet → Pod directly). Used by the drilldown to populate the pod picker
    when the operator clicks "Logs" on a workload row.
  - Returns `[{name, container_names, phase, restart_count, container_statuses}]`.

All functions raise `RuntimeError` on cluster connectivity failure with a string the form can
render verbatim. None of them mutate.

### New API endpoints (`kubeport/api/observability.py`)

All `@frappe.whitelist()` and `frappe.only_for("System Manager")` (or whatever role gates the
existing release controller — match it). All take a `release_docname` and resolve cluster /
namespace from the row, never trusting the client to name a cluster.

- `get_release_resource_logs(release_docname, kind, name, container=None, tail_lines=200,
  previous=False) -> dict` — wraps `get_pod_logs`. For workload kinds (`Deployment`,
  `StatefulSet`, `DaemonSet`), the endpoint resolves the workload's pods first and returns a
  dict `{pods: [...], selected_pod, logs_by_pod: {<pod_name>: <log_text>}, errors_by_pod: {...}}`
  with logs for one selected ownership-proven pod per request so large workloads do not require
  one web request to read every pod log.
- `get_release_resource_events(release_docname, kind, name, limit=20) -> dict` — wraps
  `list_resource_events` and returns `{rows: [...], error: ""}`. Lookup failures return
  `{rows: [], error: "<message>"}` for panel-level graceful degradation.
- `get_release_resource_rollout(release_docname, kind, name, limit=10) -> dict` — wraps
  `get_rollout_history` and returns `{rows: [...], error: ""}` with the same failure wrapper.

### UI (`helm_release.js`)

Each unready row in the existing health drilldown table grows three actions, rendered as small
inline buttons:

- **Logs** — opens a side panel with a pod picker (when the row is a workload), a tail-lines
  selector (50 / 200 / 500 / 2000), a "Previous container" toggle, and a `<pre>` for the log
  text. Refresh button re-fetches via xcall.
- **Events** — opens a side panel with a sortable list of events scoped to this resource.
  Newest first. Each row shows type/reason/age and a tooltip with the full message.
- **Rollout** — only shown for `Deployment` / `StatefulSet` / `DaemonSet`. Opens a side panel
  with the revision list, the current revision marked, and per-revision images. **No rollback
  action this cycle.** Helm rollback already exists at the release level; surface-level workload
  rollback is intentionally deferred.

Side panels are siblings of the existing readiness panel, share the same realtime channel for
auto-refresh on `helm_release_status_update`, and close cleanly on form reload.

### Existing readiness rows enhanced

`utils/release_health.py` — minor enhancement only. The `ResourceHealth.message` field already
includes the first failing event for unready rows (the existing `_attach_warning_events`). Add
a single `pod_count` integer to the per-workload result so the form knows whether to show the
pod picker without a round-trip.

## When implementation is fulfilled

The cycle is done when **all** of the following hold:

1. **The "why?" path is one click.** From a `Degraded` Helm Release with a failing
   `Deployment`, the operator can: click the readiness row → see "Logs", "Events", "Rollout"
   buttons → click any one → get the data in under 5 seconds with no console errors. Verified
   manually on a real cluster against at least one crash-looping deployment.
2. **All four kinds covered.** Logs work for `Deployment`, `StatefulSet`, `DaemonSet`, and
   standalone `Pod` resources. Events work for all eight `_HEALTH_KINDS`. Rollout works for
   `Deployment` / `StatefulSet` / `DaemonSet` and is hidden (not greyed) for the other kinds.
3. **Bounded payloads.** Log responses are capped at 256 KB and `tail_lines` ≤ 2000 server-side.
   Event lists are capped at 50. Rollout lists are capped at 20. Direct unit tests for each cap.
4. **Graceful degradation.** When the cluster is unreachable / a pod is gone / events API is
   empty / a workload has zero pods, the form panels render a clear message and the rest of the
   form remains usable. Direct tests for each failure mode.
5. **No persistence.** No new DocType, no new column on `Helm Release`. Confirmed by inspection
   of the diff: nothing under `kubeport/kubeport/doctype/helm_release/helm_release.json` changes
   beyond at most a JS asset reference.
6. **Three test suites green.** New `kubeport/tests/test_observability.py` covers the utility
   module. `kubeport/tests/test_api_observability.py` covers the whitelisted endpoints
   (release-row resolution, role gating, malformed-input rejection). Existing
   `test_release_health` and `test_helm_tasks` remain green.
7. **Docs updated in the same cycle.** `README.md` (operator guide line about logs/events
   drilldown), `docs/codebase-summary.md` (new module), `docs/control-plane-state.md` (move
   "no full in-app drilldown for pod logs, Kubernetes event history, rollout timelines" out of
   Open Gaps), `CHANGELOG.md` (ADR-style entry).
8. **No new TODO/FIXME/skip markers** in any file changed by the cycle.

## Critical files

- New: `kubeport/utils/observability.py`, `kubeport/api/observability.py`,
  `kubeport/tests/test_observability.py`, `kubeport/tests/test_api_observability.py`.
- Modify: `kubeport/kubeport/doctype/helm_release/helm_release.js` (drilldown action buttons +
  side panels), `kubeport/utils/release_health.py` (add `pod_count` to per-workload result),
  `kubeport/hooks.py` only if a new JS asset path is needed.

## Known follow-ups (explicitly deferred)

- Real-time log streaming (WebSocket-backed tail).
- CPU/memory metrics from the metrics-server / Prometheus.
- Per-Frappe-Site observability (the same drilldown surface, but anchored on the Site row's
  pods).
- Cluster-wide event timeline (events not scoped to a single resource).
- Workload-level rollout-undo button (separate from Helm rollback).
- Grep / regex search across all pods of a release.
