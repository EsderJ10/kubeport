# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import MagicMock


from frappe.tests import UnitTestCase

from kubeport.tasks.site_tasks import (
	_bench_drop_site_command,
	_bench_migrate_command,
	_bench_new_site_command,
	_build_creds_secret_manifest,
	_build_drop_creds_secret_manifest,
	_build_drop_env,
	_build_env,
	_build_op_job_manifest,
	_clone_reference_pod_spec,
	_job_name,
	_merge_env,
	_parse_install_apps,
	_safe_label_value,
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

	def test_bench_command_quotes_install_apps(self):
		cmd = _bench_new_site_command("s1", ["erpnext", "payments"], force=False)
		self.assertIn('--install-app="erpnext"', cmd)
		self.assertIn('--install-app="payments"', cmd)


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
			manifest["spec"]["activeDeadlineSeconds"], _JOB_ACTIVE_DEADLINE_SECONDS,
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

		with patch.object(site_tasks, "_site_operation_matches", side_effect=lambda *a, **kw: next(match_calls)), \
			patch.object(site_tasks, "frappe") as mock_frappe, \
			patch.object(site_tasks, "get_k8s_api_client") as mock_get_client, \
			patch.object(site_tasks, "_select_site_discovery_pod") as mock_select, \
			patch.object(site_tasks, "_clone_reference_pod_spec") as mock_clone, \
			patch.object(site_tasks, "apply_resource"), \
			patch.object(site_tasks, "_best_effort_delete_job", side_effect=_capture_delete_job), \
			patch.object(site_tasks, "_best_effort_delete_secret", side_effect=_capture_delete_secret), \
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


class UnitTestBestEffortDeleteJob(UnitTestCase):
	def test_ignores_404_without_logging(self):
		from unittest.mock import patch

		from kubernetes.client.rest import ApiException

		from kubeport.tasks import site_tasks

		batch_api = MagicMock()
		batch_api.delete_namespaced_job.side_effect = ApiException(status=404, reason="NotFound")

		with patch.object(site_tasks, "client") as mock_client, \
			patch.object(site_tasks.frappe, "logger") as mock_logger:
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

		with patch.object(site_tasks, "client") as mock_client, \
			patch.object(site_tasks.frappe, "logger") as mock_logger:
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
