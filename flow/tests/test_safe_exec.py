# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import email
from typing import cast
from unittest.mock import patch

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from frappe.desk.doctype.todo.todo import ToDo
from frappe.email.doctype.email_queue.email_queue import EmailQueue, SendMailContext
from frappe.tests import IntegrationTestCase
from frappe.utils import cstr

from flow.utils.safe_exec import get_flow_safe_globals, safe_exec

SENDMAIL = 'frappe.sendmail(recipients=["someone@example.com"], attachments=attachments)'


class TestFlowSafeExecNamespace(IntegrationTestCase):
	def test_excludes_permission_bypassing_functions(self):
		g = get_flow_safe_globals()
		for name in ("sql", "set_value", "get_all", "commit", "rollback", "add_index", "escape"):
			self.assertNotIn(name, g.frappe.db)
		self.assertNotIn("get_all", g.frappe)
		self.assertNotIn("qb", g.frappe)
		self.assertNotIn("csrf_token", g.frappe.session)

	def test_includes_permission_respecting_helpers(self):
		g = get_flow_safe_globals()
		self.assertIn("get_list", g.frappe)
		for name in ("get_value", "get_single_value", "exists", "count"):
			self.assertIn(name, g.frappe.db)

	def test_injects_write_builtins_but_not_execute(self):
		g = get_flow_safe_globals()
		for name in ("create", "update", "delete", "run_action", "read", "describe", "find_doctypes"):
			self.assertIn(name, g)
		self.assertNotIn("execute", g)
		self.assertNotIn("search_knowledge", g)


