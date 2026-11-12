# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from unittest.mock import patch

import frappe
from frappe.core.doctype.user.user import User
from frappe.tests import IntegrationTestCase

from flow.patches import grant_flow_role
from flow.permissions import FLOW_USER_ROLE, ensure_flow_role

DESK = "grant-flow-desk@example.com"
DISABLED = "grant-flow-disabled@example.com"
WEBSITE = "grant-flow-website@example.com"
PROFILED = "grant-flow-profiled@example.com"
HOLDER = "grant-flow-holder@example.com"
DRIFTED = "grant-flow-drifted@example.com"
BROKEN = "grant-flow-broken@example.com"


def _user(email: str, *roles: str, enabled: int = 1, **values) -> str:
	frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": email.split("@")[0],
			"roles": [{"role": role} for role in roles],
			"send_welcome_email": 0,
			"enabled": enabled,
			**values,
		}
	).insert(ignore_permissions=True)
	return email


def _holds(user: str) -> bool:
	frappe.clear_cache(user=user)
	return FLOW_USER_ROLE in frappe.get_roles(user)


class TestGrantFlowRole(IntegrationTestCase):
	"""Flow was open to every Desk User; the patch keeps today's users on it under the new role."""

	def setUp(self):
		ensure_flow_role()
		self.addCleanup(frappe.db.rollback)

	def test_enabled_system_users_get_the_role(self):
		desk = _user(DESK, "Report Manager")

		grant_flow_role.execute()

		self.assertTrue(_holds(desk))

	def test_disabled_and_website_users_do_not(self):
		disabled = _user(DISABLED, "Report Manager", enabled=0)
		website = _user(WEBSITE)

		grant_flow_role.execute()

		self.assertFalse(_holds(disabled))
		self.assertFalse(_holds(website))

	def test_the_role_is_created_when_missing(self):
		frappe.delete_doc("Role", FLOW_USER_ROLE, force=True)

		grant_flow_role.execute()

		self.assertTrue(frappe.db.exists("Role", FLOW_USER_ROLE))

	def test_a_second_run_changes_nothing(self):
		desk = _user(DESK, "Report Manager")
		grant_flow_role.execute()
		modified = frappe.db.get_value("User", desk, "modified")

		grant_flow_role.execute()

		self.assertEqual(frappe.db.get_value("User", desk, "modified"), modified)
		self.assertEqual(frappe.db.count("Has Role", {"parent": desk, "role": FLOW_USER_ROLE}), 1)

	def test_a_user_who_already_holds_the_role_is_skipped(self):
		holder = _user(HOLDER, FLOW_USER_ROLE)
		modified = frappe.db.get_value("User", holder, "modified")

		grant_flow_role.execute()

		self.assertEqual(frappe.db.get_value("User", holder, "modified"), modified)

	def test_a_user_managed_by_role_profiles_keeps_the_role_after_a_save(self):
		frappe.get_doc(
			{
				"doctype": "Role Profile",
				"role_profile": "Grant Flow Reporters",
				"roles": [{"role": "Report Manager"}],
			}
		).insert(ignore_permissions=True)
		profiled = _user(PROFILED, role_profiles=[{"role_profile": "Grant Flow Reporters"}])

		grant_flow_role.execute()
		frappe.get_doc("User", profiled).save()

		self.assertTrue(_holds(profiled))
		self.assertEqual(
			frappe.get_all("Has Role", filters={"parent": "Grant Flow Reporters"}, pluck="role"),
			["Report Manager"],
		)

	def test_a_clashing_role_profile_stops_the_patch(self):
		frappe.get_doc(
			{"doctype": "Role Profile", "role_profile": FLOW_USER_ROLE, "roles": [{"role": "Report Manager"}]}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Role Profile",
				"role_profile": "Grant Flow Reporters",
				"roles": [{"role": "Report Manager"}],
			}
		).insert(ignore_permissions=True)
		_user(PROFILED, role_profiles=[{"role_profile": "Grant Flow Reporters"}])

		with self.assertRaisesRegex(frappe.ValidationError, "already exists with other roles"):
			grant_flow_role.execute()

	def _reporters_profile(self):
		frappe.get_doc(
			{
				"doctype": "Role Profile",
				"role_profile": "Grant Flow Reporters",
				"roles": [{"role": "Report Manager"}],
			}
		).insert(ignore_permissions=True)

	def test_a_profiled_user_keeps_roles_outside_their_profiles(self):
		self._reporters_profile()
		frappe.get_doc({"doctype": "Role", "role_name": "Grant Flow Extra"}).insert(ignore_permissions=True)
		drifted = _user(DRIFTED, role_profiles=[{"role_profile": "Grant Flow Reporters"}])
		# a role the profile does not grant, as on sites where profiles used to only prefill roles
		frappe.get_doc(
			{
				"doctype": "Has Role",
				"parent": drifted,
				"parenttype": "User",
				"parentfield": "roles",
				"role": "Grant Flow Extra",
			}
		).insert(ignore_permissions=True)
		self.assertIn("Grant Flow Extra", frappe.get_roles(drifted))

		grant_flow_role.execute()
		frappe.get_doc("User", drifted).save()

		self.assertTrue(
			{"Report Manager", "Grant Flow Extra", FLOW_USER_ROLE} <= set(frappe.get_roles(drifted))
		)

	def test_one_users_failure_is_logged_and_the_rest_are_granted(self):
		broken = _user(BROKEN, "Report Manager")
		desk = _user(DESK, "Report Manager")
		add_roles = User.add_roles

		def add(user, *roles):
			if user.name == broken:
				frappe.throw("bad legacy row")
			return add_roles(user, *roles)

		with patch.object(User, "add_roles", add):
			grant_flow_role.execute()

		self.assertTrue(_holds(desk))
		self.assertFalse(_holds(broken))
		self.assertTrue(frappe.db.exists("Error Log", {"method": f"Flow User role not granted to {broken}"}))
