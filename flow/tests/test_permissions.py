# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from typing import cast

import frappe
from frappe.tests import IntegrationTestCase

from flow.flow.doctype.flow_run.flow_run import FlowRun
from flow.flow.doctype.flow_session.flow_session import FlowSession

DESK_USER = "flow-perm-desk@example.com"
OTHER_DESK_USER = "flow-perm-other@example.com"
WEBSITE_USER = "flow-perm-website@example.com"


def _user(email: str, *roles: str) -> str:
	"""A user holding `roles`: Frappe makes it a System User (and so a Desk User) when any of
	them has desk access, and a Website User otherwise."""
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"roles": [{"role": role} for role in roles],
			"send_welcome_email": 0,
			"enabled": 1,
		}
	).insert(ignore_permissions=True)
	return email


class TestDeskUserAccess(IntegrationTestCase):
	"""The chat panel is a desk feature: desk users read agents and models and keep their own
	sessions and runs, and website users (who also hold the All role) get nothing."""

	def setUp(self):
		self.desk = _user(DESK_USER, "Report Manager")
		self.other_desk = _user(OTHER_DESK_USER, "Report Manager")
		self.website = _user(WEBSITE_USER)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_users_are_typed_by_their_roles(self):
		self.assertIn("Desk User", frappe.get_roles(self.desk))
		self.assertNotIn("Desk User", frappe.get_roles(self.website))
		self.assertIn("All", frappe.get_roles(self.website))

	def test_desk_user_reads_agents_and_models(self):
		for doctype in ("Flow Agent", "Flow Model"):
			self.assertTrue(frappe.has_permission(doctype, "read", user=self.desk), doctype)
			self.assertFalse(frappe.has_permission(doctype, "write", user=self.desk), doctype)

	def test_desk_user_manages_only_own_sessions(self):
		frappe.set_user(self.desk)
		session = cast(FlowSession, frappe.get_doc({"doctype": "Flow Session", "title": "mine"})).insert()

		for ptype in ("read", "write", "delete"):
			self.assertTrue(frappe.has_permission(session, ptype, user=self.desk), ptype)
			self.assertFalse(frappe.has_permission(session, ptype, user=self.other_desk), ptype)

	def test_desk_user_reads_only_own_runs(self):
		frappe.set_user(self.desk)
		session = cast(FlowSession, frappe.get_doc({"doctype": "Flow Session", "title": "mine"})).insert()
		run = cast(
			FlowRun,
			frappe.get_doc(
				{"doctype": "Flow Run", "source": "Manual", "status": "Running", "session": session.name}
			),
		).insert(ignore_permissions=True)

		self.assertTrue(frappe.has_permission(run, "read", user=self.desk))
		self.assertFalse(frappe.has_permission(run, "write", user=self.desk))
		self.assertFalse(frappe.has_permission(run, "read", user=self.other_desk))

	def test_website_user_has_no_access(self):
		for doctype in ("Flow Agent", "Flow Model", "Flow Session", "Flow Run"):
			self.assertFalse(frappe.has_permission(doctype, "read", user=self.website), doctype)
		self.assertFalse(frappe.has_permission("Flow Session", "create", user=self.website))
