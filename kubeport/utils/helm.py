"""
Helm CLI Wrapper

Stateless wrapper around the Helm 3 binary.  Every function writes a
temporary kubeconfig file from the cluster document, runs ``helm`` via
``subprocess.run``, and cleans up immediately.

Security invariants:
- All subprocess calls use a **list** of arguments — never ``shell=True``
- Temporary kubeconfig files are created per-call and deleted in a
  ``finally`` block, so concurrent workers never share state
- All long-running operations (install, upgrade, uninstall) are called
  from ``frappe.enqueue`` background jobs — never from the web thread

Output parsing:
- Commands that support ``--output json`` return parsed dicts/lists
- Commands that produce text (e.g. ``helm show values``) return raw strings
"""

import base64
import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from typing import Any

import frappe
import yaml

_HELM_WORKER_TIMEOUT_SECONDS = 600
_HELM_READ_TIMEOUT_SECONDS = 30


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def repo_add(
	name: str,
	url: str,
	username: str | None = None,
	password: str | None = None,
) -> str:
	"""Register a Helm chart repository.

	Equivalent to ``helm repo add <name> <url> [--username ...] [--password ...]``.
	"""
	cmd = ["helm", "repo", "add", name, url, "--force-update"]
	if username:
		cmd.extend(["--username", username])
	if password:
		cmd.extend(["--password", password])

	return _run_helm(cmd)


def repo_update(name: str | None = None) -> str:
	"""Refresh cached chart index for one or all repositories.

	Equivalent to ``helm repo update [name]``.
	"""
	cmd = ["helm", "repo", "update"]
	if name:
		cmd.append(name)
	return _run_helm(cmd)


def repo_remove(name: str) -> str:
	"""Unregister a Helm chart repository.

	Equivalent to ``helm repo remove <name>``.
	"""
	return _run_helm(["helm", "repo", "remove", name])


def search_repo(repo_name: str, keyword: str = "") -> list[dict]:
	"""List charts in a repository.

	Equivalent to ``helm search repo <repo_name>/ --output json``.
	Returns a list of chart dicts with keys: name, version, app_version, description.
	"""
	return search_repo_with_options(repo_name, keyword=keyword)


def search_repo_with_options(
	repo_name: str,
	keyword: str = "",
	all_versions: bool = False,
	timeout: int = _HELM_READ_TIMEOUT_SECONDS,
) -> list[dict]:
	"""List charts in a repository with optional full version history."""
	search_term = f"{repo_name}/"
	if keyword:
		search_term = f"{repo_name}/{keyword}"

	cmd = ["helm", "search", "repo", search_term, "--output", "json"]
	if all_versions:
		cmd.append("--versions")
	output = _run_helm(cmd, timeout=timeout)
	return _parse_json_or_empty(output)


def show_chart(
	chart_ref: str,
	version: str | None = None,
	timeout: int = _HELM_READ_TIMEOUT_SECONDS,
) -> dict:
	"""Get chart metadata (Chart.yaml content).

	Equivalent to ``helm show chart <ref> [--version <v>]``.
	"""
	cmd = ["helm", "show", "chart", chart_ref]
	if version:
		cmd.extend(["--version", version])
	output = _run_helm(cmd, timeout=timeout)
	return yaml.safe_load(output) or {}


def show_values(
	chart_ref: str,
	version: str | None = None,
	timeout: int = _HELM_READ_TIMEOUT_SECONDS,
) -> str:
	"""Get default values.yaml for a chart as raw YAML string.

	Equivalent to ``helm show values <ref> [--version <v>]``.
	"""
	cmd = ["helm", "show", "values", chart_ref]
	if version:
		cmd.extend(["--version", version])
	return _run_helm(cmd, timeout=timeout)


def install_or_upgrade(
	release_name: str,
	chart_ref: str,
	namespace: str,
	cluster_name: str,
	values_yaml: str | None = None,
	chart_version: str | None = None,
) -> dict:
	"""Install or upgrade a Helm release (idempotent).

	Equivalent to ``helm upgrade --install <release> <chart> -n <ns>``.
	Uses a temporary kubeconfig from the cluster document.

	Returns the parsed JSON output from ``helm status``.
	"""
	with _helm_kubeconfig(cluster_name) as kubeconfig_path:
		cmd = [
			"helm", "upgrade", "--install",
			release_name, chart_ref,
			"--namespace", namespace,
			"--create-namespace",
			"--output", "json",
		]

		if chart_version:
			cmd.extend(["--version", chart_version])

		if kubeconfig_path:
			cmd.extend(["--kubeconfig", kubeconfig_path])

		# Write values to a temp file if provided
		if values_yaml:
			values_fd, values_path = tempfile.mkstemp(
				suffix=".yaml", prefix="kubeport_values_"
			)
			try:
				os.write(values_fd, values_yaml.encode("utf-8"))
				os.close(values_fd)
				cmd.extend(["--values", values_path])
				output = _run_helm(cmd, timeout=_HELM_WORKER_TIMEOUT_SECONDS)
			finally:
				if os.path.exists(values_path):
					os.unlink(values_path)
		else:
			output = _run_helm(cmd, timeout=_HELM_WORKER_TIMEOUT_SECONDS)

	return _parse_json_or_empty(output)


