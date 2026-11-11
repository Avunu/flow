# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from flow.utils.errors import throw


class TestThrow(IntegrationTestCase):
	def test_raises_a_validation_error_with_the_message(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Nothing to do"):
			throw("Nothing to do", title="Idle")

	def test_raises_the_given_exception_class(self):
		with self.assertRaises(frappe.PermissionError):
			throw("Not allowed", frappe.PermissionError)

	def test_never_returns_even_when_frappe_throw_does(self):
		"""The framework annotates frappe.throw as returning None; throw guarantees the raise."""
		with patch.object(frappe, "throw", return_value=None), self.assertRaises(frappe.DoesNotExistError):
			throw("Missing", frappe.DoesNotExistError)
