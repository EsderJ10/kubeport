#!/usr/bin/env python3
# Copyright (c) 2026, Los Favs and contributors
# For license information, please see license.txt

"""Rewrite the curated Kubeport site image catalog row for a published image.

Standalone (no Frappe dependency). The validation regexes mirror the doctype
source of truth at
``kubeport/kubeport/doctype/kubeport_site_image/kubeport_site_image.py``;
``kubeport/tests/test_publish_automation.py`` enforces drift detection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

_GHCR_REPOSITORY_RE = re.compile(r"^ghcr\.io/[a-z0-9][a-z0-9._/-]*[a-z0-9]$")
_IMAGE_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FRAPPE_BRANCH_RE = re.compile(r"^version-(\d+)$")


class CatalogUpdateError(RuntimeError):
	"""Raised when the catalog cannot be safely rewritten."""


def _fail(message: str) -> None:
	raise CatalogUpdateError(message)


def parse_frappe_major(branch: str) -> int:
	match = _FRAPPE_BRANCH_RE.fullmatch(branch.strip())
	if not match:
		_fail(f"--frappe-branch must match 'version-<n>'; got '{branch}'.")
	return int(match.group(1))


def derive_image_tag(tag_ref: str, frappe_major: int) -> str:
	semver = tag_ref[1:] if tag_ref.startswith("v") else tag_ref
	return f"{semver}-frappe{frappe_major}"


def compute_apps_json_hash(path: Path) -> str:
	return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def validate_repository(label: str, repository: str) -> None:
	if not _GHCR_REPOSITORY_RE.fullmatch(repository):
		_fail(f"Site image '{label}' must use a public GHCR repository such as 'ghcr.io/owner/image'.")
	if ":" in repository or "@" in repository:
		_fail(
			f"Site image '{label}' repository must not include a tag or digest; "
			"use the Tag and Digest fields instead."
		)


def validate_tag(label: str, tag: str) -> None:
	if not _IMAGE_TAG_RE.fullmatch(tag):
		_fail(f"Site image '{label}' has invalid image tag '{tag}'.")


def validate_digest(label: str, digest: str) -> None:
	if not _SHA256_DIGEST_RE.fullmatch(digest):
		_fail(
			f"Site image '{label}' digest must be a sha256 digest in the form "
			"'sha256:<64 lowercase hex characters>'."
		)


def find_catalog_row(payload: dict, image_repository: str, frappe_major: int) -> dict:
	images = payload.get("images")
	if not isinstance(images, list):
		_fail("Catalog must contain an 'images' list.")

	matches = [
		image
		for image in images
		if isinstance(image, dict)
		and str(image.get("image_repository", "")).strip() == image_repository
		and int(image.get("frappe_major") or 0) == frappe_major
	]
	if not matches:
		_fail(
			f"No catalog row matches image_repository='{image_repository}' and frappe_major={frappe_major}."
		)
	if len(matches) > 1:
		_fail(
			f"Multiple catalog rows match image_repository='{image_repository}' "
			f"and frappe_major={frappe_major}; refusing to guess."
		)
	return matches[0]


def emit_outputs(values: dict[str, str]) -> None:
	output_path = os.environ.get("GITHUB_OUTPUT")
	if not output_path:
		return
	with open(output_path, "a", encoding="utf-8") as handle:
		for key, value in values.items():
			handle.write(f"{key}={value}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--catalog", required=True, type=Path)
	parser.add_argument("--apps-json", required=True, type=Path)
	parser.add_argument("--image-name", required=True)
	parser.add_argument("--frappe-branch", required=True)
	parser.add_argument("--image-digest", required=True)
	parser.add_argument("--git-sha", required=True)
	parser.add_argument("--tag-ref", required=True)
	return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, str]:
	image_repository = args.image_name.strip().lower()
	frappe_major = parse_frappe_major(args.frappe_branch)
	tag_ref = args.tag_ref.strip()
	image_tag = derive_image_tag(tag_ref, frappe_major)
	image_digest = args.image_digest.strip().lower()
	git_sha = args.git_sha.strip()
	apps_json_hash = compute_apps_json_hash(args.apps_json)

	label = f"{image_repository}:{image_tag}"
	validate_repository(label, image_repository)
	validate_tag(label, image_tag)
	validate_digest(label, image_digest)
	if not git_sha:
		_fail("--git-sha must not be empty.")

	payload = json.loads(args.catalog.read_text(encoding="utf-8"))
	row = find_catalog_row(payload, image_repository, frappe_major)

	before = {
		"image_tag": str(row.get("image_tag", "")),
		"image_digest": str(row.get("image_digest", "")),
		"source_revision": str(row.get("source_revision", "")),
		"apps_json_hash": str(row.get("apps_json_hash", "")),
	}

	row["image_tag"] = image_tag
	row["image_digest"] = image_digest
	row["source_revision"] = git_sha
	row["apps_json_hash"] = apps_json_hash

	args.catalog.write_text(
		json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
		encoding="utf-8",
	)

	short_digest = image_digest.removeprefix("sha256:")[:12]
	return {
		"matched_repository": image_repository,
		"frappe_major": str(frappe_major),
		"image_tag": image_tag,
		"image_digest": image_digest,
		"short_digest": short_digest,
		"source_revision": git_sha,
		"apps_json_hash": apps_json_hash,
		"before_image_tag": before["image_tag"],
		"before_image_digest": before["image_digest"],
		"before_source_revision": before["source_revision"],
		"before_apps_json_hash": before["apps_json_hash"],
	}


def main(argv: list[str] | None = None) -> int:
	args = parse_args(argv)
	try:
		result = run(args)
	except CatalogUpdateError as exc:
		print(f"ERROR: {exc}", file=sys.stderr)
		return 1

	print("Catalog row updated:")
	print(f"  repository:      {result['matched_repository']}")
	print(f"  frappe_major:    {result['frappe_major']}")
	print(f"  image_tag:       {result['before_image_tag']} -> {result['image_tag']}")
	print(f"  image_digest:    {result['before_image_digest']} -> {result['image_digest']}")
	print(f"  source_revision: {result['before_source_revision']} -> {result['source_revision']}")
	print(f"  apps_json_hash:  {result['before_apps_json_hash']} -> {result['apps_json_hash']}")

	emit_outputs(
		{
			"matched_repository": result["matched_repository"],
			"frappe_major": result["frappe_major"],
			"image_tag": result["image_tag"],
			"image_digest": result["image_digest"],
			"short_digest": result["short_digest"],
			"source_revision": result["source_revision"],
			"apps_json_hash": result["apps_json_hash"],
		}
	)
	return 0


if __name__ == "__main__":
	sys.exit(main())
