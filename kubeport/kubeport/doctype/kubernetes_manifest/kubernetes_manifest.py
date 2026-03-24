import frappe
from frappe.model.document import Document
from kubernetes import client, config, utils
import tempfile
import os
import urllib3
import json # Importante para leer el JSON de la interfaz

class KubernetesManifest(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        cluster: DF.Link
        content: DF.Code
        manifest_name: DF.Data
        namespace: DF.Data | None
        status: DF.Literal["Draft", "Applied", "Failed"]
    # end: auto-generated types

    def setup_kubernetes_client(self):
        """Método auxiliar para aislar la lógica de autenticación y seguridad SSL."""
        if not self.cluster:
            frappe.throw("You must select a target cluster.")
            
        cluster_doc = frappe.get_doc("Kubernetes Cluster", self.cluster)
        if not cluster_doc.kubeconfig:
            frappe.throw("The selected cluster lacks configuration (Kubeconfig).")

        # 1. El Kubeconfig SIEMPRE es yaml por estándar de Kubernetes
        fd_kube, kube_path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd_kube, 'w') as f:
            f.write(cluster_doc.kubeconfig)
            
        # 2. Cargar la configuración
        config.load_kube_config(config_file=kube_path)
        
        # 3. Bypass de verificación SSL
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        conf = client.Configuration.get_default_copy()
        conf.verify_ssl = False
        client.Configuration.set_default(conf)
        
        k8s_client = client.ApiClient()
        return k8s_client, kube_path

    @frappe.whitelist()
    def apply_manifest(self):
        # Asegurarnos de usar el campo JSON que configuraste en el DocType
        if not self.content:
            frappe.throw("The JSON content is empty.")

        # Validar sintaxis JSON nativamente en Python
        try:
            manifest_data = json.loads(self.content)
        except json.JSONDecodeError as e:
            frappe.throw(f"JSON syntax error: {str(e)}")

        k8s_client, kube_path = None, None

        try:
            k8s_client, kube_path = self.setup_kubernetes_client()

            # Permitir que el JSON sea un solo objeto o una lista de objetos
            if isinstance(manifest_data, dict):
                manifest_data = [manifest_data]

            # Inyectar directamente el diccionario JSON a la API sin crear archivos temporales
            for k8s_object in manifest_data:
                utils.create_from_dict(k8s_client, data=k8s_object, namespace=self.namespace or "default")

            self.db_set('status', 'Applied')
            frappe.msgprint(f"Manifest '{self.manifest_name}' applied successfully.", indicator="green", alert=True)

        except Exception as e:
            self.db_set('status', 'Failed')
            frappe.throw(f"Error in the manifest application: {str(e)}")

        finally:
            if kube_path and os.path.exists(kube_path): 
                os.remove(kube_path)

    @frappe.whitelist()
    def delete_manifest(self):
        import subprocess
        
        if self.status != 'Applied':
            frappe.throw("Only applied manifests can be deleted.")
            
        k8s_client, kube_path = None, None
        # Para eliminar, creamos un archivo temporal JSON para pasárselo a kubectl
        fd_json, json_path = tempfile.mkstemp(suffix=".json")
        
        try:
            with os.fdopen(fd_json, 'w') as f:
                f.write(self.content)
                
            k8s_client, kube_path = self.setup_kubernetes_client()
            
            full_cmd = ["kubectl", "delete", "-f", json_path, "--kubeconfig", kube_path, "--insecure-skip-tls-verify=true"]
            result = subprocess.run(full_cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                self.db_set('status', 'Draft')
                frappe.msgprint(f"Resources deleted successfully.", indicator="orange", alert=True)
            else:
                frappe.throw(f"Error while deleting: {result.stderr}")
                
        except Exception as e:
            frappe.throw(str(e))
        finally:
            # Limpiar ambos archivos temporales (el manifiesto JSON y el kubeconfig YAML)
            if os.path.exists(json_path): os.remove(json_path)
            if kube_path and os.path.exists(kube_path): os.remove(kube_path)