class TestFlowSafeExec(IntegrationTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.clear_cache(doctype="System Settings")

	def _run(self, code):
		exec_globals, _ = safe_exec(code, script_filename="test")
		return exec_globals.get("result")

	def test_computation(self):
		self.assertEqual(self._run("result = sum([1, 2, 3])"), 6)

	def test_utils_available(self):
		self.assertEqual(self._run('result = frappe.utils.cint("5")'), 5)

	def test_blocks_import(self):
		with self.assertRaises(ImportError):
			safe_exec("import os")

	def test_blocks_dunder_access(self):
		with self.assertRaises(SyntaxError):
			safe_exec("result = ().__class__")

	def test_raw_sql_unavailable(self):
		with self.assertRaises(AttributeError):
			safe_exec('result = frappe.db.sql("select 1")')

	def test_set_value_unavailable(self):
		with self.assertRaises(AttributeError):
			safe_exec('frappe.db.set_value("ToDo", "x", "status", "Open")')

	def test_get_all_unavailable(self):
		with self.assertRaises(AttributeError):
			safe_exec('result = frappe.get_all("ToDo")')

	def test_get_list_supports_aggregates(self):
		frappe.get_doc({"doctype": "ToDo", "description": "agg a", "status": "Open"}).insert()
		frappe.get_doc({"doctype": "ToDo", "description": "agg b", "status": "Open"}).insert()
		rows = self._run(
			'result = frappe.get_list("ToDo", filters={"status": "Open"}, fields=[{"COUNT": "*", "as": "total"}])'
		)
		self.assertGreaterEqual(rows[0]["total"], 2)

	def test_count_returns_number(self):
		frappe.get_doc({"doctype": "ToDo", "description": "count me", "status": "Open"}).insert()
		self.assertGreaterEqual(self._run('result = frappe.db.count("ToDo")'), 1)

	def test_create_builtin_writes(self):
		result = self._run('result = create("ToDo", [{"description": "made in sandbox"}])')
		self.assertEqual(len(result["created"]), 1)
		self.assertTrue(frappe.db.exists("ToDo", result["created"][0]))

	def test_get_doc_enforces_read_permission(self):
		todo = cast(
			ToDo, frappe.get_doc({"doctype": "ToDo", "description": "secret", "status": "Open"})
		).insert()
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			safe_exec(f'result = frappe.get_doc("ToDo", "{todo.name}")')

	def test_create_enforces_create_permission(self):
		frappe.set_user("Guest")
		with self.assertRaises(PermissionError):
			safe_exec('result = create("ToDo", [{"description": "denied"}])')

	def test_get_list_ignore_permissions_is_stripped(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			safe_exec('result = frappe.get_list("User", fields=["name"], ignore_permissions=True)')

	def test_db_get_list_ignore_permissions_is_stripped(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			safe_exec('result = frappe.db.get_list("User", fields=["name"], ignore_permissions=True)')

	def test_get_list_ignore_user_permissions_is_stripped(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			safe_exec('result = frappe.get_list("User", fields=["name"], ignore_user_permissions=True)')

	def test_get_list_user_impersonation_is_stripped(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			safe_exec('result = frappe.get_list("User", fields=["name"], user="Administrator")')

	def test_render_template_is_unavailable(self):
		with self.assertRaises(AttributeError):
			safe_exec('result = frappe.render_template("{{ 1 }}", {})')

	def _permlevel_note(self):
		return frappe.get_doc(
			{
				"doctype": "Note",
				"title": "flow permlevel",
				"public": 1,
				"seen_by": [{"user": "Administrator"}],
			}
		).insert()

	def _desk_user(self, role="Desk User"):
		"""A user with `role`."""
		email = f"flow-{frappe.scrub(role)}@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Desk",
					"send_welcome_email": 0,
					"roles": [{"role": role}],
				}
			).insert(ignore_permissions=True)
		return email

	def test_get_doc_hides_fields_above_the_users_permlevel(self):
		# desk users can read a public Note at permlevels 0 and 1; its seen_by table is permlevel 2
		note = self._permlevel_note()
		frappe.set_user(self._desk_user())
		for code in (
			f'result = frappe.get_doc("Note", "{note.name}")',
			'result = frappe.get_last_doc("Note", filters={"title": "flow permlevel"})',
		):
			with self.subTest(code):
				result = self._run(code)
				self.assertEqual(result["public"], 1)
				self.assertEqual(result["seen_by"], [])

	def test_get_single_value_hides_fields_above_the_users_permlevel(self):
		# a System Manager reads System Settings at permlevel 0 only
		make_property_setter("System Settings", "language", "permlevel", 1, "Int")
		frappe.set_user(self._desk_user("System Manager"))
		self.assertIsNone(self._run('result = frappe.db.get_single_value("System Settings", "language")'))
		self.assertTrue(self._run('result = frappe.db.get_single_value("System Settings", "country")'))

	def test_get_list_and_get_value_hide_fields_above_the_users_permlevel(self):
		self._permlevel_note()
		frappe.set_user(self._desk_user())
		rows = self._run(
			'result = frappe.get_list("Note", filters={"title": "flow permlevel"}, fields=["*"])'
		)
		self.assertTrue(rows)
		self.assertNotIn("seen_by", rows[0])
		self.assertEqual(
			self._run('result = frappe.db.get_value("Note", {"title": "flow permlevel"}, "public")'), 1
		)

	def _private_file(self):
		"""A private file on an Administrator's ToDo."""
		todo = frappe.get_doc({"doctype": "ToDo", "description": "has a file"}).insert()
		return frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "flow-sendmail-test.txt",
				"content": b"private",
				"is_private": 1,
				"attached_to_doctype": "ToDo",
				"attached_to_name": todo.name,
			}
		).insert()

	def _mail_account(self):
		# the test bench has no built assets, which printing and the email styles look up
		self.enterContext(patch("frappe.utils.get_assets_json", return_value={}))
		frappe.db.set_single_value("Print Settings", "send_print_as_pdf", 0)
		if not frappe.db.exists("Email Account", "Flow Test Out"):
			frappe.get_doc(
				{
					"doctype": "Email Account",
					"email_account_name": "Flow Test Out",
					"email_id": "flow-out@example.com",
					"enable_outgoing": 1,
					"default_outgoing": 1,
					"smtp_server": "localhost",
					"smtp_port": 25,
					"always_use_account_email_id_as_sender": 0,
					"awaiting_password": 0,
					"password": "x",
				}
			).insert(ignore_permissions=True)

	def _queued(self, name):
		"""The queued mail as the scheduler's flush sees it: as Administrator."""
		frappe.set_user("Administrator")
		return SendMailContext(cast(EmailQueue, frappe.get_doc("Email Queue", name)))

	def _html(self, name):
		frappe.set_user("Administrator")
		message = email.message_from_string(
			str(cast(EmailQueue, frappe.get_doc("Email Queue", name)).message)
		)
		(html,) = (
			cast(bytes, part.get_payload(decode=True))
			for part in message.walk()
			if part.get_content_type() == "text/html"
		)
		return html.decode()

	def test_sendmail_attaches_only_files_the_user_can_read(self):
		file = self._private_file()
		with patch("frappe.sendmail") as sendmail:
			safe_exec(SENDMAIL, {"attachments": [{"file_url": file.file_url}]})
		self.assertEqual(sendmail.call_args.kwargs["attachments"], [{"fid": file.name}])

		frappe.set_user("Guest")
		for attachment in ({"fid": file.name}, {"file_url": file.file_url}, {"fname": "a.txt"}):
			with self.subTest(attachment), patch("frappe.sendmail") as sendmail:
				with self.assertRaises(frappe.PermissionError):
					safe_exec(SENDMAIL, {"attachments": [attachment]})
				sendmail.assert_not_called()

	def test_sendmail_print_is_rendered_as_the_user_not_as_the_queue(self):
		# desk users cannot see the permlevel-2 seen_by table; the queue runs as Administrator
		self._mail_account()
		seen_by = self._desk_user("System Manager")
		note = frappe.get_doc(
			{"doctype": "Note", "title": "flow printed", "public": 1, "seen_by": [{"user": seen_by}]}
		).insert()
		print_ = {"print_format_attachment": 1, "doctype": "Note", "name": note.name}
		frappe.set_user(self._desk_user())
		queued = self._run(
			f'result = frappe.sendmail(recipients=["x@example.com"], attachments=[{print_}]).name'
		)
		context = self._queued(queued)
		(attachment,) = context.attachments
		printed = cstr(attachment["fcontent"])
		self.assertIn("flow printed", printed)
		self.assertNotIn(seen_by, printed)

		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			safe_exec(SENDMAIL, {"attachments": [print_]})

	def test_sendmail_accepts_only_the_listed_arguments(self):
		for argument in (
			"raw_html=True",
			'template="x"',
			'wrapper="x"',
			"args={}",
			'header="x"',
			'communication="x"',
		):
			with self.subTest(argument), patch("frappe.sendmail") as sendmail:
				with self.assertRaises(frappe.PermissionError):
					safe_exec(f'frappe.sendmail(recipients=["someone@example.com"], {argument})')
				sendmail.assert_not_called()

	def test_sendmail_does_not_evaluate_templates_in_the_message(self):
		# raw_html would render the message as Jinja with the unchecked frappe.db / frappe.get_doc
		self._mail_account()
		note = frappe.get_doc({"doctype": "Note", "title": "flow hidden", "content": "LEAKME"}).insert()
		frappe.set_user(self._desk_user())
		with self.assertRaises(frappe.PermissionError):
			self._run(
				'result = frappe.sendmail(recipients=["x@example.com"], raw_html=True, '
				f"message=\"{{{{ frappe.get_doc('Note', '{note.name}').content }}}}\")"
			)
		queued = self._run(
			'result = frappe.sendmail(recipients=["x@example.com"], message="{{ 7*7 }}", subject="s").name'
		)
		body = self._html(queued)
		self.assertIn("{{ 7*7 }}", body)
		self.assertNotIn("49", body)

	def test_get_doc_and_get_last_doc_do_not_filter_or_order_by_hidden_fields(self):
		make_property_setter("Note", "content", "permlevel", 2, "Int")
		frappe.get_doc(
			{"doctype": "Note", "title": "flow oracle", "public": 1, "content": "TOPSECRET"}
		).insert()
		frappe.set_user(self._desk_user())
		visible = {"title": "flow oracle"}
		self.assertTrue(self._run(f"result = frappe.get_doc('Note', {visible})"))
		self.assertTrue(self._run(f"result = frappe.get_last_doc('Note', filters={visible})"))
		for filters in (
			{**visible, "content": ["like", "TOPSECRET%"]},
			{**visible, "content": ["like", "NOPE%"]},
		):
			for code in (
				f"result = frappe.get_doc('Note', {filters})",
				f"result = frappe.get_last_doc('Note', filters={filters})",
			):
				with self.subTest(code), self.assertRaises(frappe.PermissionError):
					self._run(code)
		with self.assertRaises(frappe.PermissionError):
			self._run(f"result = frappe.get_last_doc('Note', filters={visible}, order_by='content desc')")

	def test_get_doc_and_get_last_doc_report_a_miss(self):
		with self.assertRaises(frappe.DoesNotExistError):
			self._run("result = frappe.get_doc('Note', {'title': 'flow no such note'})")
		with self.assertRaises(frappe.DoesNotExistError):
			self._run("result = frappe.get_last_doc('Note', filters={'title': 'flow no such note'})")

	def test_sendmail_without_attachments_is_unchanged(self):
		with patch("frappe.sendmail") as sendmail:
			safe_exec('frappe.sendmail(recipients=["someone@example.com"], subject="hi")')
		self.assertEqual(sendmail.call_args.kwargs["attachments"], [])
		self.assertEqual(sendmail.call_args.kwargs["subject"], "hi")
