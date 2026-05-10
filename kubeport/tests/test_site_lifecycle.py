# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

"""
Frappe Site Lifecycle Simulation Tests

End-to-end scenario tests that exercise the full controller → task →
reconciliation chain with comprehensive K8s API mocking.  These sit between
the existing unit tests (which test individual helpers in isolation) and real
integration tests (which require a live cluster).

Each scenario builds a minimal set of mocks that simulate the K8s API
surface (BatchV1Api, CoreV1Api, stream) and then drives a complete lifecycle
arc — create → reconcile → finalize — verifying the DB-level status
transitions and cleanup behavior the unit tests cannot reach.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests import UnitTestCase

# ---------------------------------------------------------------------------
# Mock factories
# ---------------------------------------------------------------------------


def _mock_frappe_site_doc(
	site_name: str = "demo.example.com",
	status: str = "Draft",
	**overrides,
) -> MagicMock:
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
	doc.status = status
	doc.cluster = "cluster-a"
	doc.namespace = "bench-ns"
	doc.operation_job_name = ""
	doc.operation_job_token = ""
	doc.operation_token = ""
	doc.get_password = MagicMock(
		side_effect=lambda field: {
			"admin_password": "admin-pw",
			"db_root_password": "root-pw",
		}.get(field, "")
	)
	doc.db_set = MagicMock()
	for k, v in overrides.items():
		setattr(doc, k, v)
	return doc


def _mock_release() -> MagicMock:
	release = MagicMock()
	release.cluster = "cluster-a"
	release.namespace = "bench-ns"
	release.release_name = "bench-a"
	release.status = "Deployed"
	return release


def _mock_ref_spec() -> dict:
	return {
		"image": "frappe/erpnext:v15.0.0",
		"pod_level": {},
		"container_env": [],
		"container_env_from": [],
		"container_resources": None,
		"container_security_context": None,
		"volume_mounts": [{"name": "sites", "mountPath": "/home/frappe/frappe-bench/sites"}],
		"volumes": [{"name": "sites", "persistentVolumeClaim": {"claimName": "sites-pvc"}}],
	}


def _reconcile_site_row(**overrides) -> SimpleNamespace:
	defaults = {
		"name": "release-a/demo.example.com",
		"cluster": "cluster-a",
		"namespace": "bench-ns",
		"status": "In Progress",
		"operation_job_name": "ks-demo-abcdef123456",
		"operation_job_token": "tok-aaa",
		"bench_release": "release-a",
		"site_name": "demo.example.com",
	}
	defaults.update(overrides)
	return SimpleNamespace(**defaults)


def _k8s_job(succeeded: int = 0, failed: int = 0, name: str = "ks-demo-abcdef123456"):
	from kubeport.tasks.site_tasks import MANAGED_BY_VALUE, SITE_DOC_LABEL, _safe_label_value

	return SimpleNamespace(
		metadata=SimpleNamespace(
			name=name,
			labels={
				"app.kubernetes.io/managed-by": MANAGED_BY_VALUE,
				SITE_DOC_LABEL: _safe_label_value("release-a/demo.example.com"),
			},
		),
		status=SimpleNamespace(succeeded=succeeded, failed=failed),
	)


# ---------------------------------------------------------------------------
# Scenario 1: Happy-path create → Active
# ---------------------------------------------------------------------------


class TestCreateToActive(UnitTestCase):
	"""Full arc: create_site_task submits a Job, reconciliation reads
	succeeded=1 and transitions the row to Active."""

	def test_create_to_active(self):
		from kubeport.tasks import site_tasks
		from kubeport.tasks.reconciliation import _reconcile_site_create

		doc = _mock_frappe_site_doc()
		release = _mock_release()
		applied_kinds: list[str] = []

		def _apply(_api, manifest, _ns):
			applied_kinds.append(manifest["kind"])

		with (
			patch.object(site_tasks, "_site_operation_matches", return_value=True),
			patch.object(site_tasks, "frappe") as mock_frappe,
			patch.object(site_tasks, "get_k8s_api_client") as mock_get_client,
			patch.object(site_tasks, "_select_site_discovery_pod"),
			patch.object(site_tasks, "_clone_reference_pod_spec", return_value=_mock_ref_spec()),
			patch.object(site_tasks, "apply_resource", side_effect=_apply),
			patch.object(site_tasks, "client") as mock_client,
		):
			mock_frappe.get_doc.side_effect = lambda dt, name: doc if dt == "Frappe Site" else release
			mock_get_client.return_value = MagicMock()
			batch_api = MagicMock()
			batch_api.read_namespaced_job.return_value = SimpleNamespace(
				metadata=SimpleNamespace(uid="job-uid-1")
			)
			mock_client.BatchV1Api.return_value = batch_api
			mock_client.CoreV1Api.return_value = MagicMock()

			site_tasks.create_site_task("release-a/demo.example.com", "tok-aaa")

		# Task applied Secret + Job + Secret-with-ownerRef
		self.assertEqual(applied_kinds, ["Secret", "Job", "Secret"])

		# Extract the job_name recorded on the doc
		set_calls = {call.args[0]: call.args[1] for call in doc.db_set.call_args_list}
		recorded_job = set_calls.get("operation_job_name")
		self.assertTrue(recorded_job)

		# Now simulate reconciliation seeing succeeded=1
		site_row = _reconcile_site_row(
			operation_job_name=recorded_job,
			operation_job_token="tok-aaa",
		)

		with (
			patch("kubeport.tasks.reconciliation.frappe.db.get_value") as mock_get_val,
			patch("kubeport.tasks.reconciliation.frappe.db.set_value") as mock_set_val,
			patch("kubeport.tasks.reconciliation.frappe.publish_realtime"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
			mock_get_val.return_value = {"operation_token": "tok-aaa", "status": "In Progress"}

			batch_v1 = MagicMock()
			batch_v1.read_namespaced_job.return_value = _k8s_job(succeeded=1, name=recorded_job)
			core_v1 = MagicMock()

			_reconcile_site_create(site_row, batch_v1, core_v1)

		mock_set_val.assert_any_call(
			"Frappe Site",
			"release-a/demo.example.com",
			{"status": "Active", "status_detail": ""},
		)


# ---------------------------------------------------------------------------
# Scenario 2: Create → cancel mid-flight → orphan Job cleaned up
# ---------------------------------------------------------------------------


class TestCreateCancelMidFlight(UnitTestCase):
	"""Token is rotated (simulating cancel) between the first guard check
	and the post-apply re-check.  The task must tear down the orphan Job."""

	def test_cancel_mid_flight_deletes_orphan_job(self):
		from kubeport.tasks import site_tasks

		doc = _mock_frappe_site_doc()
		release = _mock_release()

		# True on entry, False on post-apply re-check
		match_seq = iter([True, False])
		deleted_jobs: list[str] = []
		deleted_secrets: list[str] = []

		with (
			patch.object(site_tasks, "_site_operation_matches", side_effect=lambda *a, **kw: next(match_seq)),
			patch.object(site_tasks, "frappe") as mock_frappe,
			patch.object(site_tasks, "get_k8s_api_client"),
			patch.object(site_tasks, "_select_site_discovery_pod"),
			patch.object(site_tasks, "_clone_reference_pod_spec", return_value=_mock_ref_spec()),
			patch.object(site_tasks, "apply_resource"),
			patch.object(
				site_tasks, "_best_effort_delete_job", side_effect=lambda _a, n, _ns: deleted_jobs.append(n)
			),
			patch.object(
				site_tasks,
				"_best_effort_delete_secret",
				side_effect=lambda _a, n, _ns: deleted_secrets.append(n),
			),
			patch.object(site_tasks, "client") as mock_client,
		):
			mock_frappe.get_doc.side_effect = lambda dt, name: doc if dt == "Frappe Site" else release
			batch_api = MagicMock()
			batch_api.read_namespaced_job.return_value = SimpleNamespace(
				metadata=SimpleNamespace(uid="uid-1")
			)
			mock_client.BatchV1Api.return_value = batch_api
			mock_client.CoreV1Api.return_value = MagicMock()

			site_tasks.create_site_task("release-a/demo.example.com", "tok-aaa")

		self.assertEqual(len(deleted_jobs), 1, "Orphan Job must be deleted")
		self.assertEqual(len(deleted_secrets), 1, "Orphan creds Secret must be deleted")

		# The superseded worker must NOT record job bookkeeping on the doc.
		set_fields = {call.args[0] for call in doc.db_set.call_args_list}
		self.assertNotIn("operation_job_name", set_fields)


# ---------------------------------------------------------------------------
# Scenario 3: Create fails → delete → row removed
# ---------------------------------------------------------------------------


class TestCreateFailDeleteRowRemoved(UnitTestCase):
	"""Create fails (Job failed + probe missing), then delete_site runs
	a drop-site Job, reconciliation confirms missing → row is deleted."""

	def test_fail_then_delete_removes_row(self):
		from kubeport.tasks.reconciliation import (
			_reconcile_site_create,
			_reconcile_site_delete,
		)

		site = _reconcile_site_row()

		# Phase 1: reconcile creation failure
		with (
			patch("kubeport.tasks.reconciliation.frappe.db.get_value") as mock_get_val,
			patch("kubeport.tasks.reconciliation.frappe.db.set_value") as mock_set_val,
			patch("kubeport.tasks.reconciliation.frappe.publish_realtime"),
			patch("kubeport.tasks.reconciliation.frappe.log_error"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
			patch("kubeport.tasks.reconciliation._probe_site_state", return_value="missing"),
			patch("kubeport.tasks.reconciliation._extract_job_failure_detail", return_value="DB error"),
		):
			mock_get_val.return_value = {"operation_token": "tok-aaa", "status": "In Progress"}

			batch_v1 = MagicMock()
			batch_v1.read_namespaced_job.return_value = _k8s_job(failed=1)
			core_v1 = MagicMock()

			_reconcile_site_create(site, batch_v1, core_v1)

		mock_set_val.assert_any_call(
			"Frappe Site",
			site.name,
			{"status": "Failed", "status_detail": "DB error"},
		)

		# Phase 2: simulate delete_site reconciliation — site confirmed missing
		delete_site = _reconcile_site_row(
			status="Deleting",
			operation_job_token="tok-bbb",
		)

		with (
			patch("kubeport.tasks.reconciliation.frappe.db.get_value") as mock_get_val,
			patch("kubeport.tasks.reconciliation.frappe.db.set_value"),
			patch("kubeport.tasks.reconciliation.frappe.publish_realtime"),
			patch("kubeport.tasks.reconciliation.frappe.delete_doc") as mock_delete_doc,
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
			patch("kubeport.tasks.reconciliation._probe_site_state", return_value="missing"),
		):
			mock_get_val.return_value = {"operation_token": "tok-bbb", "status": "Deleting"}

			batch_v1 = MagicMock()
			batch_v1.read_namespaced_job.return_value = _k8s_job(succeeded=1)
			core_v1 = MagicMock()

			_reconcile_site_delete(delete_site, batch_v1, core_v1)

		mock_delete_doc.assert_called_once_with(
			"Frappe Site",
			site.name,
			ignore_permissions=True,
			force=True,
			delete_permanently=True,
		)


# ---------------------------------------------------------------------------
# Scenario 4: Migrate fails → probe exists → recover to Active
# ---------------------------------------------------------------------------


class TestMigrateFailRecoverActive(UnitTestCase):
	"""bench migrate exits non-zero but the site is still functional.
	Reconciliation should recover the row to Active, not mark it Failed."""

	def test_migrate_false_negative_recovers(self):
		from kubeport.tasks.reconciliation import _reconcile_site_migrate

		site = _reconcile_site_row(status="Migrating")

		with (
			patch("kubeport.tasks.reconciliation.frappe.db.get_value") as mock_get_val,
			patch("kubeport.tasks.reconciliation.frappe.db.set_value") as mock_set_val,
			patch("kubeport.tasks.reconciliation.frappe.publish_realtime"),
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
			patch("kubeport.tasks.reconciliation._probe_site_state", return_value="exists"),
		):
			mock_get_val.return_value = {"operation_token": "tok-aaa", "status": "Migrating"}

			batch_v1 = MagicMock()
			batch_v1.read_namespaced_job.return_value = _k8s_job(failed=1)
			core_v1 = MagicMock()

			_reconcile_site_migrate(site, batch_v1, core_v1)

		mock_set_val.assert_any_call(
			"Frappe Site",
			site.name,
			{"status": "Active", "status_detail": ""},
		)


# ---------------------------------------------------------------------------
# Scenario 5: Concurrent supersession — stale worker exits cleanly
# ---------------------------------------------------------------------------


class TestConcurrentSupersession(UnitTestCase):
	"""Two create_site calls race: the first one's worker finds a rotated
	token on the pre-flight check and exits without touching K8s."""

	def test_stale_worker_exits_without_side_effects(self):
		from kubeport.tasks import site_tasks

		with (
			patch.object(site_tasks, "_site_operation_matches", return_value=False),
			patch.object(site_tasks, "frappe") as mock_frappe,
			patch.object(site_tasks, "get_k8s_api_client") as mock_k8s,
		):
			site_tasks.create_site_task("release-a/demo.example.com", "stale-token")

		# Must not attempt any K8s or doc interaction
		mock_frappe.get_doc.assert_not_called()
		mock_k8s.assert_not_called()

	def test_stale_reconciliation_skips_finalize(self):
		"""A reconciliation tick holding an old operation_job_token must not
		overwrite a concurrent operation's status."""
		from kubeport.tasks.reconciliation import _reconcile_site_create

		site = _reconcile_site_row(operation_job_token="old-token")

		with (
			patch("kubeport.tasks.reconciliation.frappe.db.get_value") as mock_get_val,
			patch("kubeport.tasks.reconciliation.frappe.db.set_value") as mock_set_val,
			patch("kubeport.tasks.reconciliation.frappe.publish_realtime") as mock_publish,
			patch("kubeport.tasks.reconciliation._job_belongs_to_site", return_value=True),
		):
			# The doc now has a fresh token from a new operation
			mock_get_val.return_value = {"operation_token": "fresh-token", "status": "In Progress"}

			batch_v1 = MagicMock()
			batch_v1.read_namespaced_job.return_value = _k8s_job(succeeded=1)
			core_v1 = MagicMock()

			_reconcile_site_create(site, batch_v1, core_v1)

		# No status writes or realtime events from the stale reconciler
		mock_set_val.assert_not_called()
		mock_publish.assert_not_called()
