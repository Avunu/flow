# Copyright (c) 2026, Frappe Technologies and Contributors
# See license.txt

from typing import Any, cast
from unittest.mock import patch

import frappe
from frappe.core.doctype.user.user import User
from frappe.desk.doctype.todo.todo import ToDo
from frappe.tests import IntegrationTestCase

from flow import triggers
from flow.flow.doctype.flow_agent.flow_agent import FlowAgent
from flow.flow.doctype.flow_model.flow_model import FlowModel
from flow.flow.doctype.flow_run.flow_run import FlowRun
from flow.flow.doctype.flow_trigger.flow_trigger import FlowTrigger
from flow.lib.model import ChatResponse, Model
from flow.tools.builtins import sync_builtin_tools
from flow.triggers import triggers as triggers_module


def _final(text: str = "done") -> ChatResponse:
	return ChatResponse(content=text, tool_calls=[], finish_reason="stop", usage={})


def _model(**overrides: Any) -> dict:
	doc = {
		"doctype": "Flow Model",
		"title": "Test Trigger Model",
		"model_id": "openai/gpt-4o-mini",
		"enabled": 1,
	}
	doc.update(overrides)
	return doc


def _agent(model_name: str, **overrides: Any) -> dict:
	doc = {
		"doctype": "Flow Agent",
		"title": "Test Trigger Agent",
		"model": model_name,
		"instructions": "Be terse.",
		"enabled": 1,
	}
	doc.update(overrides)
	return doc


def _trigger(agent_name: str, **overrides: Any) -> dict:
	doc = {
		"doctype": "Flow Trigger",
		"title": "Test Trigger",
		"agent": agent_name,
		"enabled": 1,
		"event": "DocType Event",
		"target_doctype": "ToDo",
		"doc_event": "after_insert",
		"prompt_template": "A new {{ doc.doctype }} '{{ doc.name }}' was created.",
	}
	doc.update(overrides)
	return doc


class TestFlowTriggerValidation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		sync_builtin_tools()

	def setUp(self):
		self.model = cast(FlowModel, frappe.get_doc(_model())).insert()
		self.agent = cast(FlowAgent, frappe.get_doc(_agent(str(self.model.name)))).insert()

	def tearDown(self):
		frappe.db.rollback()

	def test_doctype_event_trigger_inserts(self):
		doc = cast(FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name)))).insert()

		self.assertEqual(doc.event, "DocType Event")
		self.assertEqual(doc.target_doctype, "ToDo")
		self.assertEqual(doc.doc_event, "after_insert")

	def test_doctype_event_requires_target_doctype(self):
		doc = cast(FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name), target_doctype=None)))
		with self.assertRaisesRegex(frappe.ValidationError, "Target DocType"):
			doc.insert()

	def test_doctype_event_requires_doc_event(self):
		doc = cast(FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name), doc_event=None)))
		with self.assertRaisesRegex(frappe.ValidationError, "Doc Event"):
			doc.insert()

	def test_scheduled_trigger_inserts_with_cron(self):
		doc = cast(
			FlowTrigger,
			frappe.get_doc(
				_trigger(
					str(self.agent.name),
					event="Scheduled",
					target_doctype=None,
					doc_event=None,
					cron_expression="0 9 * * 5",
				)
			),
		).insert()

		self.assertEqual(doc.event, "Scheduled")
		self.assertEqual(doc.cron_expression, "0 9 * * 5")

	def test_scheduled_trigger_requires_cron(self):
		doc = cast(
			FlowTrigger,
			frappe.get_doc(
				_trigger(str(self.agent.name), event="Scheduled", target_doctype=None, doc_event=None)
			),
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Cron"):
			doc.insert()

	def test_scheduled_trigger_rejects_invalid_cron(self):
		doc = cast(
			FlowTrigger,
			frappe.get_doc(
				_trigger(
					str(self.agent.name),
					event="Scheduled",
					target_doctype=None,
					doc_event=None,
					cron_expression="this is not cron",
				)
			),
		)
		with self.assertRaisesRegex(frappe.ValidationError, "cron"):
			doc.insert()

	def test_invalid_condition_rejected(self):
		doc = cast(FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name), condition="doc.status ==")))
		with self.assertRaisesRegex(frappe.ValidationError, "condition"):
			doc.insert()

	def test_invalid_prompt_template_rejected(self):
		doc = cast(FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name), prompt_template="{{ broken")))
		with self.assertRaisesRegex(frappe.ValidationError, "template"):
			doc.insert()

	def test_unknown_agent_rejected(self):
		doc = cast(FlowTrigger, frappe.get_doc(_trigger("does-not-exist")))
		with self.assertRaises(frappe.LinkValidationError):
			doc.insert()


