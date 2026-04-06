import frappe
from frappe.model.document import Document
from kubernetes import client

from kubeport.utils.k8s_client import get_k8s_api_client


class KubernetesCluster(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cluster_name: DF.Data
		kubeconfig: DF.Code
		skip_tls_verify: DF.Check
		status: DF.Literal["Pending", "Connected", "Error"]
	# end: auto-generated types

	@frappe.whitelist()
	def test_connection(self):
		if not self.kubeconfig:
			frappe.throw("The Kubeconfig field is empty.")

		try:
			api_client = get_k8s_api_client(self.name)
			v1 = client.CoreV1Api(api_client=api_client)
			nodes = v1.list_node()

			self.db_set("status", "Connected")
			frappe.msgprint(
				f"Successfully connected. The cluster responded and has {len(nodes.items)} node(s).",
				alert=True,
				indicator="green",
			)

		except Exception as e:
			self.db_set("status", "Error")
			frappe.throw(f"Failed to connect: {str(e)}")