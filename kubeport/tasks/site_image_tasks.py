# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""Background tasks for syncing shipped Kubeport Site Image catalog rows."""

import frappe

from kubeport.site_images.catalog import sync_catalog


def enqueue_sync_site_image_catalog() -> None:
	"""Queue a catalog sync after app migration/install."""
	frappe.enqueue(
		"kubeport.tasks.site_image_tasks.sync_site_image_catalog",
		queue="long",
		enqueue_after_commit=True,
	)


def sync_site_image_catalog() -> None:
	"""Upsert the shipped site image catalog into MariaDB."""
	try:
		docnames = sync_catalog()
		frappe.logger("kubeport").info(
			"Synced %s Kubeport Site Image catalog rows.",
			len(docnames),
		)
	except Exception as e:
		frappe.log_error(
			title="Kubeport Site Image Catalog Sync Failed",
			message=str(e),
		)
		raise
