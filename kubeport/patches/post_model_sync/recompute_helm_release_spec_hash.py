import frappe

from kubeport.kubeport.doctype.helm_release.helm_release import (
	_get_site_image_digest_for_hash,
	calculate_release_spec_hash,
)


def execute():
	"""Recompute spec hashes after the ingress fields were added.

	The hash payload now includes ``ingress_*`` keys.  Existing rows have those
	fields defaulted to 0/empty, but their ``last_applied_spec_hash`` was
	produced under the old payload shape — so without this patch every saved
	release would show ``pending_changes=1`` until it was re-saved.
	"""
	releases = frappe.get_all(
		"Helm Release",
		filters={"last_applied_spec_hash": ["!=", ""]},
		fields=[
			"name",
			"chart",
			"chart_version",
			"namespace",
			"release_name",
			"values",
			"site_image",
			"ingress_enabled",
			"ingress_hostname",
			"ingress_class_name",
			"ingress_cluster_issuer",
		],
	)

	for release in releases:
		new_hash = calculate_release_spec_hash(
			chart=release.get("chart"),
			chart_version=release.get("chart_version"),
			namespace=release.get("namespace"),
			release_name=release.get("release_name"),
			values_yaml=release.get("values"),
			site_image=release.get("site_image"),
			site_image_digest=_get_site_image_digest_for_hash(release.get("site_image")),
			ingress_enabled=release.get("ingress_enabled"),
			ingress_hostname=release.get("ingress_hostname"),
			ingress_class_name=release.get("ingress_class_name"),
			ingress_cluster_issuer=release.get("ingress_cluster_issuer"),
		)
		frappe.db.set_value(
			"Helm Release",
			release["name"],
			{
				"desired_spec_hash": new_hash,
				"last_applied_spec_hash": new_hash,
				"pending_changes": 0,
			},
			update_modified=False,
		)
