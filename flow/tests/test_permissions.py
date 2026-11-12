# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from typing import cast
from unittest.mock import patch

import frappe
from frappe import share
from frappe.core.doctype.user.user import User
from frappe.tests import IntegrationTestCase

from flow.api import start_run, stop_run
from flow.flow.doctype.flow_agent.flow_agent import FlowAgent
from flow.flow.doctype.flow_agent_memory.flow_agent_memory import FlowAgentMemory
from flow.flow.doctype.flow_model.flow_model import FlowModel
from flow.flow.doctype.flow_run.flow_run import FlowRun
from flow.flow.doctype.flow_session.flow_session import FlowSession
from flow.lib.model import ChatResponse, Model
from flow.lib.session import new_session
from flow.permissions import FLOW_USER_ROLE, ensure_flow_role
from flow.tools.builtins import sync_builtin_tools

FLOW_USER = "flow-perm-user@example.com"
OTHER_FLOW_USER = "flow-perm-other@example.com"
DESK_USER = "flow-perm-desk@example.com"
MANAGER = "flow-perm-manager@example.com"


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


def _session(owner: str, *, source: str = "Manual") -> FlowSession:
	doc = cast(
		FlowSession, frappe.get_doc({"doctype": "Flow Session", "title": "s", "source": source})
	).insert(ignore_permissions=True)
	frappe.db.set_value("Flow Session", str(doc.name), "owner", owner)
	return cast(FlowSession, frappe.get_doc("Flow Session", str(doc.name)))


def _run(session: FlowSession, *, source: str = "Manual", status: str = "Running") -> FlowRun:
	doc = cast(
		FlowRun,
		frappe.get_doc({"doctype": "Flow Run", "source": source, "status": status, "session": session.name}),
	).insert(ignore_permissions=True)
	frappe.db.set_value("Flow Run", str(doc.name), "owner", session.owner)
	return cast(FlowRun, frappe.get_doc("Flow Run", str(doc.name)))


def _listed(doctype: str, user: str) -> set[str]:
	return set(frappe.get_list(doctype, pluck="name", user=user, limit=0))


class FlowPermissionCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		sync_builtin_tools()

	def setUp(self):
		ensure_flow_role()
		self.flow_user = _user(FLOW_USER, FLOW_USER_ROLE)
		self.other_flow_user = _user(OTHER_FLOW_USER, FLOW_USER_ROLE)
		self.desk = _user(DESK_USER, "Report Manager")
		self.manager = _user(MANAGER, "System Manager")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()


class TestFlowUserRole(FlowPermissionCase):
	"""Flow is for Flow Users; being a Desk User is no longer enough."""

	def test_the_role_exists_with_desk_access_and_ensuring_it_again_is_harmless(self):
		frappe.delete_doc("Role", FLOW_USER_ROLE, force=True)
		ensure_flow_role()
		ensure_flow_role()
		self.assertEqual(frappe.db.get_value("Role", FLOW_USER_ROLE, "desk_access"), 1)

	def test_flow_user_reads_agents_and_models_but_cannot_change_them(self):
		for doctype in ("Flow Agent", "Flow Model"):
			self.assertTrue(frappe.has_permission(doctype, "read", user=self.flow_user), doctype)
			self.assertFalse(frappe.has_permission(doctype, "write", user=self.flow_user), doctype)

	def test_a_desk_user_without_the_role_has_no_access(self):
		self.assertIn("Desk User", frappe.get_roles(self.desk))
		for doctype in ("Flow Agent", "Flow Model", "Flow Session", "Flow Run"):
			self.assertFalse(frappe.has_permission(doctype, "read", user=self.desk), doctype)
		self.assertFalse(frappe.has_permission("Flow Session", "create", user=self.desk))

	def test_flow_user_creates_and_manages_only_own_sessions(self):
		frappe.set_user(self.flow_user)
		session = cast(FlowSession, frappe.get_doc({"doctype": "Flow Session", "title": "mine"})).insert()

		for ptype in ("read", "write", "delete"):
			self.assertTrue(frappe.has_permission(session, ptype, user=self.flow_user), ptype)
			self.assertFalse(frappe.has_permission(session, ptype, user=self.other_flow_user), ptype)

	def test_flow_user_reads_only_own_runs(self):
		session = _session(self.flow_user)
		run = _run(session)

		self.assertTrue(frappe.has_permission(run, "read", user=self.flow_user))
		self.assertFalse(frappe.has_permission(run, "write", user=self.flow_user))
		self.assertFalse(frappe.has_permission(run, "read", user=self.other_flow_user))


