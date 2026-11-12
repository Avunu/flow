# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, cast

import frappe

if TYPE_CHECKING:
	from frappe.model.document import Document
	from frappe.utils.redis_wrapper import ClientCache

	from flow.flow.doctype.flow_agent.flow_agent import FlowAgent
	from flow.flow.doctype.flow_trigger.flow_trigger import FlowTrigger

DOC_EVENTS = frozenset({"after_insert", "on_update", "on_submit", "on_cancel", "on_trash"})
CACHE_PREFIX = "flow_doctype_triggers::"


@contextmanager
def _acting_as(user: str) -> Generator[None]:
	"""Run the block as `user`, then put the caller's identity back (also when the block raises).

	A trigger deliberately runs as its configured `run_as` user (falling back to the user who
	owns the trigger), not as whoever's action fired it. The identity is never request data:
	only System Managers can write a Flow Trigger (its only permission row), and
	FlowTrigger._validate_run_as rejects Guest and disabled users at save time.
	"""
	original_user = frappe.session.user
	# nosemgrep: frappe-setuser -- impersonation is the Flow Trigger run_as feature; see the docstring
	frappe.set_user(user)
	try:
		yield
	finally:
		# nosemgrep: frappe-setuser -- restores the identity captured on entry, not an input
		frappe.set_user(original_user)


def _client_cache() -> ClientCache:
	# frappe.init() always sets it up; the module attribute is only typed as optional
	return cast("ClientCache", frappe.client_cache)


def _enabled_doctype_triggers(target_doctype: str) -> list:
	return frappe.get_all(
		"Flow Trigger",
		filters={"event": "DocType Event", "target_doctype": target_doctype, "enabled": 1},
		fields=["name", "doc_event", "condition", "run_as", "owner"],
	)


def _doctype_triggers(target_doctype: str, doc_event: str) -> list:
	triggers = _client_cache().get_value(
		f"{CACHE_PREFIX}{target_doctype}", generator=lambda: _enabled_doctype_triggers(target_doctype)
	)
	return [t for t in triggers if t.doc_event == doc_event]


def clear_trigger_cache() -> None:
	"""Drop the cached triggers now and again when the transaction ends."""

	def forget():
		_client_cache().delete_keys(CACHE_PREFIX)

	forget()
	frappe.db.after_commit.add(forget)
	frappe.db.after_rollback.add(forget)


def _eval_condition(condition: str, doc: Document) -> bool:
	from frappe.utils.safe_exec import get_safe_globals

	from flow.utils.conditions import evaluate_condition

	context = {"doc": doc, "utils": get_safe_globals().get("frappe").get("utils")}
	try:
		return evaluate_condition(condition, context)
	except Exception:
		frappe.log_error(title="Flow Trigger condition eval failed")
		return False


def _passes_condition(trigger, doc: Document) -> bool:
	"""Evaluate the pre-enqueue condition as the trigger's run identity (matching fire),
	so a permission-sensitive condition doesn't silently under-fire for the low-privilege
	user whose action triggered it."""
	with _acting_as(trigger.run_as or trigger.owner):
		return _eval_condition(trigger.condition, doc)


def dispatch(doc: Document, method: str | None = None) -> None:
	"""doc_events hook: enqueue any matching DocType Event triggers."""
	if method not in DOC_EVENTS:
		return
	if doc.doctype in {"Flow Run", "Flow Trigger", "Flow Agent", "Flow Tool", "Flow Model"}:
		return
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_install_db:
		return

	triggers = _doctype_triggers(doc.doctype, method)
	# a deleted document can't be loaded once the job runs, so it travels with the job
	deleted_doc = doc.as_dict() if triggers and method == "on_trash" else None

	for trigger in triggers:
		if trigger.condition and not _passes_condition(trigger, doc):
			continue
		frappe.enqueue(
			"flow.triggers.fire",
			enqueue_after_commit=True,
			trigger=trigger.name,
			target_doctype=doc.doctype,
			target_name=doc.name,
			deleted_doc=deleted_doc,
		)


def dispatch_scheduled() -> None:
	"""Scheduler hook: fire any Scheduled triggers whose cron is due."""
	from croniter import CroniterBadCronError, croniter

	now = frappe.utils.now_datetime()
	triggers = frappe.get_all(
		"Flow Trigger",
		filters={"event": "Scheduled", "enabled": 1},
		fields=["name", "cron_expression", "last_fired_at", "creation"],
	)
	for t in triggers:
		# Anchor on last fire; fall back to creation so the first run isn't deferred forever.
		anchor = frappe.utils.get_datetime(t.last_fired_at or t.creation)
		try:
			nxt = croniter(t.cron_expression, anchor).get_next(datetime)
		except CroniterBadCronError, ValueError:
			frappe.log_error(title=f"Flow Trigger cron parse failed: {t.name}")
			continue
		if nxt <= now:
			frappe.db.set_value("Flow Trigger", t.name, "last_fired_at", now, update_modified=False)
			frappe.enqueue("flow.triggers.fire", trigger=t.name)


def fire(
	trigger: str,
	target_doctype: str | None = None,
	target_name: str | None = None,
	deleted_doc: dict | None = None,
) -> str | None:
	"""Worker: render the prompt and run the agent. Returns the Flow Run name, or None if skipped.
	`deleted_doc` is the target's last state when its deletion fired the trigger."""
	t = cast("FlowTrigger", frappe.get_doc("Flow Trigger", trigger))
	if not t.enabled:
		return None
	if deleted_doc and frappe.db.exists(target_doctype, target_name):
		return None  # the delete was rolled back: the job is queued on commit, not on the delete

	# A trigger runs as its configured `run_as` user (falling back to the owner)
	with _acting_as(t.run_as or t.owner):
		doc = None
		if deleted_doc:
			doc = frappe.get_doc(deleted_doc)
			target_doctype = target_name = None  # the run has nothing left to link to
		elif target_doctype and target_name:
			try:
				doc = frappe.get_doc(target_doctype, target_name)
			except frappe.DoesNotExistError:
				return None
		if doc and t.condition and not _eval_condition(t.condition, doc):
			return None

		# t.prompt_template is authored by a System Manager (the only role that can write a Flow
		# Trigger) and validated against Jinja's SandboxedEnvironment on save; frappe.render_template
		# renders in frappe's sandboxed environment too.
		# nosemgrep: frappe-ssti -- System Manager-authored template, sandboxed Jinja
		prompt = frappe.render_template(t.prompt_template, {"doc": doc, "now": frappe.utils.now_datetime()})
		agent_doc = cast("FlowAgent", frappe.get_doc("Flow Agent", t.agent))
		run = agent_doc.run(
			prompt,
			source="Trigger",
			trigger=t.name,
			reference_doctype=target_doctype if doc else None,
			reference_name=target_name if doc else None,
			auto_approve=bool(t.auto_approve),
		)
		return run.name