def uninstall(
	release_name: str,
	namespace: str,
	cluster_name: str,
) -> str:
	"""Uninstall a Helm release.

	Equivalent to ``helm uninstall <release> -n <ns>``.
	"""
	with _helm_kubeconfig(cluster_name) as kubeconfig_path:
		cmd = [
			"helm", "uninstall", release_name,
			"--namespace", namespace,
		]
		if kubeconfig_path:
			cmd.extend(["--kubeconfig", kubeconfig_path])

		return _run_helm(cmd, timeout=_HELM_WORKER_TIMEOUT_SECONDS)


def status(
	release_name: str,
	namespace: str,
	cluster_name: str,
) -> dict:
	"""Get release status from the cluster.

	Equivalent to ``helm status <release> -n <ns> --output json``.
	"""
	with _helm_kubeconfig(cluster_name) as kubeconfig_path:
		cmd = [
			"helm", "status", release_name,
			"--namespace", namespace,
			"--output", "json",
		]
		if kubeconfig_path:
			cmd.extend(["--kubeconfig", kubeconfig_path])

		output = _run_helm(cmd, timeout=_HELM_READ_TIMEOUT_SECONDS)

	return _parse_json_or_empty(output)


def get_manifest(
	release_name: str,
	namespace: str,
	cluster_name: str,
) -> list[dict]:
	"""Return the rendered multi-document manifest for a release.

	Equivalent to ``helm get manifest <release> -n <ns>``.  The output is a
	multi-document YAML stream of every resource the release owns; we parse it
	into a list of resource dicts so callers can fan out to live readiness
	queries without having to re-parse it themselves.

	The rendered manifest is the only authoritative inventory of a release's
	resources — chart authors are not required to apply
	``app.kubernetes.io/instance=<release>`` labels, so a label-selector
	approach would miss resources from charts that omit the convention.

	Returns an empty list when the release exists but has no resources (e.g.
	values disabled every workload).  Raises through ``frappe.throw`` if the
	release does not exist or helm fails.
	"""
	with _helm_kubeconfig(cluster_name) as kubeconfig_path:
		cmd = [
			"helm", "get", "manifest", release_name,
			"--namespace", namespace,
		]
		if kubeconfig_path:
			cmd.extend(["--kubeconfig", kubeconfig_path])

		output = _run_helm(cmd, timeout=_HELM_READ_TIMEOUT_SECONDS)

	if not output or not output.strip():
		return []

	try:
		documents = list(yaml.safe_load_all(output))
	except yaml.YAMLError as e:
		frappe.throw(f"Helm returned a manifest that could not be parsed: {e}")

	return [doc for doc in documents if isinstance(doc, dict) and doc.get("kind")]


def list_releases(
	namespace: str | None = None,
	cluster_name: str | None = None,
) -> list[dict]:
	"""List all Helm releases.

	Equivalent to ``helm list [--namespace <ns>] --output json``.
	"""
	cmd = ["helm", "list", "--output", "json"]
	if namespace:
		cmd.extend(["--namespace", namespace])
	else:
		cmd.append("--all-namespaces")

	if cluster_name:
		with _helm_kubeconfig(cluster_name) as kubeconfig_path:
			if kubeconfig_path:
				cmd.extend(["--kubeconfig", kubeconfig_path])
			output = _run_helm(cmd, timeout=_HELM_READ_TIMEOUT_SECONDS)
	else:
		output = _run_helm(cmd, timeout=_HELM_READ_TIMEOUT_SECONDS)

	return _parse_json_or_empty(output)


# ---------------------------------------------------------------------------
# Kubeconfig Context Manager
# ---------------------------------------------------------------------------


