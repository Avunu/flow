# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from flow.boot import boot_session
from flow.knowledge.extract import FILE_EXTENSIONS


class TestBootSession(IntegrationTestCase):
	def test_publishes_the_extractable_file_types_sorted(self):
		bootinfo = _dict()

		boot_session(bootinfo)

		self.assertEqual(bootinfo.flow_supported_file_types, sorted(FILE_EXTENSIONS))
		self.assertTrue(bootinfo.flow_supported_file_types)

	def test_is_registered_as_an_extend_bootinfo_hook(self):
		self.assertIn("flow.boot.boot_session", frappe.get_hooks("extend_bootinfo", app_name="flow"))
