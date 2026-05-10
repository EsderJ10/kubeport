# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from unittest.mock import patch

from frappe.tests import IntegrationTestCase, UnitTestCase

from kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image import (
	KubeportSiteImage,
	build_site_image_docname,
)


def _make_doc(**overrides) -> KubeportSiteImage:
	doc = object.__new__(KubeportSiteImage)
	doc.image_repository = "ghcr.io/example/site"
	doc.image_tag = "v1.0.0"
	doc.image_digest = ""
	doc.status = "Active"
	doc.is_curated = 0
	doc.is_default = 0
	doc.name = build_site_image_docname(doc.image_repository, doc.image_tag)
	for key, value in overrides.items():
		setattr(doc, key, value)
	return doc


class UnitTestKubeportSiteImage(UnitTestCase):
	@patch("kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image.frappe.db.get_value")
	@patch("kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image.frappe.throw")
	def test_validate_rejects_is_default_on_user_row(self, mock_throw, mock_get_value):
		mock_get_value.return_value = None
		mock_throw.side_effect = RuntimeError("Defaults are reserved for curated Kubeport images.")
		doc = _make_doc(is_default=1, is_curated=0)

		with self.assertRaisesRegex(RuntimeError, "curated Kubeport images"):
			doc.validate()

	@patch("kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image.frappe.db.get_value")
	def test_validate_allows_is_default_on_curated_row(self, mock_get_value):
		mock_get_value.return_value = None
		doc = _make_doc(
			image_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			is_default=1,
			is_curated=1,
		)

		doc.validate()

		self.assertEqual(doc.is_curated, 1)
		self.assertEqual(doc.is_default, 1)

	@patch("kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image.frappe.db.get_value")
	@patch("kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image.frappe.throw")
	def test_validate_rejects_active_curated_row_without_digest(self, mock_throw, mock_get_value):
		mock_get_value.return_value = None
		mock_throw.side_effect = RuntimeError("must record the pushed GHCR image digest")
		doc = _make_doc(is_curated=1, image_digest="")

		with self.assertRaisesRegex(RuntimeError, "digest"):
			doc.validate()

	@patch("kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image.frappe.db.get_value")
	@patch("kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image.frappe.throw")
	def test_validate_rejects_non_ghcr_repository(self, mock_throw, mock_get_value):
		mock_get_value.return_value = None
		mock_throw.side_effect = RuntimeError("must use a public GHCR repository")
		doc = _make_doc(image_repository="docker.io/example/site")

		with self.assertRaisesRegex(RuntimeError, "GHCR"):
			doc.validate()

	@patch("kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image.frappe.db.get_value")
	def test_validate_accepts_unspecified_is_curated_as_user_row(self, mock_get_value):
		"""A bare new() doc (is_curated=0, no enum to fail) saves cleanly."""
		mock_get_value.return_value = None
		doc = _make_doc()  # is_curated=0, is_default=0 from helper defaults

		doc.validate()

		self.assertEqual(doc.is_curated, 0)
		self.assertEqual(doc.status, "Active")

	@patch("kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image.frappe.throw")
	def test_on_trash_blocks_curated_row_deletion(self, mock_throw):
		mock_throw.side_effect = RuntimeError("mark it Deprecated instead")
		doc = _make_doc(is_curated=1)

		with self.assertRaisesRegex(RuntimeError, "mark it Deprecated instead"):
			doc.on_trash()

	@patch("kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image.frappe.get_all")
	@patch("kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image.frappe.throw")
	def test_on_trash_blocks_user_row_deletion_when_helm_release_links(self, mock_throw, mock_get_all):
		mock_get_all.return_value = [{"name": "cluster-a/default/bench-a"}]
		mock_throw.side_effect = RuntimeError("linked to Helm Release")
		doc = _make_doc(is_curated=0)

		with self.assertRaisesRegex(RuntimeError, "linked to Helm Release"):
			doc.on_trash()

	@patch("kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image.frappe.get_all")
	def test_on_trash_allows_user_row_deletion_when_unused(self, mock_get_all):
		mock_get_all.return_value = []
		doc = _make_doc(is_curated=0)

		doc.on_trash()

		mock_get_all.assert_called_once()


# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestKubeportSiteImage(IntegrationTestCase):
	"""Integration tests for KubeportSiteImage."""

	pass
