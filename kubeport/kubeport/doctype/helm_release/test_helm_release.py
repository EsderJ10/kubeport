# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase, UnitTestCase

from kubeport.kubeport.doctype.helm_release.helm_release import (
	HelmRelease,
	build_release_docname,
	calculate_release_spec_hash,
	_iter_storage_configs,
	render_site_image_values,
	_validate_storage_access_modes,
)


class UnitTestHelmRelease(UnitTestCase):
	def test_build_release_docname_scopes_release_identity_to_cluster_and_namespace(self):
		self.assertEqual(
			build_release_docname("cluster-a", "erp", "bench-a"),
			"cluster-a/erp/bench-a",
		)

	def test_calculate_release_spec_hash_normalizes_yaml_key_order(self):
		hash_a = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="erp",
			release_name="bench-a",
			values_yaml="workers:\n  replicaCount: 2\nimage:\n  tag: v1\n",
		)
		hash_b = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="erp",
			release_name="bench-a",
			values_yaml="image:\n  tag: v1\nworkers:\n  replicaCount: 2\n",
		)

		self.assertEqual(hash_a, hash_b)

	def test_calculate_release_spec_hash_changes_when_site_image_digest_changes(self):
		hash_a = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="erp",
			release_name="bench-a",
			values_yaml="workers:\n  replicaCount: 2\n",
			site_image="ghcr.io/losfavs/kubeport-site:v1.0.0-frappe16",
			site_image_digest="sha256:aaa",
		)
		hash_b = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="erp",
			release_name="bench-a",
			values_yaml="workers:\n  replicaCount: 2\n",
			site_image="ghcr.io/losfavs/kubeport-site:v1.0.0-frappe16",
			site_image_digest="sha256:bbb",
		)

		self.assertNotEqual(hash_a, hash_b)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	def test_render_site_image_values_injects_catalog_image(self, mock_get_value):
		mock_get_value.return_value = {
			"image_repository": "ghcr.io/losfavs/kubeport-site",
			"image_tag": "v1.0.0-frappe16",
			"image_digest": "sha256:aaa",
			"status": "Active",
		}

		values_yaml = render_site_image_values(
			"workers:\n  replicaCount: 2\n",
			"ghcr.io/losfavs/kubeport-site:v1.0.0-frappe16",
		)

		self.assertIn("repository: ghcr.io/losfavs/kubeport-site", values_yaml)
		self.assertIn("tag: v1.0.0-frappe16", values_yaml)
		self.assertIn("pullPolicy: IfNotPresent", values_yaml)
		self.assertIn("replicaCount: 2", values_yaml)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	def test_render_site_image_values_rejects_conflicting_manual_image_values(
		self,
		mock_get_value,
		mock_throw,
	):
		mock_get_value.return_value = {
			"image_repository": "ghcr.io/losfavs/kubeport-site",
			"image_tag": "v1.0.0-frappe16",
			"image_digest": "sha256:aaa",
			"status": "Active",
		}
		mock_throw.side_effect = RuntimeError("selected Kubeport Site Image controls image.tag")

		with self.assertRaisesRegex(RuntimeError, "image.tag"):
			render_site_image_values(
				"image:\n  tag: manual\n",
				"ghcr.io/losfavs/kubeport-site:v1.0.0-frappe16",
			)

	def test_iter_storage_configs_finds_nested_persistence_blocks(self):
		configs = _iter_storage_configs({
			"persistence": {
				"worker": {
					"storageClass": "local-path",
					"accessModes": ["ReadWriteMany"],
				},
				"logs": {
					"storageClass": "local-path",
					"accessModes": ["ReadWriteOnce"],
				},
			},
		})

		self.assertEqual(configs, [
			("values.persistence.worker", "local-path", ["ReadWriteMany"]),
			("values.persistence.logs", "local-path", ["ReadWriteOnce"]),
		])

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_validate_storage_access_modes_rejects_local_path_rwx(self, mock_throw):
		_validate_storage_access_modes({
			"persistence": {
				"worker": {
					"storageClass": "local-path",
					"accessModes": ["ReadWriteMany"],
				},
			},
		})

		mock_throw.assert_called_once()
		self.assertIn("values.persistence.worker", mock_throw.call_args.args[0])
		self.assertIn("local-path", mock_throw.call_args.args[0])
		self.assertIn("ReadWriteMany", mock_throw.call_args.args[0])

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_validate_storage_access_modes_allows_local_path_rwo(self, mock_throw):
		_validate_storage_access_modes({
			"persistence": {
				"worker": {
					"storageClass": "local-path",
					"accessModes": ["ReadWriteOnce"],
				},
			},
		})

		mock_throw.assert_not_called()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_deploy_release_rejects_duplicate_in_progress_deploy(self, mock_throw):
		doc = object.__new__(HelmRelease)
		doc.chart = "ERPNext"
		doc.cluster = "cluster-a"
		doc.status = "In Progress"
		doc.db_set = MagicMock()
		doc.name = "bench-a"
		doc.release_name = "bench-a"
		mock_throw.side_effect = RuntimeError("Deployment is already in progress for this release.")

		with self.assertRaisesRegex(RuntimeError, "already in progress"):
			doc.deploy_release()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_validate_rejects_identity_changes_after_creation(self, mock_throw):
		doc = object.__new__(HelmRelease)
		doc.name = "cluster-a/default/bench-a"
		doc.cluster = "cluster-b"
		doc.namespace = "default"
		doc.release_name = "bench-a"
		doc.values = ""
		doc.is_new = lambda: False
		mock_throw.side_effect = RuntimeError("immutable after creation")

		with self.assertRaisesRegex(RuntimeError, "immutable after creation"):
			doc.validate()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.get_doc")
	def test_validate_marks_pending_changes_when_desired_hash_differs_from_last_applied(
		self,
		mock_get_doc,
	):
		mock_get_doc.return_value = MagicMock(latest_version="8.0.41")
		doc = object.__new__(HelmRelease)
		doc.name = "cluster-a/default/bench-a"
		doc.cluster = "cluster-a"
		doc.namespace = "default"
		doc.release_name = "bench-a"
		doc.chart = "repo/erpnext"
		doc.chart_version = "8.0.41"
		doc.values = "workers:\n  replicaCount: 2\n"
		doc.last_applied_spec_hash = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="default",
			release_name="bench-a",
			values_yaml="workers:\n  replicaCount: 1\n",
		)
		doc.is_new = lambda: False

		doc.validate()

		self.assertTrue(doc.pending_changes)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_on_trash_refuses_resource_owning_statuses(self, mock_throw):
		mock_throw.side_effect = RuntimeError("Uninstall the release first")

		for resource_owning_status in ("In Progress", "Deployed", "Degraded", "Uninstalling", "Failed"):
			with self.subTest(status=resource_owning_status):
				doc = object.__new__(HelmRelease)
				doc.name = "cluster-a/default/bench-a"
				doc.status = resource_owning_status

				with self.assertRaisesRegex(RuntimeError, "Uninstall the release first"):
					doc.on_trash()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_on_trash_allows_draft_only(self, mock_throw):
		doc = object.__new__(HelmRelease)
		doc.name = "cluster-a/default/bench-a"
		doc.status = "Draft"

		doc.on_trash()

		mock_throw.assert_not_called()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.secrets.token_hex", return_value="tok-1")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.enqueue")
	def test_deploy_release_rotates_operation_token_and_enqueues_with_it(
		self,
		mock_enqueue,
		_mock_msgprint,
		_mock_token_hex,
	):
		doc = object.__new__(HelmRelease)
		doc.chart = "ERPNext"
		doc.cluster = "cluster-a"
		doc.status = "Draft"
		doc.name = "cluster-a/default/bench-a"
		doc.release_name = "bench-a"
		doc.db_set = MagicMock()

		doc.deploy_release()

		doc.db_set.assert_any_call("operation_token", "tok-1")
		doc.db_set.assert_any_call("status", "In Progress")
		mock_enqueue.assert_called_once_with(
			"kubeport.tasks.helm_tasks.install_or_upgrade_release",
			release_name="cluster-a/default/bench-a",
			operation_token="tok-1",
			queue="long",
			enqueue_after_commit=True,
		)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.secrets.token_hex", return_value="tok-2")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.enqueue")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.get_all", return_value=[])
	def test_uninstall_release_rotates_operation_token_and_enqueues_with_it(
		self,
		_mock_get_all,
		mock_enqueue,
		_mock_msgprint,
		_mock_token_hex,
	):
		doc = object.__new__(HelmRelease)
		doc.status = "Deployed"
		doc.name = "cluster-a/default/bench-a"
		doc.release_name = "bench-a"
		doc.db_set = MagicMock()

		doc.uninstall_release()

		doc.db_set.assert_any_call("operation_token", "tok-2")
		doc.db_set.assert_any_call("status", "Uninstalling")
		mock_enqueue.assert_called_once_with(
			"kubeport.tasks.helm_tasks.uninstall_release",
			release_name="cluster-a/default/bench-a",
			operation_token="tok-2",
			queue="long",
			enqueue_after_commit=True,
		)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.secrets.token_hex", return_value="tok-3")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.enqueue")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.get_all", return_value=[])
	def test_uninstall_release_allows_failed_rows(
		self,
		_mock_get_all,
		mock_enqueue,
		_mock_msgprint,
		_mock_token_hex,
	):
		doc = object.__new__(HelmRelease)
		doc.status = "Failed"
		doc.name = "cluster-a/default/bench-a"
		doc.release_name = "bench-a"
		doc.db_set = MagicMock()

		doc.uninstall_release()

		doc.db_set.assert_any_call("operation_token", "tok-3")
		doc.db_set.assert_any_call("status", "Uninstalling")
		mock_enqueue.assert_called_once()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.get_all")
	def test_uninstall_release_blocks_active_frappe_sites(
		self,
		mock_get_all,
		mock_throw,
	):
		mock_get_all.return_value = [{
			"name": "cluster-a/default/bench-a/site.local",
			"site_name": "site.local",
			"status": "Active",
			"operation_job_name": "",
		}]
		mock_throw.side_effect = RuntimeError("still depend")
		doc = object.__new__(HelmRelease)
		doc.status = "Deployed"
		doc.name = "cluster-a/default/bench-a"
		doc.release_name = "bench-a"

		with self.assertRaisesRegex(RuntimeError, "still depend"):
			doc.uninstall_release()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.secrets.token_hex", return_value="tok-4")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.enqueue")
	def test_rollback_release_rotates_operation_token_and_enqueues_revision(
		self,
		mock_enqueue,
		_mock_msgprint,
		_mock_token_hex,
	):
		doc = object.__new__(HelmRelease)
		doc.status = "Deployed"
		doc.name = "cluster-a/default/bench-a"
		doc.release_name = "bench-a"
		doc.db_set = MagicMock()

		doc.rollback_release(2)

		doc.db_set.assert_any_call("operation_token", "tok-4")
		doc.db_set.assert_any_call("operation_type", "Rollback")
		doc.db_set.assert_any_call("status", "In Progress")
		mock_enqueue.assert_called_once_with(
			"kubeport.tasks.helm_tasks.rollback_release",
			release_name="cluster-a/default/bench-a",
			operation_token="tok-4",
			target_revision=2,
			queue="long",
			enqueue_after_commit=True,
		)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.logger")
	@patch("kubeport.utils.release_health.walk")
	def test_get_release_health_returns_structured_rows(self, mock_walk, _mock_logger):
		doc = object.__new__(HelmRelease)
		doc.name = "cluster-a/default/bench-a"
		mock_walk.return_value = [
			MagicMock(to_dict=lambda: {
				"kind": "Deployment",
				"name": "bench-a",
				"namespace": "default",
				"ready": True,
				"reason": "",
				"message": "1/1 available",
			}),
		]

		result = doc.get_release_health()

		self.assertEqual(result["error"], "")
		self.assertEqual(result["rows"][0]["kind"], "Deployment")

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.logger")
	@patch("kubeport.utils.release_health.walk", side_effect=RuntimeError("helm get manifest failed"))
	def test_get_release_health_returns_structured_error(self, _mock_walk, _mock_logger):
		doc = object.__new__(HelmRelease)
		doc.name = "cluster-a/default/bench-a"

		result = doc.get_release_health()

		self.assertEqual(result["rows"], [])
		self.assertIn("helm get manifest failed", result["error"])

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.logger")
	@patch("kubeport.utils.helm.history")
	def test_get_release_history_returns_structured_rows(self, mock_history, _mock_logger):
		doc = object.__new__(HelmRelease)
		doc.name = "cluster-a/default/bench-a"
		doc.release_name = "bench-a"
		doc.namespace = "default"
		doc.cluster = "cluster-a"
		mock_history.return_value = [{"revision": 1, "status": "deployed"}]

		result = doc.get_release_history()

		self.assertEqual(result["error"], "")
		self.assertEqual(result["rows"][0]["status"], "deployed")

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.logger")
	@patch("kubeport.utils.helm.history", side_effect=RuntimeError("helm history failed"))
	def test_get_release_history_returns_structured_error(self, _mock_history, _mock_logger):
		doc = object.__new__(HelmRelease)
		doc.name = "cluster-a/default/bench-a"
		doc.release_name = "bench-a"
		doc.namespace = "default"
		doc.cluster = "cluster-a"

		result = doc.get_release_history()

		self.assertEqual(result["rows"], [])
		self.assertIn("helm history failed", result["error"])


# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]


class IntegrationTestHelmRelease(IntegrationTestCase):
	"""Integration tests for HelmRelease."""

	pass
