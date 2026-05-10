# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml
from frappe.tests import IntegrationTestCase, UnitTestCase

from kubeport.kubeport.doctype.helm_release.helm_release import (
	HelmRelease,
	_iter_storage_configs,
	_validate_storage_access_modes,
	build_release_docname,
	calculate_release_spec_hash,
	prepare_release_values,
	render_bundled_database_values,
	render_chart_starter_values,
	render_ingress_values,
	render_site_image_values,
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
			site_image="ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
			site_image_digest="sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		)
		hash_b = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="erp",
			release_name="bench-a",
			values_yaml="workers:\n  replicaCount: 2\n",
			site_image="ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
			site_image_digest="sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		)

		self.assertNotEqual(hash_a, hash_b)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	def test_render_site_image_values_injects_catalog_image(self, mock_get_value):
		mock_get_value.return_value = {
			"image_repository": "ghcr.io/esderj10/kubeport-site",
			"image_tag": "v1.0.0-frappe16",
			"image_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"status": "Active",
		}

		values_yaml = render_site_image_values(
			"workers:\n  replicaCount: 2\n",
			"ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
		)

		self.assertIn("repository: ghcr.io/esderj10/kubeport-site", values_yaml)
		self.assertIn(
			"tag: v1.0.0-frappe16@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			values_yaml,
		)
		self.assertIn("pullPolicy: IfNotPresent", values_yaml)
		self.assertIn("replicaCount: 2", values_yaml)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	def test_render_site_image_values_keeps_plain_tag_when_digest_missing(self, mock_get_value):
		mock_get_value.return_value = {
			"image_repository": "ghcr.io/esderj10/kubeport-site",
			"image_tag": "v1.0.0-frappe16",
			"image_digest": "",
			"status": "Active",
		}

		values_yaml = render_site_image_values(
			"",
			"ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
		)

		self.assertIn("tag: v1.0.0-frappe16", values_yaml)
		self.assertNotIn("@sha256:", values_yaml)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	def test_render_site_image_values_rejects_conflicting_manual_image_values(
		self,
		mock_get_value,
		mock_throw,
	):
		mock_get_value.return_value = {
			"image_repository": "ghcr.io/esderj10/kubeport-site",
			"image_tag": "v1.0.0-frappe16",
			"image_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"status": "Active",
		}
		mock_throw.side_effect = RuntimeError("selected Kubeport Site Image controls image.tag")

		with self.assertRaisesRegex(RuntimeError, "image.tag"):
			render_site_image_values(
				"image:\n  tag: manual\n",
				"ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
			)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	def test_render_site_image_values_injects_default_storage_class_when_missing(
		self,
		mock_get_value,
	):
		mock_get_value.return_value = {
			"image_repository": "ghcr.io/esderj10/kubeport-site",
			"image_tag": "v1.0.0-frappe16",
			"image_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"status": "Active",
		}

		values_yaml = render_site_image_values(
			None,
			"ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
			default_storage_class="local-path",
		)

		self.assertIn("persistence:", values_yaml)
		self.assertIn("worker:", values_yaml)
		self.assertIn("storageClass: local-path", values_yaml)
		# local-path is RWO-only; injection must downgrade the chart's
		# RWX default so the PVC can actually schedule on k3s/local-path.
		self.assertIn("- ReadWriteOnce", values_yaml)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	def test_render_site_image_values_keeps_rwx_default_for_non_local_path_class(
		self,
		mock_get_value,
	):
		mock_get_value.return_value = {
			"image_repository": "ghcr.io/esderj10/kubeport-site",
			"image_tag": "v1.0.0-frappe16",
			"image_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"status": "Active",
		}

		values_yaml = render_site_image_values(
			None,
			"ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
			default_storage_class="nfs-csi",
		)

		self.assertIn("storageClass: nfs-csi", values_yaml)
		# Non-local-path classes are assumed RWX-capable; let the chart
		# default ReadWriteMany flow through.
		self.assertNotIn("accessModes", values_yaml)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	def test_render_site_image_values_keeps_user_supplied_access_modes(
		self,
		mock_get_value,
	):
		mock_get_value.return_value = {
			"image_repository": "ghcr.io/esderj10/kubeport-site",
			"image_tag": "v1.0.0-frappe16",
			"image_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"status": "Active",
		}

		values_yaml = render_site_image_values(
			"persistence:\n  worker:\n    accessModes:\n      - ReadWriteMany\n",
			"ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
			default_storage_class="local-path",
		)

		self.assertIn("storageClass: local-path", values_yaml)
		self.assertIn("- ReadWriteMany", values_yaml)
		self.assertNotIn("ReadWriteOnce", values_yaml)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	def test_render_site_image_values_keeps_user_supplied_storage_class(
		self,
		mock_get_value,
	):
		mock_get_value.return_value = {
			"image_repository": "ghcr.io/esderj10/kubeport-site",
			"image_tag": "v1.0.0-frappe16",
			"image_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"status": "Active",
		}

		values_yaml = render_site_image_values(
			"persistence:\n  worker:\n    storageClass: fast-ssd\n",
			"ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
			default_storage_class="local-path",
		)

		self.assertIn("storageClass: fast-ssd", values_yaml)
		self.assertNotIn("local-path", values_yaml)

	@patch("kubeport.utils.discovery.discover_default_storage_class", return_value="local-path")
	def test_render_chart_starter_values_injects_storage_for_erpnext_without_site_image(
		self,
		mock_discover_default_storage_class,
	):
		chart_doc = SimpleNamespace(chart_name="erpnext")

		values_yaml = render_chart_starter_values(
			"persistence:\n  worker:\n    enabled: true\n    accessModes:\n    - ReadWriteMany\n",
			chart_doc,
			cluster_name="cluster-a",
		)

		self.assertIn("storageClass: local-path", values_yaml)
		self.assertIn("- ReadWriteOnce", values_yaml)
		self.assertNotIn("ReadWriteMany", values_yaml)
		mock_discover_default_storage_class.assert_called_once_with("cluster-a")

	@patch("kubeport.utils.discovery.discover_default_storage_class")
	def test_render_chart_starter_values_leaves_non_site_charts_raw(
		self, mock_discover_default_storage_class
	):
		chart_doc = SimpleNamespace(chart_name="nginx")

		values_yaml = render_chart_starter_values(
			"service:\n  type: ClusterIP\n",
			chart_doc,
			cluster_name="cluster-a",
		)

		self.assertEqual(values_yaml, "service:\n  type: ClusterIP\n")
		mock_discover_default_storage_class.assert_not_called()

	@patch("kubeport.utils.discovery.discover_default_storage_class")
	def test_render_chart_starter_values_keeps_user_supplied_storage_class(
		self,
		mock_discover_default_storage_class,
	):
		chart_doc = SimpleNamespace(chart_name="erpnext")
		raw_values = "persistence:\n  worker:\n    storageClass: fast-ssd\n"

		values_yaml = render_chart_starter_values(raw_values, chart_doc, cluster_name="cluster-a")

		self.assertEqual(values_yaml, raw_values)
		mock_discover_default_storage_class.assert_not_called()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_render_chart_starter_values_requires_cluster_for_site_chart(self, mock_throw):
		mock_throw.side_effect = RuntimeError("Select a target cluster")
		chart_doc = SimpleNamespace(chart_name="erpnext")

		with self.assertRaisesRegex(RuntimeError, "target cluster"):
			render_chart_starter_values("", chart_doc, cluster_name="")

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	@patch("kubeport.utils.discovery.discover_default_storage_class", return_value=None)
	def test_render_chart_starter_values_requires_default_storage_class(
		self,
		_mock_discover_default_storage_class,
		mock_throw,
	):
		mock_throw.side_effect = RuntimeError("no default StorageClass")
		chart_doc = SimpleNamespace(chart_name="erpnext")

		with self.assertRaisesRegex(RuntimeError, "default StorageClass"):
			render_chart_starter_values("", chart_doc, cluster_name="cluster-a")

	def test_render_bundled_database_values_noop_for_non_frappe_chart(self):
		chart_doc = SimpleNamespace(chart_name="postgres")
		self.assertEqual(
			render_bundled_database_values(
				"foo: bar\n", chart_doc, use_external_database=False, release_name="rel-a"
			),
			"foo: bar\n",
		)

	def test_render_bundled_database_values_noop_when_user_chose_external_db(self):
		chart_doc = SimpleNamespace(chart_name="erpnext")
		self.assertEqual(
			render_bundled_database_values("", chart_doc, use_external_database=True, release_name="rel-a"),
			"",
		)

	def test_render_bundled_database_values_preserves_user_supplied_dbhost(self):
		# When the operator already pinned dbHost, we don't touch it — they
		# may be pointing at an external DB even without ticking the toggle.
		chart_doc = SimpleNamespace(chart_name="erpnext")
		out = render_bundled_database_values(
			"dbHost: my-mariadb.svc\n",
			chart_doc,
			use_external_database=False,
			release_name="rel-a",
		)
		parsed = yaml.safe_load(out)
		self.assertEqual(parsed["dbHost"], "my-mariadb.svc")

	def test_render_bundled_database_values_injects_sibling_dbhost_for_frappe_default(self):
		# Default flow: chart values get dbHost=<release>-mariadb so the
		# bench's common_site_config.json points at the sibling release the
		# install/upgrade task is about to provision.
		chart_doc = SimpleNamespace(chart_name="erpnext")
		out = render_bundled_database_values("", chart_doc, use_external_database=False, release_name="rel-a")
		parsed = yaml.safe_load(out)
		self.assertEqual(parsed["dbHost"], "rel-a-mariadb")

	def test_render_bundled_database_values_skips_when_release_name_missing(self):
		# Form preview before the operator types a release name should not
		# inject a half-formed "<empty>-mariadb" hostname; the deploy task
		# always has the real name.
		chart_doc = SimpleNamespace(chart_name="erpnext")
		self.assertEqual(
			render_bundled_database_values("", chart_doc, use_external_database=False, release_name=""),
			"",
		)

	def test_render_bundled_database_values_overrides_chart_default_mariadb_enabled(self):
		# The frappe/erpnext chart ships values.yaml with vestigial
		# `mariadb.enabled: false` (the chart has no mariadb subchart).
		# Earlier we incorrectly treated that as "operator opted out" and
		# refused to inject dbHost — so the bundled flow silently broke.
		# The toggle is the source of truth, not the values blob.
		chart_doc = SimpleNamespace(chart_name="erpnext")
		out = render_bundled_database_values(
			"mariadb:\n  enabled: false\n",
			chart_doc,
			use_external_database=False,
			release_name="rel-a",
		)
		parsed = yaml.safe_load(out)
		self.assertEqual(parsed["dbHost"], "rel-a-mariadb")

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	@patch("kubeport.utils.discovery.discover_default_storage_class", return_value="local-path")
	def test_prepare_release_values_pipeline_injects_sibling_dbhost_for_frappe_default(
		self, _mock_discover_default_storage_class, mock_get_value
	):
		"""Wiring guard: prepare_release_values must call render_bundled_database_values
		so the Frappe-chart default flow ends up with dbHost=<release>-mariadb.  If a
		refactor ever drops the call from the pipeline, this test catches it before
		the install/upgrade task's sibling provisioning silently goes unused."""
		mock_get_value.return_value = None
		chart_doc = SimpleNamespace(chart_name="erpnext")
		out = prepare_release_values(
			"",
			chart_doc,
			cluster_name="cluster-a",
			use_external_database=False,
			release_name="rel-a",
		)
		parsed = yaml.safe_load(out)
		self.assertEqual(parsed["dbHost"], "rel-a-mariadb")

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	@patch("kubeport.utils.discovery.discover_default_storage_class", return_value="local-path")
	def test_prepare_release_values_pipeline_skips_dbhost_injection_for_external_db(
		self, _mock_discover_default_storage_class, mock_get_value
	):
		mock_get_value.return_value = None
		chart_doc = SimpleNamespace(chart_name="erpnext")
		out = prepare_release_values(
			"",
			chart_doc,
			cluster_name="cluster-a",
			use_external_database=True,
			release_name="rel-a",
		)
		parsed = yaml.safe_load(out) or {}
		self.assertNotIn("dbHost", parsed)

	def test_calculate_release_spec_hash_changes_when_use_external_database_toggled(self):
		hash_a = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="erp",
			release_name="bench-a",
			values_yaml="",
			use_external_database=False,
		)
		hash_b = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="erp",
			release_name="bench-a",
			values_yaml="",
			use_external_database=True,
		)
		self.assertNotEqual(hash_a, hash_b)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	@patch("kubeport.utils.discovery.discover_default_storage_class", return_value="local-path")
	def test_prepare_release_values_can_override_chart_default_image_for_starter_values(
		self,
		_mock_discover_default_storage_class,
		mock_get_value,
	):
		mock_get_value.return_value = {
			"image_repository": "ghcr.io/esderj10/kubeport-site",
			"image_tag": "v1.0.0-frappe16",
			"image_digest": "",
			"status": "Active",
		}
		chart_doc = SimpleNamespace(chart_name="erpnext")

		values_yaml = prepare_release_values(
			"image:\n  repository: frappe/erpnext\n  tag: v16.17.0\n",
			chart_doc,
			cluster_name="cluster-a",
			site_image="ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
			allow_site_image_override=True,
		)

		self.assertIn("repository: ghcr.io/esderj10/kubeport-site", values_yaml)
		self.assertIn("tag: v1.0.0-frappe16", values_yaml)
		self.assertIn("storageClass: local-path", values_yaml)

	def test_render_ingress_values_no_op_when_disabled(self):
		chart_doc = SimpleNamespace(chart_name="erpnext")
		raw = "workers:\n  replicaCount: 2\n"

		out = render_ingress_values(
			raw,
			chart_doc,
			ingress_enabled=0,
			hostname="erp.example.com",
			class_name="nginx",
			cluster_issuer=None,
			release_name="bench-a",
		)

		self.assertEqual(out, raw)

	def test_render_ingress_values_no_op_for_non_frappe_chart(self):
		chart_doc = SimpleNamespace(chart_name="redis")
		raw = "service:\n  type: ClusterIP\n"

		out = render_ingress_values(
			raw,
			chart_doc,
			ingress_enabled=1,
			hostname="erp.example.com",
			class_name="nginx",
			cluster_issuer="letsencrypt-prod",
			release_name="bench-a",
		)

		self.assertEqual(out, raw)

	def test_render_ingress_values_emits_http_block_without_issuer(self):
		chart_doc = SimpleNamespace(chart_name="erpnext")

		out = render_ingress_values(
			"workers:\n  replicaCount: 2\n",
			chart_doc,
			ingress_enabled=1,
			hostname="erp.example.com",
			class_name="nginx",
			cluster_issuer=None,
			release_name="bench-a",
		)

		parsed = yaml.safe_load(out)
		ingress = parsed["ingress"]
		self.assertTrue(ingress["enabled"])
		self.assertEqual(ingress["className"], "nginx")
		self.assertEqual(ingress["hosts"][0]["host"], "erp.example.com")
		self.assertEqual(ingress["hosts"][0]["paths"][0]["path"], "/")
		self.assertEqual(ingress["hosts"][0]["paths"][0]["pathType"], "ImplementationSpecific")
		self.assertNotIn("annotations", ingress)
		self.assertNotIn("tls", ingress)

	def test_render_ingress_values_emits_tls_block_with_issuer(self):
		chart_doc = SimpleNamespace(chart_name="erpnext")

		out = render_ingress_values(
			None,
			chart_doc,
			ingress_enabled=1,
			hostname="erp.example.com",
			class_name="nginx",
			cluster_issuer="letsencrypt-prod",
			release_name="bench-a",
		)

		parsed = yaml.safe_load(out)
		ingress = parsed["ingress"]
		self.assertEqual(ingress["annotations"]["cert-manager.io/cluster-issuer"], "letsencrypt-prod")
		self.assertEqual(ingress["tls"][0]["secretName"], "bench-a-tls")
		self.assertEqual(ingress["tls"][0]["hosts"], ["erp.example.com"])

	def test_render_ingress_values_omits_class_name_when_blank(self):
		chart_doc = SimpleNamespace(chart_name="erpnext")

		out = render_ingress_values(
			None,
			chart_doc,
			ingress_enabled=1,
			hostname="erp.example.com",
			class_name="",
			cluster_issuer=None,
			release_name="bench-a",
		)

		parsed = yaml.safe_load(out)
		self.assertNotIn("className", parsed["ingress"])

	def test_render_ingress_values_replaces_disabled_chart_default_ingress(self):
		chart_doc = SimpleNamespace(chart_name="erpnext")
		raw = "ingress:\n  enabled: false\n"

		out = render_ingress_values(
			raw,
			chart_doc,
			ingress_enabled=1,
			hostname="erp.example.com",
			class_name="nginx",
			cluster_issuer="letsencrypt-prod",
			release_name="bench-a",
		)

		parsed = yaml.safe_load(out)
		self.assertTrue(parsed["ingress"]["enabled"])
		self.assertEqual(parsed["ingress"]["hosts"][0]["host"], "erp.example.com")
		self.assertEqual(parsed["ingress"]["className"], "nginx")

	def test_render_ingress_values_preserves_enabled_user_supplied_ingress(self):
		chart_doc = SimpleNamespace(chart_name="erpnext")
		raw = (
			"ingress:\n"
			"  enabled: true\n"
			"  annotations:\n"
			"    nginx.ingress.kubernetes.io/proxy-body-size: 50m\n"
			"  hosts:\n"
			"    - host: custom.example.com\n"
			"      paths:\n"
			"        - path: /custom\n"
			"          pathType: Prefix\n"
		)

		out = render_ingress_values(
			raw,
			chart_doc,
			ingress_enabled=1,
			hostname="erp.example.com",
			class_name="nginx",
			cluster_issuer="letsencrypt-prod",
			release_name="bench-a",
		)

		self.assertEqual(out, raw)

	def test_render_ingress_values_replaces_stale_simple_enabled_ingress(self):
		chart_doc = SimpleNamespace(chart_name="erpnext")
		raw = (
			"ingress:\n"
			"  enabled: true\n"
			"  className: nginx\n"
			"  hosts:\n"
			"    - host: erp.local\n"
			"      paths:\n"
			"        - path: /\n"
			"          pathType: ImplementationSpecific\n"
		)

		out = render_ingress_values(
			raw,
			chart_doc,
			ingress_enabled=1,
			hostname="test-ingress.172.23.0.2.nip.io",
			class_name="traefik",
			cluster_issuer="",
			release_name="bench-a",
		)

		parsed = yaml.safe_load(out)
		self.assertEqual(parsed["ingress"]["hosts"][0]["host"], "test-ingress.172.23.0.2.nip.io")
		self.assertEqual(parsed["ingress"]["className"], "traefik")

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_render_ingress_values_throws_when_hostname_blank(self, mock_throw):
		mock_throw.side_effect = RuntimeError("Hostname is required")
		chart_doc = SimpleNamespace(chart_name="erpnext")

		with self.assertRaisesRegex(RuntimeError, "Hostname is required"):
			render_ingress_values(
				None,
				chart_doc,
				ingress_enabled=1,
				hostname="",
				class_name="nginx",
				cluster_issuer=None,
				release_name="bench-a",
			)

	def test_calculate_release_spec_hash_changes_when_ingress_enabled_toggled(self):
		hash_a = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="erp",
			release_name="bench-a",
			values_yaml="workers:\n  replicaCount: 2\n",
			ingress_enabled=False,
		)
		hash_b = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="erp",
			release_name="bench-a",
			values_yaml="workers:\n  replicaCount: 2\n",
			ingress_enabled=True,
			ingress_hostname="erp.example.com",
		)

		self.assertNotEqual(hash_a, hash_b)

	def test_calculate_release_spec_hash_changes_when_ingress_hostname_changes(self):
		hash_a = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="erp",
			release_name="bench-a",
			values_yaml="",
			ingress_enabled=True,
			ingress_hostname="erp.example.com",
		)
		hash_b = calculate_release_spec_hash(
			chart="repo/erpnext",
			chart_version="8.0.41",
			namespace="erp",
			release_name="bench-a",
			values_yaml="",
			ingress_enabled=True,
			ingress_hostname="erp.staging.example.com",
		)

		self.assertNotEqual(hash_a, hash_b)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	@patch("kubeport.utils.discovery.discover_default_storage_class", return_value="local-path")
	def test_prepare_release_values_pipeline_renders_ingress_alongside_site_image(
		self,
		_mock_discover_default_storage_class,
		mock_get_value,
	):
		mock_get_value.return_value = {
			"image_repository": "ghcr.io/esderj10/kubeport-site",
			"image_tag": "v1.0.0-frappe16",
			"image_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"status": "Active",
		}
		chart_doc = SimpleNamespace(chart_name="erpnext")

		out = prepare_release_values(
			"persistence:\n  worker:\n    enabled: true\n",
			chart_doc,
			cluster_name="cluster-a",
			site_image="ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
			ingress_enabled=1,
			ingress_hostname="erp.example.com",
			ingress_class_name="nginx",
			ingress_cluster_issuer="letsencrypt-prod",
			release_name="bench-a",
		)

		parsed = yaml.safe_load(out)
		self.assertEqual(parsed["persistence"]["worker"]["storageClass"], "local-path")
		self.assertIn("repository: ghcr.io/esderj10/kubeport-site", out)
		self.assertTrue(parsed["ingress"]["enabled"])
		self.assertEqual(parsed["ingress"]["hosts"][0]["host"], "erp.example.com")
		self.assertEqual(parsed["ingress"]["tls"][0]["secretName"], "bench-a-tls")

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	def test_render_site_image_values_omits_storage_class_when_no_default(
		self,
		mock_get_value,
	):
		mock_get_value.return_value = {
			"image_repository": "ghcr.io/esderj10/kubeport-site",
			"image_tag": "v1.0.0-frappe16",
			"image_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"status": "Active",
		}

		values_yaml = render_site_image_values(
			None,
			"ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
			default_storage_class=None,
		)

		self.assertNotIn("storageClass", values_yaml)
		self.assertNotIn("persistence:", values_yaml)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.db.get_value")
	def test_render_site_image_values_preserves_other_persistence_keys(
		self,
		mock_get_value,
	):
		mock_get_value.return_value = {
			"image_repository": "ghcr.io/esderj10/kubeport-site",
			"image_tag": "v1.0.0-frappe16",
			"image_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			"status": "Active",
		}

		values_yaml = render_site_image_values(
			"persistence:\n  worker:\n    size: 16Gi\n",
			"ghcr.io/esderj10/kubeport-site:v1.0.0-frappe16",
			default_storage_class="local-path",
		)

		self.assertIn("size: 16Gi", values_yaml)
		self.assertIn("storageClass: local-path", values_yaml)

	def test_iter_storage_configs_finds_nested_persistence_blocks(self):
		configs = _iter_storage_configs(
			{
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
			}
		)

		self.assertEqual(
			configs,
			[
				("values.persistence.worker", "local-path", ["ReadWriteMany"]),
				("values.persistence.logs", "local-path", ["ReadWriteOnce"]),
			],
		)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_validate_storage_access_modes_rejects_local_path_rwx(self, mock_throw):
		_validate_storage_access_modes(
			{
				"persistence": {
					"worker": {
						"storageClass": "local-path",
						"accessModes": ["ReadWriteMany"],
					},
				},
			}
		)

		mock_throw.assert_called_once()
		self.assertIn("values.persistence.worker", mock_throw.call_args.args[0])
		self.assertIn("local-path", mock_throw.call_args.args[0])
		self.assertIn("ReadWriteMany", mock_throw.call_args.args[0])

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	def test_validate_storage_access_modes_allows_local_path_rwo(self, mock_throw):
		_validate_storage_access_modes(
			{
				"persistence": {
					"worker": {
						"storageClass": "local-path",
						"accessModes": ["ReadWriteOnce"],
					},
				},
			}
		)

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
	def test_validate_throws_when_ingress_enabled_without_hostname(self, mock_throw):
		mock_throw.side_effect = RuntimeError("Hostname is required")
		doc = object.__new__(HelmRelease)
		doc.name = "cluster-a/default/bench-a"
		doc.cluster = "cluster-a"
		doc.namespace = "default"
		doc.release_name = "bench-a"
		doc.chart = "repo/erpnext"
		doc.chart_version = "8.0.41"
		doc.values = ""
		doc.last_applied_spec_hash = ""
		doc.ingress_enabled = 1
		doc.ingress_hostname = ""
		doc.ingress_class_name = ""
		doc.ingress_cluster_issuer = ""
		doc.is_new = lambda: False

		with self.assertRaisesRegex(RuntimeError, "Hostname is required"):
			doc.validate()

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
		mock_enqueue.assert_called_once()
		call_kwargs = mock_enqueue.call_args.kwargs
		self.assertEqual(
			mock_enqueue.call_args.args,
			("kubeport.tasks.helm_tasks.install_or_upgrade_release",),
		)
		self.assertEqual(call_kwargs["release_name"], "cluster-a/default/bench-a")
		self.assertEqual(call_kwargs["operation_token"], "tok-1")
		self.assertEqual(call_kwargs["queue"], "long")
		self.assertTrue(call_kwargs["enqueue_after_commit"])
		# TODO-14: a correlation_id (UUID4 string) must be threaded through
		# every enqueue call so a single operation is grep-able from web →
		# enqueue → worker.
		self.assertIsInstance(call_kwargs.get("correlation_id"), str)
		self.assertEqual(len(call_kwargs["correlation_id"]), 36)

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
		mock_enqueue.assert_called_once()
		call_kwargs = mock_enqueue.call_args.kwargs
		self.assertEqual(
			mock_enqueue.call_args.args,
			("kubeport.tasks.helm_tasks.uninstall_release",),
		)
		self.assertEqual(call_kwargs["release_name"], "cluster-a/default/bench-a")
		self.assertEqual(call_kwargs["operation_token"], "tok-2")
		self.assertIsInstance(call_kwargs.get("correlation_id"), str)

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

	@patch(
		"kubeport.kubeport.doctype.helm_release.helm_release.secrets.token_hex",
		return_value="tok-none",
	)
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.enqueue")
	@patch(
		"kubeport.kubeport.doctype.helm_release.helm_release.frappe.get_all",
		return_value=[],
	)
	def test_uninstall_release_coerces_none_kwargs_from_typing_validator(
		self,
		_mock_get_all,
		mock_enqueue,
		_mock_msgprint,
		_mock_token_hex,
	):
		# Regression: the regular Uninstall button calls run_doc_method without
		# args, and Frappe's typing validator passes force=None / confirmation=None
		# instead of using Python defaults. The method must accept that.
		doc = object.__new__(HelmRelease)
		doc.status = "Deployed"
		doc.name = "cluster-a/default/bench-a"
		doc.release_name = "bench-a"
		doc.db_set = MagicMock()

		doc.uninstall_release(force=None, confirmation=None)

		doc.db_set.assert_any_call("status", "Uninstalling")
		mock_enqueue.assert_called_once()

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.throw")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.get_all")
	def test_uninstall_release_blocks_active_frappe_sites(
		self,
		mock_get_all,
		mock_throw,
	):
		mock_get_all.return_value = [
			{
				"name": "cluster-a/default/bench-a/site.local",
				"site_name": "site.local",
				"status": "Active",
				"operation_job_name": "",
			}
		]
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
		mock_enqueue.assert_called_once()
		call_kwargs = mock_enqueue.call_args.kwargs
		self.assertEqual(
			mock_enqueue.call_args.args,
			("kubeport.tasks.helm_tasks.rollback_release",),
		)
		self.assertEqual(call_kwargs["release_name"], "cluster-a/default/bench-a")
		self.assertEqual(call_kwargs["operation_token"], "tok-4")
		self.assertEqual(call_kwargs["target_revision"], 2)
		self.assertIsInstance(call_kwargs.get("correlation_id"), str)

	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.logger")
	@patch("kubeport.utils.release_health.walk")
	def test_get_release_health_returns_structured_rows(self, mock_walk, _mock_logger):
		doc = object.__new__(HelmRelease)
		doc.name = "cluster-a/default/bench-a"
		mock_walk.return_value = [
			MagicMock(
				to_dict=lambda: {
					"kind": "Deployment",
					"name": "bench-a",
					"namespace": "default",
					"ready": True,
					"reason": "",
					"message": "1/1 available",
				}
			),
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
	@patch(
		"kubeport.utils.release_health.walk",
		side_effect=RuntimeError("Helm command failed: Error: release: not found"),
	)
	def test_get_release_health_formats_missing_release_as_empty_state(self, _mock_walk, _mock_logger):
		doc = object.__new__(HelmRelease)
		doc.name = "cluster-a/default/bench-a"

		result = doc.get_release_health()

		self.assertEqual(result["rows"], [])
		self.assertIn("No Helm release exists yet", result["error"])

	@patch("kubeport.utils.helm.show_values")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.get_doc")
	def test_load_defaults_uses_cached_values_for_latest_chart_version(self, mock_get_doc, mock_show_values):
		chart_doc = SimpleNamespace(
			chart_name="nginx",
			default_values="service:\n  type: ClusterIP\n",
			latest_version="1.2.3",
			get_chart_reference=lambda: "repo/nginx",
		)
		mock_get_doc.return_value = chart_doc
		doc = object.__new__(HelmRelease)
		doc.chart = "repo/nginx"
		doc.chart_version = "1.2.3"
		doc.cluster = "cluster-a"
		doc.site_image = ""

		values_yaml = doc.load_defaults()

		self.assertEqual(values_yaml, "service:\n  type: ClusterIP\n")
		mock_show_values.assert_not_called()

	@patch("kubeport.utils.helm.show_values", return_value="replicaCount: 1\n")
	@patch("kubeport.kubeport.doctype.helm_release.helm_release.frappe.get_doc")
	def test_load_defaults_fetches_values_for_non_latest_chart_version(self, mock_get_doc, mock_show_values):
		chart_doc = SimpleNamespace(
			chart_name="nginx",
			default_values="replicaCount: 2\n",
			latest_version="2.0.0",
			get_chart_reference=lambda: "repo/nginx",
		)
		mock_get_doc.return_value = chart_doc
		doc = object.__new__(HelmRelease)
		doc.chart = "repo/nginx"
		doc.chart_version = "1.0.0"
		doc.cluster = "cluster-a"
		doc.site_image = ""

		values_yaml = doc.load_defaults()

		self.assertEqual(values_yaml, "replicaCount: 1\n")
		mock_show_values.assert_called_once_with("repo/nginx", version="1.0.0")

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
