import frappe
from frappe.model.document import Document
import subprocess
import tempfile
import os


class KubernetesCommand(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF

        cluster: DF.Link
        command: DF.Data
        command_name: DF.Data
        output: DF.Code | None
    # end: auto-generated types

    @frappe.whitelist()
    def execute_command(self):
        if not self.cluster or not self.command:
            frappe.throw("The cluster and command are required.")

        # Get the cluster document
        cluster_doc = frappe.get_doc("Kubernetes Cluster", self.cluster)
        if not cluster_doc.kubeconfig:
            frappe.throw("The selected cluster does not have a saved Kubeconfig.")

        cmd_string = self.command.strip()
        if cmd_string.startswith("kubectl "):
            cmd_string = cmd_string[8:]

        cmd_args = cmd_string.split()

        # Temporal safe file for Kubeconfig
        fd, temp_path = tempfile.mkstemp(suffix=".yaml")

        try:
            with os.fdopen(fd, "w") as f:
                f.write(cluster_doc.kubeconfig)

            full_cmd = [
                "kubectl",
                "--kubeconfig",
                temp_path,
                "--insecure-skip-tls-verify=true",
            ] + cmd_args

            result = subprocess.run(full_cmd, capture_output=True, text=True)

            # Save output
            output_text = result.stdout if result.returncode == 0 else result.stderr
            self.db_set("output", output_text)

            if result.returncode == 0:
                frappe.msgprint("Command executed successfully", indicator="green", alert=True)
            else:
                frappe.msgprint(
                    "The command returned an error. Please check the Output field.",
                    indicator="red",
                    alert=True,
                )

        except Exception as e:
            frappe.throw(f"Internal error while executing the command: {str(e)}")

        finally:
            # Clean temporal safe file
            if os.path.exists(temp_path):
                os.remove(temp_path)