class TestFlowTriggerFiring(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		sync_builtin_tools()

	def setUp(self):
		self.model = cast(FlowModel, frappe.get_doc(_model())).insert()
		self.agent = cast(FlowAgent, frappe.get_doc(_agent(str(self.model.name)))).insert()

	def tearDown(self):
		frappe.db.rollback()

	# ── fire(): the worker that renders the prompt and runs the agent ──────────

	def test_fire_creates_run_with_trigger_source(self):
		trig = cast(FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name)))).insert()
		todo = cast(ToDo, frappe.get_doc({"doctype": "ToDo", "description": "trigger probe"})).insert()

		with patch.object(Model, "chat", return_value=_final("acknowledged")):
			run_name = triggers.fire(str(trig.name), target_doctype="ToDo", target_name=todo.name)

		self.assertIsNotNone(run_name)
		run = cast(FlowRun, frappe.get_doc("Flow Run", run_name or ""))
		self.assertEqual(run.source, "Trigger")
		self.assertEqual(run.trigger, trig.name)
		self.assertEqual(run.status, "Completed")

	def test_fire_renders_doc_context_into_prompt(self):
		trig = cast(FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name)))).insert()
		todo = cast(ToDo, frappe.get_doc({"doctype": "ToDo", "description": "trigger probe"})).insert()

		with patch.object(Model, "chat", return_value=_final()):
			run_name = triggers.fire(str(trig.name), target_doctype="ToDo", target_name=todo.name)

		run = cast(FlowRun, frappe.get_doc("Flow Run", run_name or ""))
		# template is "A new {{ doc.doctype }} '{{ doc.name }}' was created."
		self.assertIn(todo.name, run.input or "")
		self.assertIn("ToDo", run.input or "")

	def test_fire_skips_disabled_trigger(self):
		trig = cast(FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name), enabled=0))).insert()
		with patch.object(Model, "chat", return_value=_final()) as chat:
			result = triggers.fire(str(trig.name))
		self.assertIsNone(result)
		chat.assert_not_called()

	def test_fire_skips_when_condition_false(self):
		trig = cast(
			FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name), condition="doc.status == 'Cancelled'"))
		).insert()
		todo = cast(ToDo, frappe.get_doc({"doctype": "ToDo", "description": "open todo"})).insert()

		with patch.object(Model, "chat", return_value=_final()) as chat:
			result = triggers.fire(str(trig.name), target_doctype="ToDo", target_name=todo.name)

		self.assertIsNone(result)
		chat.assert_not_called()

	def test_fire_skips_when_target_deleted(self):
		trig = cast(FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name)))).insert()
		with patch.object(Model, "chat", return_value=_final()) as chat:
			result = triggers.fire(str(trig.name), target_doctype="ToDo", target_name="does-not-exist")
		self.assertIsNone(result)
		chat.assert_not_called()

	# ── dispatch(): the doc_events hook that enqueues matching triggers ────────

	def test_dispatch_enqueues_matching_trigger(self):
		trig = cast(FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name)))).insert()
		todo = cast(ToDo, frappe.get_doc({"doctype": "ToDo", "description": "dispatch probe"})).insert()

		with patch.object(frappe, "enqueue") as enqueue:
			triggers.dispatch(todo, "after_insert")

		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["trigger"], trig.name)
		self.assertEqual(enqueue.call_args.kwargs["target_name"], todo.name)

	def test_dispatch_ignores_non_matching_event(self):
		frappe.get_doc(_trigger(str(self.agent.name), doc_event="after_insert")).insert()
		todo = cast(ToDo, frappe.get_doc({"doctype": "ToDo", "description": "x"})).insert()
		with patch.object(frappe, "enqueue") as enqueue:
			triggers.dispatch(todo, "on_trash")
		enqueue.assert_not_called()

	def test_dispatch_skips_ai_internal_doctypes(self):
		frappe.get_doc(_trigger(str(self.agent.name))).insert()
		with patch.object(frappe, "enqueue") as enqueue:
			triggers.dispatch(self.agent, "on_update")
		enqueue.assert_not_called()

	def test_dispatch_respects_false_condition(self):
		frappe.get_doc(_trigger(str(self.agent.name), condition="doc.status == 'Cancelled'")).insert()
		todo = cast(ToDo, frappe.get_doc({"doctype": "ToDo", "description": "open"})).insert()
		with patch.object(frappe, "enqueue") as enqueue:
			triggers.dispatch(todo, "after_insert")
		enqueue.assert_not_called()

	# ── dispatch_scheduled(): the cron hook ────────────────────────────────────

	def _scheduled(self, cron: str) -> Any:
		return cast(
			FlowTrigger,
			frappe.get_doc(
				_trigger(
					str(self.agent.name),
					event="Scheduled",
					target_doctype=None,
					doc_event=None,
					cron_expression=cron,
				)
			),
		).insert()

	def test_dispatch_scheduled_fires_when_due(self):
		trig = self._scheduled("* * * * *")
		# last fired 2 hours ago → the next minute boundary after that is long past → due now
		two_hours_ago = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-2)
		frappe.db.set_value("Flow Trigger", trig.name, "last_fired_at", two_hours_ago, update_modified=False)

		with patch.object(frappe, "enqueue") as enqueue:
			triggers.dispatch_scheduled()

		fired = [c for c in enqueue.call_args_list if c.kwargs.get("trigger") == trig.name]
		self.assertEqual(len(fired), 1)
		last = frappe.db.get_value("Flow Trigger", trig.name, "last_fired_at")
		last_fired_at = frappe.utils.get_datetime(last)
		assert last_fired_at is not None
		self.assertGreater(last_fired_at, two_hours_ago)

	def test_dispatch_scheduled_fires_first_run_from_creation(self):
		# never fired; backdate creation so the first occurrence after it is already due
		trig = self._scheduled("* * * * *")
		frappe.db.set_value(
			"Flow Trigger",
			trig.name,
			"creation",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-2),
			update_modified=False,
		)

		with patch.object(frappe, "enqueue") as enqueue:
			triggers.dispatch_scheduled()

		fired = [c for c in enqueue.call_args_list if c.kwargs.get("trigger") == trig.name]
		self.assertEqual(len(fired), 1)

	def test_dispatch_scheduled_skips_recently_fired(self):
		# fired just now → next occurrence is in the future → not due
		trig = self._scheduled("* * * * *")
		frappe.db.set_value(
			"Flow Trigger", trig.name, "last_fired_at", frappe.utils.now_datetime(), update_modified=False
		)

		with patch.object(frappe, "enqueue") as enqueue:
			triggers.dispatch_scheduled()

		fired = [c for c in enqueue.call_args_list if c.kwargs.get("trigger") == trig.name]
		self.assertEqual(len(fired), 0)


