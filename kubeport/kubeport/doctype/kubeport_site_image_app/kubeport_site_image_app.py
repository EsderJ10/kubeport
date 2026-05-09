# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class KubeportSiteImageApp(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		app_name: DF.Data
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		ref: DF.Data | None
		source_url: DF.Data | None
	# end: auto-generated types

	pass