class TestPrivateByOwner(FlowPermissionCase):
	"""Sessions, runs and personal memories are private to their owner, System Managers included."""

	def test_a_system_manager_cannot_open_anothers_manual_session_or_run(self):
		session = _session(self.flow_user)
		run = _run(session)

		for ptype in ("read", "write", "delete"):
			self.assertFalse(frappe.has_permission(session, ptype, user=self.manager), ptype)
		self.assertFalse(frappe.has_permission(run, "read", user=self.manager))

	def test_a_system_manager_sees_trigger_sessions_and_runs(self):
		session = _session(self.flow_user, source="Trigger")
		run = _run(session, source="Trigger")

		self.assertTrue(frappe.has_permission(session, "read", user=self.manager))
		self.assertTrue(frappe.has_permission(run, "read", user=self.manager))
		self.assertFalse(frappe.has_permission(session, "read", user=self.other_flow_user))

	def test_lists_show_the_same_records_as_single_checks(self):
		mine = _session(self.flow_user)
		theirs = _session(self.other_flow_user)
		trigger = _session(self.other_flow_user, source="Trigger")

		self.assertEqual(_listed("Flow Session", self.flow_user), {mine.name})
		self.assertEqual(_listed("Flow Session", self.manager), {trigger.name})
		everything = {mine.name, theirs.name, trigger.name}
		self.assertTrue(everything <= _listed("Flow Session", "Administrator"))

	def test_run_lists_follow_the_owner_and_trigger_rules(self):
		mine = _run(_session(self.flow_user))
		trigger = _run(_session(self.other_flow_user, source="Trigger"), source="Trigger")

		self.assertEqual(_listed("Flow Run", self.flow_user), {mine.name})
		self.assertEqual(_listed("Flow Run", self.manager), {trigger.name})

	def _memory(self, agent: str, scope: str, user: str | None) -> FlowAgentMemory:
		return cast(
			FlowAgentMemory,
			frappe.get_doc(
				{
					"doctype": "Flow Agent Memory",
					"agent": agent,
					"scope": scope,
					"user": user,
					"content": f"{scope} note",
				}
			),
		).insert(ignore_permissions=True)

	def test_a_personal_memory_is_private_to_its_user(self):
		model = cast(
			FlowModel,
			frappe.get_doc(
				{
					"doctype": "Flow Model",
					"title": "Mem Model",
					"model_id": "openai/gpt-4o-mini",
					"enabled": 1,
				}
			),
		).insert()
		agent = cast(
			FlowAgent,
			frappe.get_doc(
				{
					"doctype": "Flow Agent",
					"title": "Mem Agent",
					"model": model.name,
					"instructions": "x",
					"enabled": 1,
				}
			),
		).insert()
		other_manager = _user("flow-perm-manager-2@example.com", "System Manager")
		personal = self._memory(str(agent.name), "User", self.manager)
		shared = self._memory(str(agent.name), "Agent", None)

		self.assertTrue(frappe.has_permission(personal, "read", user=self.manager))
		self.assertFalse(frappe.has_permission(personal, "read", user=other_manager))
		self.assertTrue(frappe.has_permission(shared, "read", user=other_manager))
		self.assertEqual(
			{personal.name, shared.name} & _listed("Flow Agent Memory", other_manager), {shared.name}
		)


