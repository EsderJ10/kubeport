import frappe
from frappe.model.document import Document
import subprocess
import tempfile
import os
import json

class HelmRelease(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        chart_reference: DF.Data
        cluster: DF.Link
        namespace: DF.Data
        release_name: DF.Data
        status: DF.Literal["Draft", "Deployed", "Failed"]
        values: DF.Code
    # end: auto-generated types

    def get_cluster_kubeconfig(self):
        """Recupera el Kubeconfig del clúster vinculado."""
        if not self.cluster:
            frappe.throw("El clúster destino es obligatorio.")
            
        cluster_doc = frappe.get_doc("Kubernetes Cluster", self.cluster)
        if not cluster_doc.kubeconfig:
            frappe.throw("El clúster carece de Kubeconfig.")
            
        return cluster_doc.kubeconfig

    @frappe.whitelist()
    def deploy_release(self):
        if not self.values:
            frappe.throw("El campo Values (JSON) está vacío.")

        # Validación estricta del JSON
        try:
            json.loads(self.values)
        except json.JSONDecodeError as e:
            frappe.throw(f"Error de sintaxis JSON: {str(e)}")

        kubeconfig_data = self.get_cluster_kubeconfig()
        fd_kube, kube_path = tempfile.mkstemp(suffix=".yaml")
        fd_values, values_path = tempfile.mkstemp(suffix=".json")
        
        try:
            with os.fdopen(fd_kube, 'w') as f:
                f.write(kubeconfig_data)
                
            with os.fdopen(fd_values, 'w') as f:
                f.write(self.values)

            # Construcción dinámica del comando Helm
            cmd = [
                "helm", "upgrade", "--install",
                self.release_name,
                self.chart_reference,
                "--namespace", self.namespace,
                "--create-namespace",
                "-f", values_path,
                "--kubeconfig", kube_path,
                "--kube-insecure-skip-tls-verify"
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                self.db_set('status', 'Deployed')
                frappe.msgprint(f"Release '{self.release_name}' desplegada correctamente.", indicator="green", alert=True)
            else:
                self.db_set('status', 'Failed')
                frappe.throw(f"Fallo en Helm: {result.stderr}")

        except Exception as e:
            self.db_set('status', 'Failed')
            frappe.throw(f"Error interno: {str(e)}")
            
        finally:
            if os.path.exists(kube_path): os.remove(kube_path)
            if os.path.exists(values_path): os.remove(values_path)

    @frappe.whitelist()
    def uninstall_release(self):
        if self.status != 'Deployed':
            frappe.throw("Solo se pueden desinstalar Releases 'Deployed'.")

        kubeconfig_data = self.get_cluster_kubeconfig()
        fd_kube, kube_path = tempfile.mkstemp(suffix=".yaml")
        
        try:
            with os.fdopen(fd_kube, 'w') as f:
                f.write(kubeconfig_data)

            cmd = [
                "helm", "uninstall",
                self.release_name,
                "--namespace", self.namespace,
                "--kubeconfig", kube_path,
                "--kube-insecure-skip-tls-verify"
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode == 0:
                self.db_set('status', 'Draft')
                frappe.msgprint(f"Release desinstalada del clúster.", indicator="orange", alert=True)
            else:
                frappe.throw(f"Fallo al desinstalar: {result.stderr}")

        finally:
            if os.path.exists(kube_path): os.remove(kube_path)