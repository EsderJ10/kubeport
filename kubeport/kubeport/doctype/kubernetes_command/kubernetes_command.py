import frappe
from frappe.model.document import Document
from kubernetes import client
import json

from kubeport.utils.k8s_client import get_k8s_api_client


# Mapping of resource types to their corresponding API methods for listing
RESOURCE_API_MAP = {
	"Pod": {"api_class": "CoreV1Api", "list_method": "list_namespaced_pod"},
	"Service": {"api_class": "CoreV1Api", "list_method": "list_namespaced_service"},
	"Deployment": {"api_class": "AppsV1Api", "list_method": "list_namespaced_deployment"},
	"ConfigMap": {"api_class": "CoreV1Api", "list_method": "list_namespaced_config_map"},
	"Secret": {"api_class": "CoreV1Api", "list_method": "list_namespaced_secret"},
	"Namespace": {"api_class": "CoreV1Api", "list_method": "list_namespace"},
	"Node": {"api_class": "CoreV1Api", "list_method": "list_node"},
	"Ingress": {"api_class": "NetworkingV1Api", "list_method": "list_namespaced_ingress"},
	"PersistentVolumeClaim": {"api_class": "CoreV1Api", "list_method": "list_namespaced_persistent_volume_claim"},
	"StatefulSet": {"api_class": "AppsV1Api", "list_method": "list_namespaced_stateful_set"},
	"DaemonSet": {"api_class": "AppsV1Api", "list_method": "list_namespaced_daemon_set"},
	"Job": {"api_class": "BatchV1Api", "list_method": "list_namespaced_job"},
	"CronJob": {"api_class": "BatchV1Api", "list_method": "list_namespaced_cron_job"},
}

# Resources that are cluster-scoped (no namespace parameter)
CLUSTER_SCOPED_RESOURCES = {"Namespace", "Node"}


class KubernetesCommand(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cluster: DF.Link
		command_name: DF.Data
		namespace: DF.Data | None
		output: DF.Code | None
		resource_type: DF.Literal[
			"Pod", "Service", "Deployment", "ConfigMap", "Secret",
			"Namespace", "Node", "Ingress", "PersistentVolumeClaim",
			"StatefulSet", "DaemonSet", "Job", "CronJob"
		]
	# end: auto-generated types

	@frappe.whitelist()
	def execute_command(self):
		if not self.cluster:
			frappe.throw("A target cluster is required.")

		if not self.resource_type:
			frappe.throw("Please select a resource type.")

		resource_config = RESOURCE_API_MAP.get(self.resource_type)
		if not resource_config:
			frappe.throw(f"Unsupported resource type: {self.resource_type}")

		try:
			api_client = get_k8s_api_client(self.cluster)

			# Instantiate the appropriate API class
			api_class = getattr(client, resource_config["api_class"])
			api_instance = api_class(api_client=api_client)

			# Call the list method
			list_method = getattr(api_instance, resource_config["list_method"])

			if self.resource_type in CLUSTER_SCOPED_RESOURCES:
				result = list_method()
			else:
				namespace = self.namespace or "default"
				result = list_method(namespace=namespace)

			# Format output as a readable table
			output_lines = self._format_resource_list(result, self.resource_type)
			self.db_set("output", output_lines)

			frappe.msgprint("Query executed successfully.", indicator="green", alert=True)

		except Exception as e:
			error_msg = f"Error querying {self.resource_type}: {str(e)}"
			self.db_set("output", error_msg)
			frappe.msgprint(
				"The query returned an error. Check the Output field.",
				indicator="red",
				alert=True,
			)

	def _format_resource_list(self, result, resource_type: str) -> str:
		"""Format a K8s resource list into a readable text table."""
		if not result.items:
			return f"No {resource_type} resources found."

		lines = []
		header = f"{'NAME':<50} {'NAMESPACE':<20} {'STATUS/PHASE':<15} {'AGE'}"
		lines.append(header)
		lines.append("-" * len(header))

		for item in result.items:
			name = item.metadata.name or ""
			namespace = item.metadata.namespace or "N/A"
			age = ""
			if item.metadata.creation_timestamp:
				from datetime import datetime, timezone

				delta = datetime.now(timezone.utc) - item.metadata.creation_timestamp
				if delta.days > 0:
					age = f"{delta.days}d"
				else:
					hours = delta.seconds // 3600
					age = f"{hours}h"

			# Extract status/phase depending on resource type
			status = ""
			if hasattr(item, "status"):
				if hasattr(item.status, "phase"):
					status = item.status.phase or ""
				elif hasattr(item.status, "conditions") and item.status.conditions:
					status = item.status.conditions[-1].type or ""
				elif hasattr(item.status, "available_replicas"):
					ready = item.status.ready_replicas or 0
					desired = item.status.replicas or 0
					status = f"{ready}/{desired}"

			lines.append(f"{name:<50} {namespace:<20} {status:<15} {age}")

		lines.append(f"\nTotal: {len(result.items)} resource(s)")
		return "\n".join(lines)