@contextmanager
def _helm_kubeconfig(cluster_name: str):
	"""Write a temporary kubeconfig for Helm CLI usage.

	Reads the cluster document, writes a temp file, yields the file path,
	and unconditionally deletes it.  For In-Cluster auth, yields ``None``
	(Helm uses the pod's default service account automatically).
	"""
	cluster_doc = frappe.get_doc("Kubernetes Cluster", cluster_name)
	auth_method = cluster_doc.auth_method or "Kubeconfig"

	if auth_method == "In-Cluster":
		# Helm auto-detects in-cluster credentials from the pod environment
		yield None
		return

	if auth_method == "Kubeconfig":
		content = cluster_doc.kubeconfig or ""
		if content and cluster_doc.skip_tls_verify:
			try:
				kc_dict = yaml.safe_load(content)
				for c in kc_dict.get("clusters", []):
					if "cluster" in c:
						c["cluster"]["insecure-skip-tls-verify"] = True
						c["cluster"].pop("certificate-authority-data", None)
						c["cluster"].pop("certificate-authority", None)
				content = yaml.dump(kc_dict, default_flow_style=False)
			except Exception:
				pass
	elif auth_method == "Bearer Token":
		content = _build_kubeconfig_from_token(cluster_doc)
	else:
		frappe.throw(f"Unknown auth method: {auth_method}")

	if not content:
		frappe.throw(
			f"Kubernetes Cluster '{cluster_name}' has no credentials "
			f"configured for auth method '{auth_method}'."
		)

	fd, path = tempfile.mkstemp(suffix=".yaml", prefix="kubeport_helm_kc_")
	try:
		os.write(fd, content.encode("utf-8"))
		os.close(fd)
		yield path
	finally:
		if os.path.exists(path):
			os.unlink(path)


def _build_kubeconfig_from_token(cluster_doc) -> str:
	"""Build a minimal kubeconfig YAML from a Bearer Token cluster doc.

	Creates a single-context kubeconfig that authenticates via token,
	suitable for passing to ``helm --kubeconfig``.
	"""
	token = cluster_doc.get_password("bearer_token")
	if not token:
		frappe.throw(
			f"Kubernetes Cluster '{cluster_doc.name}' has no bearer token configured."
		)

	kubeconfig: dict[str, Any] = {
		"apiVersion": "v1",
		"kind": "Config",
		"current-context": "default",
		"clusters": [{
			"name": "default",
			"cluster": {
				"server": cluster_doc.api_server_url,
			},
		}],
		"contexts": [{
			"name": "default",
			"context": {
				"cluster": "default",
				"user": "default",
			},
		}],
		"users": [{
			"name": "default",
			"user": {
				"token": token,
			},
		}],
	}

	# Keep Helm TLS behavior aligned with the Python Kubernetes client.
	if cluster_doc.skip_tls_verify:
		kubeconfig["clusters"][0]["cluster"]["insecure-skip-tls-verify"] = True
	elif cluster_doc.ca_certificate:
		kubeconfig["clusters"][0]["cluster"]["certificate-authority-data"] = (
			base64.b64encode(cluster_doc.ca_certificate.encode("utf-8")).decode("ascii")
		)
	else:
		frappe.throw(
			"Bearer Token authentication requires a CA certificate unless "
			"'Skip TLS Verification (Development Only)' is enabled."
		)

	return yaml.dump(kubeconfig, default_flow_style=False)


# ---------------------------------------------------------------------------
# Private Helpers
# ---------------------------------------------------------------------------


def _run_helm(cmd: list[str], timeout: int = _HELM_WORKER_TIMEOUT_SECONDS) -> str:
	"""Execute a Helm CLI command and return stdout.

	Args:
		cmd: Command as a list of strings (never use shell=True).

	Returns:
		The stdout text output from the command.

	Raises:
		frappe.ValidationError: If the command fails (non-zero exit code).
	"""
	try:
		result = subprocess.run(
			cmd,
			capture_output=True,
			text=True,
			check=True,
			timeout=timeout,
		)
		return result.stdout
	except FileNotFoundError:
		frappe.throw(
			"Helm CLI binary not found. Please install Helm 3: "
			"https://helm.sh/docs/intro/install/"
		)
	except subprocess.CalledProcessError as e:
		error_msg = e.stderr.strip() if e.stderr else str(e)
		frappe.throw(
			f"Helm command failed: {error_msg}",
			title="Helm Error",
		)
	except subprocess.TimeoutExpired:
		frappe.throw(
			f"Helm command timed out after {timeout} seconds.",
			title="Helm Timeout",
		)

	return ""  # Unreachable but satisfies type checkers


def _parse_json_or_empty(text: str) -> Any:
	"""Parse JSON output from Helm, returning an empty list for empty output."""
	if not text or not text.strip():
		return []
	try:
		return json.loads(text)
	except json.JSONDecodeError:
		frappe.throw("Helm returned invalid JSON output.", title="Helm Error")
