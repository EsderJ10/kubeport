# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from frappe.tests import UnitTestCase

from kubeport.kubeport.doctype.kubeport_site_image.kubeport_site_image import (
	_GHCR_REPOSITORY_RE as DOCTYPE_GHCR_RE,
	_IMAGE_TAG_RE as DOCTYPE_TAG_RE,
	_SHA256_DIGEST_RE as DOCTYPE_DIGEST_RE,
)


_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "update_site_catalog.py"
_VALID_DIGEST = "sha256:" + ("a" * 64)
_OTHER_DIGEST = "sha256:" + ("b" * 64)


def _load_script() -> ModuleType:
	spec = importlib.util.spec_from_file_location("kubeport_update_site_catalog", _SCRIPT_PATH)
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


class UnitTestRegexDriftFromDoctype(UnitTestCase):
	def test_script_regex_strings_match_doctype(self):
		script = _load_script()
		self.assertEqual(script._GHCR_REPOSITORY_RE.pattern, DOCTYPE_GHCR_RE.pattern)
		self.assertEqual(script._IMAGE_TAG_RE.pattern, DOCTYPE_TAG_RE.pattern)
		self.assertEqual(script._SHA256_DIGEST_RE.pattern, DOCTYPE_DIGEST_RE.pattern)


class UnitTestSiteCatalogRewrite(UnitTestCase):
	def setUp(self):
		self.script = _load_script()
		self.apps_json = _write_tempfile(b'[{"url":"https://x","branch":"main"}]\n')
		self.catalog = _write_tempfile(
			json.dumps({
				"images": [{
					"image_title": "Kubeport ERPNext v16",
					"image_repository": "ghcr.io/owner/kubeport-site",
					"image_tag": "0.0.1-frappe16",
					"image_digest": _OTHER_DIGEST,
					"frappe_major": 16,
					"erpnext_version": "16",
					"apps_json_hash": "sha256:" + ("c" * 64),
					"source_revision": "abc123",
					"status": "Active",
					"is_default": True,
					"description": "Default Kubeport runtime image.",
					"apps": [
						{"app_name": "erpnext", "source_url": "https://x", "ref": "version-16"},
					],
				}],
			}).encode("utf-8"),
		)

	def tearDown(self):
		self.catalog.unlink(missing_ok=True)
		self.apps_json.unlink(missing_ok=True)

	def _args(self, **overrides):
		base = {
			"catalog": self.catalog,
			"apps_json": self.apps_json,
			"image_name": "ghcr.io/Owner/kubeport-site",
			"frappe_branch": "version-16",
			"image_digest": _VALID_DIGEST,
			"git_sha": "deadbeef" * 5,
			"tag_ref": "v0.0.99",
		}
		base.update(overrides)
		return _Args(**base)

	def test_rewrite_updates_only_target_fields(self):
		original = json.loads(self.catalog.read_text(encoding="utf-8"))

		result = self.script.run(self._args())

		updated = json.loads(self.catalog.read_text(encoding="utf-8"))
		row = updated["images"][0]
		self.assertEqual(row["image_tag"], "0.0.99-frappe16")
		self.assertEqual(row["image_digest"], _VALID_DIGEST)
		self.assertEqual(row["source_revision"], "deadbeef" * 5)
		self.assertEqual(
			row["apps_json_hash"],
			self.script.compute_apps_json_hash(self.apps_json),
		)
		# Everything else preserved verbatim.
		original_row = original["images"][0]
		for field in (
			"image_title",
			"image_repository",
			"frappe_major",
			"erpnext_version",
			"status",
			"is_default",
			"description",
			"apps",
		):
			self.assertEqual(row[field], original_row[field], f"{field} should be preserved")

		self.assertEqual(result["matched_repository"], "ghcr.io/owner/kubeport-site")
		self.assertEqual(result["short_digest"], "a" * 12)

	def test_rewrite_output_passes_load_catalog(self):
		from kubeport.site_images.catalog import load_catalog

		self.script.run(self._args())
		rows = load_catalog(self.catalog)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["image_tag"], "0.0.99-frappe16")
		self.assertEqual(rows[0]["image_digest"], _VALID_DIGEST)

	def test_rewrite_aborts_on_invalid_digest(self):
		with self.assertRaises(self.script.CatalogUpdateError) as cm:
			self.script.run(self._args(image_digest="sha256:not-hex"))
		self.assertIn("digest must be a sha256 digest", str(cm.exception))

	def test_rewrite_aborts_on_invalid_tag(self):
		with self.assertRaises(self.script.CatalogUpdateError) as cm:
			self.script.run(self._args(tag_ref="v0.0@bad"))
		self.assertIn("invalid image tag", str(cm.exception))

	def test_rewrite_aborts_on_non_ghcr_repository(self):
		with self.assertRaises(self.script.CatalogUpdateError) as cm:
			self.script.run(self._args(image_name="docker.io/owner/kubeport-site"))
		self.assertIn("public GHCR repository", str(cm.exception))

	def test_rewrite_aborts_on_unparseable_frappe_branch(self):
		with self.assertRaises(self.script.CatalogUpdateError) as cm:
			self.script.run(self._args(frappe_branch="develop"))
		self.assertIn("--frappe-branch must match", str(cm.exception))

	def test_rewrite_aborts_when_no_row_matches(self):
		with self.assertRaises(self.script.CatalogUpdateError) as cm:
			self.script.run(self._args(frappe_branch="version-99"))
		self.assertIn("No catalog row matches", str(cm.exception))

	def test_rewrite_aborts_when_multiple_rows_match(self):
		payload = json.loads(self.catalog.read_text(encoding="utf-8"))
		payload["images"].append(dict(payload["images"][0], image_tag="0.0.0-frappe16"))
		self.catalog.write_text(json.dumps(payload), encoding="utf-8")

		with self.assertRaises(self.script.CatalogUpdateError) as cm:
			self.script.run(self._args())
		self.assertIn("Multiple catalog rows match", str(cm.exception))

	def test_rewrite_aborts_on_empty_git_sha(self):
		with self.assertRaises(self.script.CatalogUpdateError) as cm:
			self.script.run(self._args(git_sha=""))
		self.assertIn("--git-sha must not be empty", str(cm.exception))

	def test_emit_outputs_writes_to_github_output(self):
		output_file = _write_tempfile(b"")
		try:
			with patch.dict("os.environ", {"GITHUB_OUTPUT": str(output_file)}, clear=False):
				self.script.emit_outputs({"image_tag": "0.0.99-frappe16", "short_digest": "abc"})
			contents = output_file.read_text(encoding="utf-8")
		finally:
			output_file.unlink(missing_ok=True)
		self.assertIn("image_tag=0.0.99-frappe16\n", contents)
		self.assertIn("short_digest=abc\n", contents)


class _Args:
	def __init__(self, **kwargs):
		for key, value in kwargs.items():
			setattr(self, key, value)


def _write_tempfile(payload: bytes) -> Path:
	handle = tempfile.NamedTemporaryFile("wb", delete=False)
	with handle:
		handle.write(payload)
	return Path(handle.name)
