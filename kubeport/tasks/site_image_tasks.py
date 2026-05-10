# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""Background tasks for syncing shipped Kubeport Site Image catalog rows."""

import frappe

from kubeport.site_images.catalog import sync_catalog
from kubeport.utils import metrics


def enqueue_sync_site_image_catalog() -> None:
	"""Queue a catalog sync after app migration/install."""
	correlation_id = metrics.new_correlation_id()
	with metrics.correlation_scope(correlation_id):
		metrics.logger("kubeport.siteimage").info("enqueue sync_site_image_catalog")
	frappe.enqueue(
		"kubeport.tasks.site_image_tasks.sync_site_image_catalog",
		correlation_id=correlation_id,
		queue="long",
		enqueue_after_commit=True,
	)


def sync_site_image_catalog(correlation_id: str | None = None) -> None:
	"""Upsert the shipped site image catalog into MariaDB."""
	with metrics.correlation_scope(correlation_id):
		_sync_site_image_catalog_impl()


def _sync_site_image_catalog_impl() -> None:
	metrics.logger("kubeport.siteimage").info("worker enter sync_site_image_catalog")
	try:
		docnames = sync_catalog()
		metrics.logger("kubeport.siteimage").info(
			"Synced %s Kubeport Site Image catalog rows.", len(docnames)
		)
	except Exception as e:
		frappe.log_error(
			title="Kubeport Site Image Catalog Sync Failed",
			message=str(e),
		)
		raise