class TestAgentAndModelRoles(FlowPermissionCase):
	"""An agent or model with roles is only for users holding one of them."""

	def setUp(self):
		super().setUp()
		self.model = cast(
			FlowModel,
			frappe.get_doc(
				{
					"doctype": "Flow Model",
					"title": "Role Model",
					"model_id": "openai/gpt-4o-mini",
					"enabled": 1,
				}
			),
		).insert()
		self.agent = cast(
			FlowAgent,
			frappe.get_doc(
				{
					"doctype": "Flow Agent",
					"title": "Role Agent",
					"model": self.model.name,
					"instructions": "x",
					"enabled": 1,
				}
			),
		).insert()
		self.reporter = _user("flow-perm-reporter@example.com", FLOW_USER_ROLE, "Report Manager")

	def _restrict(self, doc, role: str = "Report Manager") -> None:
		doc.append("roles", {"role": role})
		doc.save()
		frappe.clear_cache(user=self.flow_user)

	def test_an_agent_without_roles_is_open_to_every_flow_user(self):
		self.assertTrue(frappe.has_permission("Flow Agent", "read", self.agent.name, user=self.flow_user))
		self.assertIn(self.agent.name, _listed("Flow Agent", self.flow_user))

	def test_an_agent_with_roles_is_only_for_users_holding_one(self):
		self._restrict(self.agent)

		self.assertFalse(frappe.has_permission("Flow Agent", "read", self.agent.name, user=self.flow_user))
		self.assertNotIn(self.agent.name, _listed("Flow Agent", self.flow_user))
		self.assertTrue(frappe.has_permission("Flow Agent", "read", self.agent.name, user=self.reporter))
		self.assertIn(self.agent.name, _listed("Flow Agent", self.reporter))
		self.assertTrue(frappe.has_permission("Flow Agent", "read", self.agent.name, user=self.manager))
		self.assertIn(self.agent.name, _listed("Flow Agent", self.manager))

	def test_a_model_with_roles_is_only_for_users_holding_one(self):
		self._restrict(self.model)

		self.assertFalse(frappe.has_permission("Flow Model", "read", self.model.name, user=self.flow_user))
		self.assertNotIn(self.model.name, _listed("Flow Model", self.flow_user))
		self.assertTrue(frappe.has_permission("Flow Model", "read", self.model.name, user=self.reporter))
		self.assertIn(self.model.name, _listed("Flow Model", self.manager))

	def test_roles_on_one_doctype_do_not_restrict_the_other(self):
		self._restrict(self.model)

		self.assertTrue(frappe.has_permission("Flow Agent", "read", self.agent.name, user=self.flow_user))

	def test_a_session_cannot_start_on_an_agent_the_user_may_not_read(self):
		self._restrict(self.agent)
		frappe.set_user(self.flow_user)

		with self.assertRaises(frappe.PermissionError):
			new_session(self.agent.name)

	def test_a_user_kept_from_the_agents_model_must_pick_another(self):
		self._restrict(self.model)
		other = cast(
			FlowModel,
			frappe.get_doc(
				{"doctype": "Flow Model", "title": "Open Model", "model_id": "openai/gpt-4o", "enabled": 1}
			),
		).insert()
		frappe.set_user(self.flow_user)

		with self.assertRaises(frappe.PermissionError):
			self.agent.assemble()
		self.assertIsNotNone(self.agent.assemble(model=other.name))


class TestOutsideTheRole(FlowPermissionCase):
	"""Paths other than a plain list or check: Guests, child tables, odd role names and shares."""

	def test_the_workspace_is_for_flow_users_and_system_managers(self):
		roles = set(
			frappe.get_all("Has Role", filters={"parent": "Flow", "parenttype": "Workspace"}, pluck="role")
		)

		self.assertEqual(roles, {FLOW_USER_ROLE, "System Manager"})

	def test_guests_and_website_users_are_denied_every_list(self):
		website = _user("flow-perm-website@example.com")
		for user in ("Guest", website):
			for doctype in ("Flow Session", "Flow Run", "Flow Agent", "Flow Model", "Flow Agent Memory"):
				with self.assertRaises(frappe.PermissionError, msg=f"{user} {doctype}"):
					frappe.get_list(doctype, user=user)

	def test_a_flow_user_cannot_list_anothers_session_messages(self):
		theirs = _session(self.other_flow_user)
		theirs.append("messages", {"role": "user", "content": "private words"})
		theirs.save(ignore_permissions=True)
		mine = _session(self.flow_user)
		mine.append("messages", {"role": "user", "content": "my words"})
		mine.save(ignore_permissions=True)

		for doctype in ("Flow Session Message", "Flow Session Attachment"):
			rows = frappe.get_list(
				doctype, parent_doctype="Flow Session", fields=["parent"], user=self.flow_user
			)
			self.assertNotIn(theirs.name, {row.parent for row in rows}, doctype)
		rows = frappe.get_list(
			"Flow Session Message", parent_doctype="Flow Session", fields=["content"], user=self.flow_user
		)
		self.assertEqual([row.content for row in rows], ["my words"])

	def test_a_role_name_with_quotes_does_not_break_the_list_conditions(self):
		role = "Flow \\'quoted\" role"
		frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
		user = _user("flow-perm-quoted@example.com", FLOW_USER_ROLE, role)
		model = cast(
			FlowModel,
			frappe.get_doc(
				{
					"doctype": "Flow Model",
					"title": "Quote Model",
					"model_id": "openai/gpt-4o-mini",
					"enabled": 1,
				}
			),
		).insert()
		open_agent = cast(
			FlowAgent,
			frappe.get_doc(
				{
					"doctype": "Flow Agent",
					"title": "Quote Agent",
					"model": model.name,
					"instructions": "x",
					"enabled": 1,
				}
			),
		).insert()

		self.assertIn(open_agent.name, _listed("Flow Agent", user))
		self.assertIn(model.name, _listed("Flow Model", user))

	def test_a_share_lets_a_user_without_the_role_read_that_one_session(self):
		# Frappe turns a controller denial into a grant when a DocShare exists. Only users who
		# hold the share right (System Managers) can create one, and it covers the one record.
		session = _session(self.flow_user)
		other = _session(self.flow_user)
		share.add_docshare(
			"Flow Session", session.name, self.desk, read=1, flags={"ignore_share_permission": True}
		)

		self.assertTrue(frappe.has_permission(session, "read", user=self.desk))
		self.assertFalse(frappe.has_permission(other, "read", user=self.desk))
		self.assertFalse(frappe.has_permission(session, "write", user=self.desk))


