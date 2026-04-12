import frappe
from frappe.model.rename_doc import rename_doc

from kubeport.kubeport.doctype.helm_release.helm_release import build_release_docname


def execute():
	releases = frappe.get_all(
		"Helm Release",
		fields=["name", "cluster", "namespace", "release_name"],
	)

	for release in releases:
		target_name = build_release_docname(
			release.get("cluster"),
			release.get("namespace"),
			release.get("release_name"),
		)
		current_name = release.get("name")
		if not current_name or not target_name or current_name == target_name:
			continue
		if frappe.db.exists("Helm Release", target_name):
			frappe.log_error(
				title="Helm Release Rename Skipped",
				message=(
					f"Could not rename Helm Release '{current_name}' to '{target_name}' "
					"because the target name already exists."
				),
			)
			continue

		rename_doc(
			"Helm Release",
			current_name,
			target_name,
			force=True,
			merge=False,
			ignore_permissions=True,
		)
