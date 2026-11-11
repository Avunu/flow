# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from flow.flow.doctype.flow_agent.flow_agent import FlowAgent
from flow.flow.doctype.flow_model.flow_model import FlowModel
from flow.flow.doctype.flow_session.flow_session import FlowSession
from flow.lib.agent import Agent
from flow.lib.model import ChatResponse, Model
from flow.lib.session import load_session, new_session
from flow.tools.builtins import sync_builtin_tools


def _final(text: str = "done") -> ChatResponse:
	return ChatResponse(
		content=text,
		finish_reason="stop",
		usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
	)


class TestCodeAgentSession(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def _agent(self) -> Agent:
		return Agent(model=Model(model_id="openai/gpt-4o-mini"), name="Coder", instructions="be terse")

	def test_code_agent_session_has_no_agent_link(self):
		with patch.object(Model, "chat", return_value=_final("hi")):
			run = self._agent().new_session().chat("hello")

		self.assertEqual(run.status, "Completed")
		self.assertEqual(run.output, "hi")
		self.assertIsNone(cast(FlowSession, frappe.get_doc("Flow Session", run.session)).agent)

	def test_code_agent_snapshot_uses_model_id(self):
		with patch.object(Model, "chat", return_value=_final()):
			run = self._agent().new_session().chat("hello")

		snapshot = json.loads(run.config_snapshot)
		self.assertEqual(snapshot["title"], "Coder")
		self.assertEqual(snapshot["model"], "openai/gpt-4o-mini")

	def test_code_agent_multi_turn_threads_history(self):
		captured: list[list[dict[str, Any]]] = []

		def capture(messages: list[dict[str, Any]], **_: Any) -> ChatResponse:
			captured.append([dict(m) for m in messages])
			return _final(f"reply {len(captured)}")

		with patch.object(Model, "chat", side_effect=capture):
			session = self._agent().new_session()
			session.chat("one")
			session.chat("two")

		user_msgs = [m["content"] for m in captured[1] if m.get("role") == "user"]
		self.assertEqual(user_msgs, ["one", "two"])

	def test_load_code_session_requires_agent(self):
		with patch.object(Model, "chat", return_value=_final()):
			run = self._agent().new_session().chat("hello")

		with self.assertRaisesRegex(frappe.ValidationError, "code agent"):
			load_session(run.session)

		with patch.object(Model, "chat", return_value=_final("again")):
			resumed = load_session(run.session, agent=self._agent()).chat("more")
		self.assertEqual(resumed.output, "again")

	def test_doctype_agent_new_session_links_agent(self):
		sync_builtin_tools()
		model = cast(
			FlowModel,
			frappe.get_doc(
				{
					"doctype": "Flow Model",
					"title": "Sess Model",
					"model_id": "openai/gpt-4o-mini",
					"enabled": 1,
				}
			),
		).insert()
		agent_doc = cast(
			FlowAgent,
			frappe.get_doc(
				{
					"doctype": "Flow Agent",
					"title": "Sess Agent",
					"model": model.name,
					"instructions": "x",
					"enabled": 1,
				}
			),
		).insert()

		with patch.object(Model, "chat", return_value=_final()):
			run = agent_doc.new_session().chat("hello")

		self.assertEqual(cast(FlowSession, frappe.get_doc("Flow Session", run.session)).agent, agent_doc.name)


class TestSessionModelOverride(IntegrationTestCase):
	def setUp(self):
		sync_builtin_tools()
		self.model_a = cast(
			FlowModel,
			frappe.get_doc(
				{
					"doctype": "Flow Model",
					"title": "Override Base",
					"model_id": "openai/gpt-4o-mini",
					"enabled": 1,
				}
			),
		).insert()
		self.model_b = cast(
			FlowModel,
			frappe.get_doc(
				{"doctype": "Flow Model", "title": "Override Alt", "model_id": "openai/gpt-4o", "enabled": 1}
			),
		).insert()
		self.agent_doc = cast(
			FlowAgent,
			frappe.get_doc(
				{
					"doctype": "Flow Agent",
					"title": "Override Agent",
					"model": self.model_a.name,
					"instructions": "x",
					"enabled": 1,
				}
			),
		).insert()

	def tearDown(self):
		frappe.db.rollback()

	def test_new_session_applies_model_override(self):
		convo = new_session(self.agent_doc, model=self.model_b.name)
		self.assertEqual(convo.model, self.model_b.name)

		with patch.object(Model, "chat", return_value=_final()):
			run = convo.chat("hi")
		self.assertEqual(json.loads(run.config_snapshot or "")["model"], self.model_b.name)

	def test_load_session_switches_model_override(self):
		convo = new_session(self.agent_doc)  # uses the agent's default model
		with patch.object(Model, "chat", return_value=_final()):
			first = convo.chat("hi")
		self.assertEqual(json.loads(first.config_snapshot or "")["model"], self.model_a.name)

		switched = load_session(str(convo.name), model=self.model_b.name)
		self.assertEqual(switched.model, self.model_b.name)

		with patch.object(Model, "chat", return_value=_final()):
			second = switched.chat("again")
		self.assertEqual(json.loads(second.config_snapshot or "")["model"], self.model_b.name)


class TestChatCommitBoundaries(IntegrationTestCase):
	"""chat() commits the Running run before the model call and the Failed run before re-raising.

	Tests run in a transaction their tearDown rolls back, so the commit is skipped under
	frappe.flags.in_test; these tests lift that and watch the commit instead of performing it."""

	def setUp(self):
		sync_builtin_tools()
		model = cast(
			FlowModel,
			frappe.get_doc(
				{
					"doctype": "Flow Model",
					"title": "Commit Model",
					"model_id": "openai/gpt-4o-mini",
					"enabled": 1,
				}
			),
		).insert()
		self.agent_doc = cast(
			FlowAgent,
			frappe.get_doc(
				{
					"doctype": "Flow Agent",
					"title": "Commit Agent",
					"model": model.name,
					"instructions": "x",
					"enabled": 1,
				}
			),
		).insert()
		self.session = new_session(self.agent_doc)
		self.events: list[tuple[str, str | None]] = []

	def tearDown(self):
		frappe.db.rollback()

	@contextmanager
	def _outside_tests(self) -> Generator[None]:
		frappe.flags.in_test = False
		try:
			yield
		finally:
			frappe.flags.in_test = True

	def _run_status(self) -> str | None:
		return frappe.db.get_value("Flow Run", {"session": self.session.name}, "status")

	def _record_commit(self):
		self.events.append(("commit", self._run_status()))

	def _model_chat(self, *args, **kwargs):
		self.events.append(("model", self._run_status()))
		return _final()

	def test_running_run_is_committed_before_the_model_call(self):
		with (
			self._outside_tests(),
			patch.object(frappe.db, "commit", side_effect=self._record_commit),
			patch.object(Model, "chat", side_effect=self._model_chat),
		):
			self.session.chat("hello")

		self.assertEqual(self.events, [("commit", "Running"), ("model", "Running")])

	def test_failed_run_is_committed_before_the_error_propagates(self):
		def boom(*args, **kwargs):
			self.events.append(("model", self._run_status()))
			raise RuntimeError("model down")

		with (
			self._outside_tests(),
			patch.object(frappe.db, "commit", side_effect=self._record_commit),
			patch.object(Model, "chat", side_effect=boom),
			self.assertRaisesRegex(RuntimeError, "model down"),
		):
			self.session.chat("hello")

		self.assertEqual(self.events, [("commit", "Running"), ("model", "Running"), ("commit", "Failed")])

	def test_tests_never_commit(self):
		with (
			patch.object(frappe.db, "commit") as commit,
			patch.object(Model, "chat", return_value=_final()),
		):
			self.session.chat("hello")

		commit.assert_not_called()
