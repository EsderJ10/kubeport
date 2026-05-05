"""
Frappe Site API Endpoints

Read-only queries that support the Frappe Site form UI.  All cluster
operations are performed in background tasks — these endpoints only
read and return data.
"""

from __future__ import annotations

import frappe


@frappe.whitelist()
def get_site_job_logs(site_docname: str) -> dict:
	"""Return the stdout logs from the current operation's Job pod.

	Fetches the most recent log lines from the pod created by the most
	recent Kubernetes Job submitted for this site (``bench new-site``,
	``bench drop-site``, or ``bench migrate``).  Returns an empty string
	for ``logs`` when the Job pod is not yet available or has already
	been cleaned up by ttlSecondsAfterFinished.

	Args:
		site_docname: The Frappe Site document name.

	Returns:
		A dict with keys ``job_name`` (str), ``logs`` (str), and
		``error`` (str | None).
	"""
	doc = frappe.get_doc("Frappe Site", site_docname)

	if not doc.operation_job_name:
		return {"job_name": "", "logs": "", "error": "No operation job has been submitted yet."}

	namespace = doc.namespace or "default"
	cluster = doc.cluster

	if not cluster:
		return {
			"job_name": doc.operation_job_name,
			"logs": "",
			"error": "Site document is missing cluster information.",
		}

	try:
		from kubernetes import client
		from kubernetes.client.rest import ApiException

		from kubeport.utils.k8s_client import get_k8s_api_client

		api_client = get_k8s_api_client(cluster)
		core_v1 = client.CoreV1Api(api_client=api_client)

		pods = core_v1.list_namespaced_pod(
			namespace=namespace,
			label_selector=f"job-name={doc.operation_job_name}",
			_request_timeout=15,
		)

		if not pods.items:
			return {
				"job_name": doc.operation_job_name,
				"logs": "",
				"error": "Job pod not found — it may still be pending or has been cleaned up.",
			}

		pod = pods.items[0]
		pod_name = pod.metadata.name if pod.metadata else None
		if not pod_name:
			return {
				"job_name": doc.operation_job_name,
				"logs": "",
				"error": "Job pod name unavailable.",
			}

		try:
			logs = core_v1.read_namespaced_pod_log(
				name=pod_name,
				namespace=namespace,
				tail_lines=200,
				_request_timeout=15,
			)
		except ApiException as e:
			if e.status == 400:
				# Pod exists but container hasn't started yet
				logs = ""
			else:
				raise

		return {"job_name": doc.operation_job_name, "logs": logs or "", "error": None}

	except Exception as e:
		return {
			"job_name": doc.operation_job_name,
			"logs": "",
			"error": str(e),
		}


@frappe.whitelist()
def list_site_backups(site_docname: str) -> list[dict]:
	"""Return backup rows for a Frappe Site, newest first."""
	return frappe.get_all(
		"Frappe Site Backup",
		filters={"frappe_site": site_docname},
		fields=[
			"name",
			"backup_name",
			"status",
			"size_bytes",
			"storage_path",
			"started_at",
			"completed_at",
			"status_detail",
		],
		order_by="creation desc",
		limit_page_length=50,
	)


@frappe.whitelist()
def get_site_backup_job_logs(backup_docname: str) -> dict:
	"""Return stdout logs from the backup or restore Job for a backup row."""
	doc = frappe.get_doc("Frappe Site Backup", backup_docname)
	if not doc.operation_job_name:
		return {"job_name": "", "logs": "", "error": "No backup operation job has been submitted yet."}
	if not doc.cluster:
		return {
			"job_name": doc.operation_job_name,
			"logs": "",
			"error": "Backup document is missing cluster information.",
		}

	try:
		from kubernetes import client
		from kubernetes.client.rest import ApiException

		from kubeport.utils.k8s_client import get_k8s_api_client

		api_client = get_k8s_api_client(doc.cluster)
		core_v1 = client.CoreV1Api(api_client=api_client)
		pods = core_v1.list_namespaced_pod(
			namespace=doc.namespace or "default",
			label_selector=f"job-name={doc.operation_job_name}",
			_request_timeout=15,
		)
		if not pods.items:
			return {
				"job_name": doc.operation_job_name,
				"logs": "",
				"error": "Job pod not found — it may still be pending or has been cleaned up.",
			}
		pod = pods.items[0]
		pod_name = pod.metadata.name if pod.metadata else None
		if not pod_name:
			return {"job_name": doc.operation_job_name, "logs": "", "error": "Job pod name unavailable."}
		try:
			logs = core_v1.read_namespaced_pod_log(
				name=pod_name,
				namespace=doc.namespace or "default",
				tail_lines=200,
				_request_timeout=15,
			)
		except ApiException as e:
			if e.status == 400:
				logs = ""
			else:
				raise
		return {"job_name": doc.operation_job_name, "logs": logs or "", "error": None}
	except Exception as e:
		return {"job_name": doc.operation_job_name, "logs": "", "error": str(e)}
