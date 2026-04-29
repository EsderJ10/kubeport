import frappe
from frappe.model.utils.rename_field import rename_field


def execute():
	frappe.reload_doctype("Frappe Site")
	rename_field("Frappe Site", "creation_job_name", "operation_job_name")
	rename_field("Frappe Site", "creation_job_token", "operation_job_token")
