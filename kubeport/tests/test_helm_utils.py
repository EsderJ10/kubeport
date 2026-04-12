# Copyright (c) 2026, Los Favs and Contributors
# See license.txt

import base64
from types import SimpleNamespace

import yaml
from frappe.tests import UnitTestCase

from kubeport.utils.helm import _build_kubeconfig_from_token


class UnitTestHelmUtils(UnitTestCase):
	def test_build_kubeconfig_from_token_base64_encodes_ca_certificate(self):
		cluster_doc = SimpleNamespace(
			name="cluster-a",
			api_server_url="https://cluster.example",
			ca_certificate="-----BEGIN CERTIFICATE-----\nPEM\n-----END CERTIFICATE-----",
			get_password=lambda fieldname: "token-123",
		)

		kubeconfig = yaml.safe_load(_build_kubeconfig_from_token(cluster_doc))

		self.assertEqual(
			kubeconfig["clusters"][0]["cluster"]["certificate-authority-data"],
			base64.b64encode(cluster_doc.ca_certificate.encode("utf-8")).decode("ascii"),
		)
		self.assertEqual(kubeconfig["users"][0]["user"]["token"], "token-123")