class TestApiNeedsTheRole(FlowPermissionCase):
	"""The whitelisted methods re-check the role, so an owner who lost it is locked out."""

	def setUp(self):
		super().setUp()
		self.model = cast(
			FlowModel,
			frappe.get_doc(
				{
					"doctype": "Flow Model",
					"title": "Api Model",
					"model_id": "openai/gpt-4o-mini",
					"enabled": 1,
				}
			),
		).insert()
		self.agent = cast(
			FlowAgent,
			frappe.get_doc(
				{
					"doctype": "Flow Agent",
					"title": "Api Agent",
					"model": self.model.name,
					"instructions": "x",
					"enabled": 1,
				}
			),
		).insert()
		self.reply = ChatResponse(content="ok", tool_calls=[], usage={})

	def test_start_run_works_for_a_flow_user(self):
		frappe.set_user(self.flow_user)
		with patch.object(Model, "chat", return_value=self.reply):
			payload = start_run("hello", agent=self.agent.name)
		self.assertEqual(payload["status"], "Completed")

	def test_start_run_is_refused_without_the_role(self):
		frappe.set_user(self.desk)
		with self.assertRaises(frappe.PermissionError):
			start_run("hello", agent=self.agent.name)

	def test_an_owner_who_lost_the_role_cannot_continue_their_session(self):
		frappe.set_user(self.flow_user)
		with patch.object(Model, "chat", return_value=self.reply):
			first = start_run("hello", agent=self.agent.name)
		frappe.set_user("Administrator")
		cast(User, frappe.get_doc("User", self.flow_user)).remove_roles(FLOW_USER_ROLE)

		frappe.set_user(self.flow_user)
		with patch.object(Model, "chat", return_value=self.reply), self.assertRaises(frappe.PermissionError):
			start_run("again", session=first["session"])
		with self.assertRaises(frappe.PermissionError):
			stop_run(first["name"])

	def test_a_session_cannot_continue_on_an_agent_whose_roles_changed(self):
		frappe.set_user(self.flow_user)
		with patch.object(Model, "chat", return_value=self.reply):
			first = start_run("hello", agent=self.agent.name)
		frappe.set_user("Administrator")
		self.agent.append("roles", {"role": "Report Manager"})
		self.agent.save()
		frappe.clear_cache(user=self.flow_user)

		frappe.set_user(self.flow_user)
		with patch.object(Model, "chat", return_value=self.reply), self.assertRaises(frappe.PermissionError):
			start_run("again", session=first["session"])

	def test_a_system_manager_cannot_stop_anothers_manual_run(self):
		session = _session(self.flow_user)
		run = _run(session)
		frappe.set_user(self.manager)

		with self.assertRaises(frappe.PermissionError):
			stop_run(run.name)

	def test_a_system_manager_can_stop_a_trigger_run(self):
		session = _session(self.flow_user, source="Trigger")
		run = _run(session, source="Trigger")
		frappe.set_user(self.manager)

		self.assertEqual(stop_run(run.name)["status"], "Failed")