class TestFlowTriggerIdentity(IntegrationTestCase):
	"""A trigger runs as its run_as user (else its owner), and the caller's identity comes back."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		sync_builtin_tools()

	def setUp(self):
		self.model = cast(FlowModel, frappe.get_doc(_model())).insert()
		self.agent = cast(FlowAgent, frappe.get_doc(_agent(str(self.model.name)))).insert()
		self.user = (
			cast(
				User,
				frappe.get_doc(
					{
						"doctype": "User",
						"email": "trigger-run-as@example.com",
						"first_name": "Run As",
						"roles": [{"role": "Report Manager"}],
						"send_welcome_email": 0,
					}
				),
			)
			.insert(ignore_permissions=True)
			.name
		)
		self.todo = cast(ToDo, frappe.get_doc({"doctype": "ToDo", "description": "identity probe"})).insert()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _fire_as_seen_by_the_model(self, trig) -> str:
		seen: list[str] = []

		def chat(*args, **kwargs):
			seen.append(frappe.session.user)
			return _final()

		with patch.object(Model, "chat", side_effect=chat):
			triggers.fire(trig.name, target_doctype="ToDo", target_name=self.todo.name)
		return seen[0]

	def test_fire_runs_as_run_as_user(self):
		trig = cast(FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name), run_as=self.user))).insert()

		self.assertEqual(self._fire_as_seen_by_the_model(trig), self.user)
		self.assertEqual(frappe.session.user, "Administrator")

	def test_fire_falls_back_to_the_trigger_owner(self):
		trig = cast(FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name)))).insert()
		frappe.db.set_value("Flow Trigger", trig.name, "owner", self.user, update_modified=False)

		self.assertEqual(self._fire_as_seen_by_the_model(trig), self.user)

	def test_fire_restores_the_caller_when_the_agent_fails(self):
		trig = cast(FlowTrigger, frappe.get_doc(_trigger(str(self.agent.name), run_as=self.user))).insert()

		with (
			patch.object(Model, "chat", side_effect=RuntimeError("model down")),
			self.assertRaisesRegex(RuntimeError, "model down"),
		):
			triggers.fire(str(trig.name), target_doctype="ToDo", target_name=self.todo.name)

		self.assertEqual(frappe.session.user, "Administrator")

	def test_dispatch_evaluates_the_condition_as_run_as_user(self):
		frappe.get_doc(_trigger(str(self.agent.name), run_as=self.user, condition="True")).insert()
		seen: list[str] = []

		def condition(condition, doc):
			seen.append(frappe.session.user)
			return True

		with (
			patch("flow.triggers.triggers._eval_condition", side_effect=condition),
			patch.object(frappe, "enqueue"),
		):
			triggers.dispatch(self.todo, "after_insert")

		self.assertEqual(seen, [self.user])
		self.assertEqual(frappe.session.user, "Administrator")

	def test_acting_as_restores_the_caller_when_the_block_raises(self):
		with self.assertRaises(ZeroDivisionError), triggers_module._acting_as(self.user or ""):
			self.assertEqual(frappe.session.user, self.user)
			raise ZeroDivisionError

		self.assertEqual(frappe.session.user, "Administrator")
