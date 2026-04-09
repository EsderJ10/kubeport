# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""
Helm Chart Controller

Represents a single Helm chart discovered from a repository.  Contains a
child table of available versions populated during repository sync.

Charts are mostly read-only (auto-populated by the sync task).  Users
interact with them via the Helm Release form to deploy specific versions.
"""

import frappe
from frappe.model.document import Document


class HelmChart(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF
		from kubeport.kubeport.doctype.helm_chart_version.helm_chart_version import HelmChartVersion

		chart_name: DF.Data
		default_values: DF.Code | None
		description: DF.SmallText | None
		latest_app_version: DF.Data | None
		latest_version: DF.Data | None
		repository: DF.Link
		versions: DF.Table[HelmChartVersion]
	# end: auto-generated types

	def get_chart_reference(self) -> str:
		"""Return the Helm chart reference string (e.g. bitnami/nginx)."""
		return f"{self.repository}/{self.chart_name}"

	@frappe.whitelist()
	def fetch_default_values(self) -> str:
		"""Fetch the default values.yaml from the Helm CLI for the latest version.

		Returns the YAML string and also persists it on the document.
		"""
		from kubeport.utils.helm import show_values

		chart_ref = self.get_chart_reference()
		values = show_values(chart_ref, version=self.latest_version)

		self.db_set("default_values", values)
		return values
