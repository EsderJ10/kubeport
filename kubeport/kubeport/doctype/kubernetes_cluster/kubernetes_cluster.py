import frappe
from frappe.model.document import Document
from kubernetes import client, config
import yaml
import urllib3

class KubernetesCluster(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        cluster_name: DF.Data
        kubeconfig: DF.Code
        status: DF.Literal["Pending", "Connected", "Error"]
    # end: auto-generated types

    
    @frappe.whitelist()
    def test_connection(self):
        if not self.kubeconfig:
            frappe.throw("The Kubeconfig field is empty.")

        try:
            kubeconfig_dict = yaml.safe_load(self.kubeconfig)
            config.load_kube_config_from_dict(kubeconfig_dict)
            
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            conf = client.Configuration.get_default_copy()
            conf.verify_ssl = False
            client.Configuration.set_default(conf)
            
            v1 = client.CoreV1Api()
            nodes = v1.list_node()
            
            self.db_set('status', 'Connected')
            frappe.msgprint(f"Successfully connected. The cluster responded and has {len(nodes.items)} node(s).", alert=True, indicator='green')
            
        except Exception as e:
            self.db_set('status', 'Error')
            frappe.throw(f"Failed to connect: {str(e)}")