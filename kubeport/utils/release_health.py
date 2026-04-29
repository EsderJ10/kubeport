"""
Helm Release Workload-Readiness Walker

Reads the rendered manifest for a Helm release (``helm get manifest``), then
queries the live cluster for each workload-class resource to decide whether
it is actually Ready.  Returned data is purely observed state — never
persisted to MariaDB — and is used by:

- ``helm_tasks.install_or_upgrade_release`` (post-deploy decision)
- ``reconciliation._reconcile_helm_releases`` (periodic recheck)
- ``HelmRelease.get_release_health`` (form-side drilldown via xcall)

Resource-kind coverage is intentionally narrow.  Workloads (Deployment,
StatefulSet, DaemonSet, Pod, Job) are the only kinds with a meaningful
"ready" signal that maps cleanly onto the document's ``Deployed`` /
``Degraded`` states.  Service / PVC / Ingress / CRDs are out of scope: they
either have no readiness or their semantics tie into separate milestones
(storage strategy, custom resources).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import frappe
from kubernetes import client
from kubernetes.client.rest import ApiException

from kubeport.utils import helm
from kubeport.utils.k8s_client import get_k8s_api_client

_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Pod", "Job")
_HELM_PENDING_STATUSES = frozenset({
	"pending-install", "pending-upgrade", "pending-rollback",
	"uninstalling",
})


@dataclass(frozen=True)
class ResourceHealth:
	"""Readiness snapshot for a single Helm-managed workload resource."""

	kind: str
	name: str
	namespace: str
	ready: bool
	# Short human label for the failing condition ("ImagePullBackOff",
	# "rollout 1/2", "Job Failed", "missing").  Empty string for healthy rows.
	reason: str
	# Longer detail useful inside the form drilldown.  Truncated at the call
	# site if it ever needs to land in ``helm_status_detail``.
	message: str

	def to_dict(self) -> dict[str, Any]:
		return {
			"kind": self.kind,
			"name": self.name,
			"namespace": self.namespace,
			"ready": self.ready,
			"reason": self.reason,
			"message": self.message,
		}


def walk(release_docname: str) -> list[ResourceHealth]:
	"""Return readiness snapshots for every workload resource in the release.

	Looks up the Helm Release row to resolve cluster / namespace / release
	name, calls ``helm get manifest`` to enumerate the release's resources,
	then queries the live cluster for each workload kind.  Non-workload kinds
	are skipped (no useful readiness signal for them at this milestone).
	"""
	release = frappe.db.get_value(
		"Helm Release",
		release_docname,
		["release_name", "namespace", "cluster"],
		as_dict=True,
	)
	if not release:
		raise ValueError(f"Helm Release '{release_docname}' was not found.")

	namespace = release.get("namespace") or "default"
	cluster_name = release.get("cluster")
	release_name = release.get("release_name")

	if not cluster_name or not release_name:
		raise ValueError(
			f"Helm Release '{release_docname}' is missing cluster or release name."
		)

	manifest = helm.get_manifest(
		release_name=release_name,
		namespace=namespace,
		cluster_name=cluster_name,
	)

	api_client = get_k8s_api_client(cluster_name)
	apps_v1 = client.AppsV1Api(api_client=api_client)
	core_v1 = client.CoreV1Api(api_client=api_client)
	batch_v1 = client.BatchV1Api(api_client=api_client)

	results: list[ResourceHealth] = []
	for resource in manifest:
		kind = str(resource.get("kind") or "")
		if kind not in _WORKLOAD_KINDS:
			continue

		metadata = resource.get("metadata") or {}
		name = str(metadata.get("name") or "")
		if not name:
			continue
		obj_namespace = str(metadata.get("namespace") or namespace or "default")

		try:
			results.append(
				_check_workload(
					kind=kind,
					name=name,
					namespace=obj_namespace,
					apps_v1=apps_v1,
					core_v1=core_v1,
					batch_v1=batch_v1,
				)
			)
		except ApiException as e:
			results.append(_resource_health_from_api_error(kind, name, obj_namespace, e))

	return results


def summarize(results: list[ResourceHealth]) -> tuple[bool, str]:
	"""Return ``(all_ready, summary)`` suitable for ``helm_status_detail``.

	The summary is one line plus the first failing resource so it stays under
	the 500-char field cap.  Full per-resource detail lives in the form
	drilldown, not the field.
	"""
	total = len(results)
	ready_count = sum(1 for r in results if r.ready)
	all_ready = total == 0 or ready_count == total

	if total == 0:
		return True, "deployed (no workload resources)"

	if all_ready:
		return True, f"deployed | {ready_count}/{total} ready"

	# Pull the first unready row for context — the panel shows the rest.
	first_unready = next((r for r in results if not r.ready), None)
	header = f"deployed | {ready_count}/{total} ready"
	if first_unready is not None:
		return False, (
			f"{header}\n- {first_unready.kind}/{first_unready.name}: {first_unready.reason}"
		)
	return False, header


def classify_release_state(
	runtime_status: str,
	walker_results: list[ResourceHealth] | None = None,
	walker_error: str | None = None,
) -> tuple[str, str]:
	"""Map Helm runtime state + workload readiness onto persisted fields.

	Policy:
	- ``deployed`` + all workloads ready -> ``Deployed``
	- ``deployed`` + unready workloads or probe error -> ``Degraded``
	- pending or non-deployed Helm states -> ``Failed``
	"""
	if runtime_status in _HELM_PENDING_STATUSES:
		return "Failed", (
			f"stuck: helm reports '{runtime_status}'. "
			"Re-run Install / Upgrade or run `helm rollback` manually to recover."
		)

	if runtime_status != "deployed":
		return "Failed", f"Helm reports: {runtime_status or 'unknown'}"

	if walker_error is not None:
		return "Degraded", f"deployed | readiness probe failed: {walker_error}"

	all_ready, summary = summarize(walker_results or [])
	return ("Deployed" if all_ready else "Degraded"), summary


def classify_release_from_cluster(
	release_docname: str,
	runtime_status: str,
	walk_fn=None,
) -> tuple[str, str]:
	"""Classify a release using a live readiness walk.

	Walker failures do not become ``Failed`` because Helm says the release
	exists and is deployed; they mean readiness could not be proven.
	"""
	if runtime_status != "deployed":
		return classify_release_state(runtime_status=runtime_status)

	if walk_fn is None:
		walk_fn = walk

	try:
		return classify_release_state(
			runtime_status=runtime_status,
			walker_results=walk_fn(release_docname),
		)
	except Exception as e:
		return classify_release_state(
			runtime_status=runtime_status,
			walker_error=str(e),
		)


# ---------------------------------------------------------------------------
# Per-kind readiness predicates
# ---------------------------------------------------------------------------


def _check_workload(
	kind: str,
	name: str,
	namespace: str,
	apps_v1: "client.AppsV1Api",
	core_v1: "client.CoreV1Api",
	batch_v1: "client.BatchV1Api",
) -> ResourceHealth:
	if kind == "Deployment":
		obj = apps_v1.read_namespaced_deployment(name=name, namespace=namespace)
		return _check_deployment(obj, namespace)
	if kind == "StatefulSet":
		obj = apps_v1.read_namespaced_stateful_set(name=name, namespace=namespace)
		return _check_stateful_set(obj, namespace)
	if kind == "DaemonSet":
		obj = apps_v1.read_namespaced_daemon_set(name=name, namespace=namespace)
		return _check_daemon_set(obj, namespace)
	if kind == "Pod":
		obj = core_v1.read_namespaced_pod(name=name, namespace=namespace)
		return _check_pod(obj, namespace)
	if kind == "Job":
		obj = batch_v1.read_namespaced_job(name=name, namespace=namespace)
		return _check_job(obj, namespace)

	# Should never reach here — caller filters on _WORKLOAD_KINDS.
	return ResourceHealth(kind, name, namespace, False, "unsupported kind", "")


def _check_deployment(obj: Any, namespace: str) -> ResourceHealth:
	name = _meta_name(obj)
	spec_replicas = _attr(obj, "spec", "replicas")
	if spec_replicas is None:
		spec_replicas = 1
	status_obj = getattr(obj, "status", None)
	available = getattr(status_obj, "available_replicas", None) or 0
	updated = getattr(status_obj, "updated_replicas", None) or 0
	conditions = getattr(status_obj, "conditions", None) or []

	available_cond = _find_condition(conditions, "Available")
	progressing_cond = _find_condition(conditions, "Progressing")

	if spec_replicas == 0:
		return ResourceHealth("Deployment", name, namespace, True, "", "scaled to 0 replicas")

	if available >= spec_replicas and updated >= spec_replicas:
		if available_cond is None or available_cond.get("status") != "False":
			return ResourceHealth(
				"Deployment", name, namespace, True, "",
				f"{available}/{spec_replicas} available",
			)

	# Surface the most informative condition reason.
	reason = ""
	message = ""
	if available_cond and available_cond.get("status") == "False":
		reason = available_cond.get("reason") or "Unavailable"
		message = available_cond.get("message") or ""
	elif progressing_cond and progressing_cond.get("status") == "False":
		reason = progressing_cond.get("reason") or "ProgressDeadlineExceeded"
		message = progressing_cond.get("message") or ""
	else:
		reason = f"rollout {available}/{spec_replicas}"
		message = f"updated={updated}, available={available}, desired={spec_replicas}"

	return ResourceHealth("Deployment", name, namespace, False, reason, message)


def _check_stateful_set(obj: Any, namespace: str) -> ResourceHealth:
	name = _meta_name(obj)
	spec_replicas = _attr(obj, "spec", "replicas")
	if spec_replicas is None:
		spec_replicas = 1
	status_obj = getattr(obj, "status", None)
	ready = getattr(status_obj, "ready_replicas", None) or 0
	current_revision = getattr(status_obj, "current_revision", None) or ""
	update_revision = getattr(status_obj, "update_revision", None) or ""

	if spec_replicas == 0:
		return ResourceHealth("StatefulSet", name, namespace, True, "", "scaled to 0 replicas")

	if ready >= spec_replicas and (
		not update_revision or current_revision == update_revision
	):
		return ResourceHealth(
			"StatefulSet", name, namespace, True, "",
			f"{ready}/{spec_replicas} ready",
		)

	if update_revision and current_revision != update_revision:
		reason = "rollout in progress"
		message = (
			f"current_revision={current_revision}, update_revision={update_revision}, "
			f"ready={ready}/{spec_replicas}"
		)
	else:
		reason = f"rollout {ready}/{spec_replicas}"
		message = f"ready={ready}, desired={spec_replicas}"

	return ResourceHealth("StatefulSet", name, namespace, False, reason, message)


def _check_daemon_set(obj: Any, namespace: str) -> ResourceHealth:
	name = _meta_name(obj)
	status_obj = getattr(obj, "status", None)
	desired = getattr(status_obj, "desired_number_scheduled", None) or 0
	ready = getattr(status_obj, "number_ready", None) or 0
	mis_scheduled = getattr(status_obj, "number_misscheduled", None) or 0

	if desired == 0:
		return ResourceHealth(
			"DaemonSet", name, namespace, True, "",
			"no nodes match the DaemonSet's selector",
		)

	if ready >= desired and mis_scheduled == 0:
		return ResourceHealth(
			"DaemonSet", name, namespace, True, "",
			f"{ready}/{desired} ready",
		)

	if mis_scheduled > 0:
		reason = "misscheduled"
		message = f"misscheduled={mis_scheduled}, ready={ready}/{desired}"
	else:
		reason = f"rollout {ready}/{desired}"
		message = f"ready={ready}, desired={desired}"

	return ResourceHealth("DaemonSet", name, namespace, False, reason, message)


def _check_pod(obj: Any, namespace: str) -> ResourceHealth:
	name = _meta_name(obj)
	status_obj = getattr(obj, "status", None)
	phase = getattr(status_obj, "phase", None) or "Unknown"

	# Successfully completed one-shot pods (image-build, init Jobs not in a
	# Job, etc.) are "ready" in the sense that they are not blocking the
	# release — they have done their work and exited.
	if phase == "Succeeded":
		return ResourceHealth("Pod", name, namespace, True, "", "phase=Succeeded")

	if phase == "Running":
		conditions = getattr(status_obj, "conditions", None) or []
		ready_cond = _find_condition(conditions, "Ready")
		if ready_cond and ready_cond.get("status") == "True":
			return ResourceHealth("Pod", name, namespace, True, "", "phase=Running, Ready=True")
		# Running but not Ready — fall through to surface the container reason.

	# Mine the container statuses for the most actionable reason.
	reason, message = _pod_failure_reason(status_obj, phase)
	return ResourceHealth("Pod", name, namespace, False, reason, message)


def _check_job(obj: Any, namespace: str) -> ResourceHealth:
	name = _meta_name(obj)
	status_obj = getattr(obj, "status", None)
	conditions = getattr(status_obj, "conditions", None) or []
	complete_cond = _find_condition(conditions, "Complete")
	failed_cond = _find_condition(conditions, "Failed")

	if complete_cond and complete_cond.get("status") == "True":
		return ResourceHealth("Job", name, namespace, True, "", "Complete=True")

	if failed_cond and failed_cond.get("status") == "True":
		reason = failed_cond.get("reason") or "Failed"
		message = failed_cond.get("message") or ""
		return ResourceHealth("Job", name, namespace, False, reason, message)

	active = getattr(status_obj, "active", None) or 0
	succeeded = getattr(status_obj, "succeeded", None) or 0
	failed = getattr(status_obj, "failed", None) or 0
	return ResourceHealth(
		"Job", name, namespace, False,
		"in progress",
		f"active={active}, succeeded={succeeded}, failed={failed}",
	)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _meta_name(obj: Any) -> str:
	metadata = getattr(obj, "metadata", None)
	return getattr(metadata, "name", None) or "<unknown>"


def _attr(obj: Any, *path: str) -> Any:
	cursor: Any = obj
	for segment in path:
		if cursor is None:
			return None
		cursor = getattr(cursor, segment, None)
	return cursor


def _find_condition(conditions: list[Any], cond_type: str) -> dict[str, Any] | None:
	"""Return the named condition dict (snake_case fields normalized)."""
	for cond in conditions:
		# Conditions can be returned as kubernetes-client objects (snake_case
		# attribute access) or as plain dicts (when the source is the raw
		# manifest).  Normalize both shapes onto a small read-only dict.
		if isinstance(cond, dict):
			if str(cond.get("type")) == cond_type:
				return cond
			continue
		if getattr(cond, "type", None) == cond_type:
			return {
				"type": cond_type,
				"status": getattr(cond, "status", None),
				"reason": getattr(cond, "reason", None),
				"message": getattr(cond, "message", None),
			}
	return None


def _pod_failure_reason(status_obj: Any, phase: str) -> tuple[str, str]:
	container_statuses = getattr(status_obj, "container_statuses", None) or []
	init_statuses = getattr(status_obj, "init_container_statuses", None) or []

	for cs in list(container_statuses) + list(init_statuses):
		state = getattr(cs, "state", None)
		waiting = getattr(state, "waiting", None) if state else None
		terminated = getattr(state, "terminated", None) if state else None
		if waiting and getattr(waiting, "reason", None):
			return getattr(waiting, "reason", "Waiting"), (
				getattr(waiting, "message", None) or ""
			)
		if terminated and getattr(terminated, "reason", None):
			exit_code = getattr(terminated, "exit_code", "?")
			return (
				getattr(terminated, "reason", "Terminated"),
				f"exit_code={exit_code}: {getattr(terminated, 'message', None) or ''}".strip(": "),
			)

	# No actionable container status — fall back to the pod phase.
	return phase, f"phase={phase}"


def _resource_health_from_api_error(
	kind: str,
	name: str,
	namespace: str,
	error: ApiException,
) -> ResourceHealth:
	if error.status == 404:
		return ResourceHealth(
			kind, name, namespace, False, "missing",
			"Resource is in the rendered manifest but missing from the cluster.",
		)
	return ResourceHealth(
		kind, name, namespace, False,
		f"api-error-{error.status}",
		(error.reason or "")[:200],
	)
