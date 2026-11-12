# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import json
from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import frappe
from frappe.handler import run_doc_method
from frappe.tests import IntegrationTestCase, UnitTestCase

from flow.flow.doctype.flow_model.flow_model import FlowModel, get_model_hints, get_provider_models
from flow.flow.doctype.flow_provider.flow_provider import FlowProvider
from flow.lib import cloudflare
from flow.lib.cloudflare import CatalogueEntry, CatalogueError
from flow.tests.cloudflare_mock import (
	ACCOUNT_ID,
	PAGE_SIZE,
	TOKEN,
	MockCloudflare,
	native_models,
	openrouter_models,
	provider_row,
)

FLASH = "@cf/zai-org/glm-5.3-flash"
FULL = "@cf/zai-org/glm-5.3"
EMBEDDING = "@cf/baai/bge-m3"


def _model(model_id: str = FLASH, **overrides: Any) -> dict[str, Any]:
	title = overrides.pop("title", f"Cloudflare {model_id}")
	return {
		"doctype": "Flow Model",
		"title": title,
		"provider": "cloudflare",
		"model_id": model_id,
		"enabled": 1,
		**overrides,
	}


def _entry(model_id: str, **overrides: Any) -> CatalogueEntry:
	return CatalogueEntry(id=model_id, kind="generation", **overrides)


class TestParsing(UnitTestCase):
	def test_a_native_item_reads_its_string_valued_properties(self):
		item = next(i for i in native_models() if i["name"] == FLASH)

		entry = cloudflare._native_entry(item)

		self.assertEqual(
			entry,
			CatalogueEntry(
				id=FLASH,
				kind="generation",
				description=item["description"],
				context_window=1048576,
				tools=True,
				reasoning=True,
				vision=True,
				deprecation_date="2026-11-20",
				supported_efforts=("max", "high", "low"),
				mandatory_effort=True,
			),
		)

	def test_missing_and_malformed_properties_become_unknown(self):
		item = {
			"name": "@cf/x/y",
			"task": {"name": "Text Generation"},
			"properties": [
				{"property_id": "context_window", "value": "lots"},
				{"property_id": "function_calling", "value": "maybe"},
				{"property_id": "planned_deprecation_date", "value": "soon"},
				{"property_id": "reasoning_effort", "value": "high"},
				"not a property",
			],
		}

		self.assertEqual(cloudflare._native_entry(item), CatalogueEntry(id="@cf/x/y", kind="generation"))
		self.assertEqual(
			cloudflare._native_entry({"name": "@cf/x/y", "task": {"name": "Text Generation"}}),
			CatalogueEntry(id="@cf/x/y", kind="generation"),
		)

	def test_other_tasks_and_nameless_items_are_skipped(self):
		for item in (
			{"name": "@cf/x/img", "task": {"name": "Text-to-Image"}},
			{"name": "@cf/x/none"},
			{"name": "", "task": {"name": "Text Generation"}},
			{"task": {"name": "Text Generation"}},
		):
			with self.subTest(item=item):
				self.assertIsNone(cloudflare._native_entry(item))

	def test_embedding_models_use_their_input_limit(self):
		by_name = {i["name"]: cloudflare._native_entry(i) for i in native_models()}

		base = by_name["@cf/baai/bge-base-en-v1.5"]
		assert base
		self.assertEqual((base.kind, base.context_window), ("embedding", 512))
		m3 = by_name[EMBEDDING]
		assert m3
		self.assertEqual(m3.context_window, 60000)
		plamo = by_name["@cf/pfnet/plamo-embedding-1b"]
		assert plamo
		self.assertIsNone(plamo.context_window)

	def test_the_openrouter_shape_adds_limits_features_and_efforts(self):
		item = next(i for i in openrouter_models() if i["id"] == FLASH)
		bare = CatalogueEntry(id=FLASH, kind="generation", description="Flash")

		self.assertEqual(
			cloudflare._merge_openrouter(bare, item),
			CatalogueEntry(
				id=FLASH,
				kind="generation",
				description="Flash",
				context_window=1048576,
				max_output=1048576,
				tools=True,
				reasoning=True,
				vision=True,
				deprecation_date="2026-11-20",
				supported_efforts=("max", "high", "low"),
				mandatory_effort=True,
			),
		)

	def test_an_openrouter_item_states_only_what_it_knows(self):
		known = CatalogueEntry(id="@cf/x/y", kind="generation", context_window=8192, tools=True)

		self.assertEqual(cloudflare._merge_openrouter(known, {"id": "@cf/x/y"}), known)

	def test_the_picker_describes_a_model_in_one_line(self):
		entry = _entry(
			FLASH, context_window=1048576, tools=True, reasoning=True, deprecation_date="2026-11-20"
		)

		self.assertEqual(
			cloudflare.describe(entry), "Text · 1,048,576 tokens · tools · reasoning · deprecates 2026-11-20"
		)
		self.assertEqual(cloudflare.describe(CatalogueEntry(id=EMBEDDING, kind="embedding")), "Embeddings")


class TestDeprecation(UnitTestCase):
	TODAY = date(2026, 11, 11)

	def test_a_model_far_from_deprecation_has_no_notice(self):
		entry = _entry(FLASH, deprecation_date="2027-06-01")
		self.assertIsNone(cloudflare.deprecation_notice(entry, [entry], today=self.TODAY))
		self.assertIsNone(cloudflare.deprecation_notice(_entry(FULL), [], today=self.TODAY))

	def test_a_model_deprecating_within_60_days_is_flagged_with_its_likely_successor(self):
		flash = _entry(FLASH, deprecation_date="2026-11-20")

		notice = cloudflare.deprecation_notice(flash, [flash, _entry(FULL)], today=self.TODAY)

		self.assertEqual(
			notice,
			"@cf/zai-org/glm-5.3-flash is planned for deprecation on 2026-11-20. "
			"@cf/zai-org/glm-5.3 is its likely successor.",
		)

	def test_a_successor_that_is_itself_deprecated_is_not_suggested(self):
		flash = _entry(FLASH, deprecation_date="2026-11-20")
		full = _entry(FULL, deprecation_date="2026-12-01")

		notice = cloudflare.deprecation_notice(flash, [flash, full], today=self.TODAY)

		self.assertEqual(notice, "@cf/zai-org/glm-5.3-flash is planned for deprecation on 2026-11-20.")

	def test_a_past_deprecation_date_says_so(self):
		flash = _entry(FLASH, deprecation_date="2026-11-20")

		notice = cloudflare.deprecation_notice(flash, [flash], today=date(2026, 12, 1))

		self.assertEqual(notice, "@cf/zai-org/glm-5.3-flash was deprecated on 2026-11-20.")

	def test_the_window_edge(self):
		entry = _entry(FLASH, deprecation_date="2027-01-10")
		self.assertTrue(cloudflare.deprecation_notice(entry, [], today=date(2026, 11, 11)))
		self.assertIsNone(cloudflare.deprecation_notice(entry, [], today=date(2026, 11, 10)))


class TestReasoningEffort(UnitTestCase):
	FLASH = _entry(FLASH, supported_efforts=("max", "high", "low"), mandatory_effort=True)

	def test_a_listed_effort_is_accepted(self):
		for effort in ("max", "high", "low", None):
			with self.subTest(effort=effort):
				cloudflare.validate_reasoning_effort(self.FLASH, {"reasoning_effort": effort})
		cloudflare.validate_reasoning_effort(self.FLASH, {})

	def test_an_unlisted_effort_is_rejected_with_the_supported_ones(self):
		for effort in ("medium", "none", "xhigh", "ultra", 3):
			with (
				self.subTest(effort=effort),
				self.assertRaisesRegex(
					frappe.ValidationError, "supports the reasoning efforts max, high, low"
				),
			):
				cloudflare.validate_reasoning_effort(self.FLASH, {"reasoning_effort": effort})

	def test_nothing_is_checked_without_a_catalogue_entry_or_listed_efforts(self):
		cloudflare.validate_reasoning_effort(None, {"reasoning_effort": "ultra"})
		cloudflare.validate_reasoning_effort(_entry(FULL), {"reasoning_effort": "ultra"})


class _CatalogueCase(IntegrationTestCase):
	"""A mock Cloudflare and a cloudflare provider pointing at it, with a cold catalogue cache."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cloudflare = cls.enterClassContext(MockCloudflare())
		cls.enterClassContext(patch.dict(frappe.conf, {"developer_mode": 1}))

	def setUp(self):
		self.cloudflare.reset()
		frappe.message_log.clear()
		self.provider = cast(FlowProvider, frappe.get_doc(provider_row(self.cloudflare.base_url)).insert())
		cloudflare.clear_catalogue()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		cloudflare.clear_catalogue()

	def searches(self, **query: str) -> list[dict[str, Any]]:
		return [
			r
			for r in self.cloudflare.calls("/ai/models/search")
			if all(r["query"].get(k) == v for k, v in query.items())
		]


class TestFetchingTheCatalogue(_CatalogueCase):
	def test_every_page_of_both_shapes_is_read(self):
		entries = cloudflare.fetch_catalogue(self.provider)

		self.assertEqual(len(entries), len(native_models()))
		self.assertEqual([e.id for e in entries], sorted(e.id for e in entries))
		pages = -(-len(native_models()) // PAGE_SIZE)
		# The native shape stops at its total_count; the openrouter one has none, so it reads one
		# empty page past the end.
		self.assertEqual(
			[r["query"]["page"] for r in self.searches() if "format" not in r["query"]],
			[str(n) for n in range(1, pages + 1)],
		)
		self.assertEqual(
			[r["query"]["page"] for r in self.searches(format="openrouter")],
			[str(n) for n in range(1, pages + 2)],
		)

	def test_the_request_carries_the_token_and_no_redirects_are_followed(self):
		cloudflare.fetch_catalogue(self.provider)

		for request in self.searches():
			self.assertEqual(request["headers"]["authorization"], f"Bearer {TOKEN}")
			self.assertEqual(request["method"], "GET")

	def test_the_openrouter_facts_win_over_the_native_ones(self):
		by_id = {e.id: e for e in cloudflare.fetch_catalogue(self.provider)}

		flash = by_id[FLASH]
		self.assertEqual(flash.context_window, 1048576)
		self.assertEqual(flash.max_output, 1048576)
		self.assertTrue(flash.tools and flash.reasoning and flash.vision)
		self.assertEqual(flash.deprecation_date, "2026-11-20")
		self.assertEqual((flash.supported_efforts, flash.mandatory_effort), (("max", "high", "low"), True))
		self.assertEqual(by_id["@cf/zai-org/glm-4.7-flash"].context_window, 131072)
		self.assertFalse(by_id["@cf/zai-org/glm-4.7-flash"].vision)
		self.assertEqual(by_id[EMBEDDING].kind, "embedding")

	def test_the_native_shape_is_enough_when_openrouter_is_unavailable(self):
		self.cloudflare.no_openrouter = True

		by_id = {e.id: e for e in cloudflare.fetch_catalogue(self.provider)}

		flash = by_id[FLASH]
		self.assertEqual(flash.context_window, 1048576)
		self.assertIsNone(flash.max_output)
		self.assertTrue(flash.tools and flash.reasoning and flash.vision)
		self.assertEqual(flash.deprecation_date, "2026-11-20")
		self.assertEqual(flash.supported_efforts, ("max", "high", "low"))

	def test_a_server_that_ignores_page_cannot_loop_the_reader(self):
		self.cloudflare.ignore_page = True

		entries = cloudflare.fetch_catalogue(self.provider)

		self.assertEqual(len(entries), PAGE_SIZE)
		# One page, then a second that brought nothing new: for each of the two shapes.
		self.assertEqual(len(self.searches()), 4)

	def test_a_custom_endpoint_has_no_catalogue(self):
		self.provider.base_url = "https://proxy.example.com/openai/v1"
		self.provider.save()

		self.assertIsNone(cloudflare.catalogue_url(self.provider))
		self.assertEqual(cloudflare.fetch_catalogue(self.provider), [])
		self.assertEqual(self.cloudflare.requests, [])

	def test_failures_are_reported_without_the_token(self):
		for status, message in (
			(401, "Workers AI > Read"),
			(403, "Workers AI > Read"),
			(404, "Account ID"),
			(500, "HTTP 500"),
		):
			with self.subTest(status=status):
				self.cloudflare.fail_with = status
				with self.assertRaises(CatalogueError) as raised:
					cloudflare.fetch_catalogue(self.provider)
				self.assertIn(message, str(raised.exception))
				self.assertNotIn(TOKEN, str(raised.exception))

	def test_a_rejected_token_is_a_catalogue_error(self):
		self.provider.api_key = "wrong-token"
		self.provider.save()

		with self.assertRaisesRegex(CatalogueError, "rejected the token"):
			cloudflare.fetch_catalogue(self.provider)

	def test_an_unreachable_server_is_a_catalogue_error(self):
		self.provider.base_url = "http://127.0.0.1:9/client/v4/accounts/x/ai/v1"
		self.provider.save()

		with self.assertRaisesRegex(CatalogueError, "Could not reach Cloudflare"):
			cloudflare.fetch_catalogue(self.provider)

	def test_a_reply_that_is_not_json_is_a_catalogue_error(self):
		with (
			patch("flow.lib.cloudflare.requests.get") as get,
			self.assertRaisesRegex(CatalogueError, "other than JSON"),
		):
			get.return_value.status_code = 200
			get.return_value.ok = True
			get.return_value.json.side_effect = ValueError("no json")
			cloudflare.get_page("https://example.com", TOKEN, {})

		with (
			patch("flow.lib.cloudflare.requests.get") as get,
			self.assertRaisesRegex(CatalogueError, "unexpected"),
		):
			get.return_value.status_code = 200
			get.return_value.ok = True
			get.return_value.json.return_value = ["not", "an", "object"]
			cloudflare.get_page("https://example.com", TOKEN, {})


class TestCatalogueCache(_CatalogueCase):
	def test_a_cold_cache_is_not_filled_unless_asked(self):
		self.assertIsNone(cloudflare.get_catalogue(fetch=False))
		self.assertEqual(self.cloudflare.requests, [])

	def test_a_fetch_is_cached(self):
		entries = cloudflare.get_catalogue(fetch=True)
		assert entries
		calls = len(self.cloudflare.requests)

		again = cloudflare.get_catalogue(fetch=True)

		self.assertEqual(again, entries)
		self.assertEqual(cloudflare.get_catalogue(fetch=False), entries)
		self.assertEqual(len(self.cloudflare.requests), calls)

	def test_a_failed_fetch_is_not_retried_straight_away(self):
		self.cloudflare.fail_with = 500

		self.assertIsNone(cloudflare.get_catalogue(fetch=True))
		calls = len(self.cloudflare.requests)
		self.assertIsNone(cloudflare.get_catalogue(fetch=True))

		self.assertEqual(len(self.cloudflare.requests), calls)
		# Syncing by hand clears the hold.
		cloudflare.sync_catalogue(self.provider)
		self.assertTrue(cloudflare.get_catalogue(fetch=False))

	def test_nothing_is_fetched_for_a_disabled_or_missing_provider(self):
		frappe.db.set_value("Flow Provider", "cloudflare", "enabled", 0)
		self.assertIsNone(cloudflare.get_catalogue(fetch=True))
		frappe.delete_doc("Flow Provider", "cloudflare", force=True)
		self.assertIsNone(cloudflare.get_catalogue(fetch=True))

		self.assertEqual(self.cloudflare.requests, [])

	def test_saving_or_deleting_the_provider_drops_the_cache(self):
		cloudflare.get_catalogue(fetch=True)
		self.provider.gateway_id = "gw"
		self.provider.save()
		self.assertIsNone(cloudflare.get_catalogue(fetch=False))

		cloudflare.get_catalogue(fetch=True)
		self.provider.delete()
		self.assertIsNone(cloudflare.get_catalogue(fetch=False))

	def test_other_providers_leave_the_cache_alone(self):
		cloudflare.get_catalogue(fetch=True)

		frappe.get_doc({"doctype": "Flow Provider", "provider": "openai"}).insert()

		self.assertTrue(cloudflare.get_catalogue(fetch=False))

	def test_entries_are_found_by_model_id(self):
		cloudflare.get_catalogue(fetch=True)

		entry = cloudflare.catalogue_entry(f"cloudflare/{FLASH}", fetch=False)
		assert entry
		self.assertEqual(entry.id, FLASH)
		self.assertEqual(cloudflare.catalogue_entry(FLASH, fetch=False), entry)
		self.assertIsNone(cloudflare.catalogue_entry("cloudflare/@cf/nobody/nothing", fetch=False))


class TestProviderActions(_CatalogueCase):
	def test_test_connection_reaches_the_account_without_spending_inference(self):
		result = self.provider.test_connection()

		self.assertTrue(result["ok"])
		self.assertIn(f"{len(native_models())} models", result["message"])
		self.assertIn("none (direct)", result["message"])
		self.assertEqual(self.cloudflare.calls("/chat/completions"), [])

	def test_test_connection_names_the_gateway(self):
		self.provider.gateway_id = "my-gw"
		self.provider.save()

		self.assertIn("my-gw", self.provider.test_connection()["message"])

	def test_test_connection_reports_a_bad_token_without_the_token(self):
		self.provider.api_key = "wrong-token"
		self.provider.save()

		with self.assertRaisesRegex(frappe.ValidationError, "rejected the token"):
			self.provider.test_connection()
		self.assertNotIn("wrong-token", str(frappe.message_log))

	def test_test_connection_needs_the_default_endpoint(self):
		self.provider.base_url = "https://proxy.example.com/openai/v1"
		self.provider.save()

		with self.assertRaisesRegex(frappe.ValidationError, "default Cloudflare endpoint"):
			self.provider.test_connection()

	def test_sync_models_fills_the_cache_and_flags_deprecated_models(self):
		frappe.get_doc(_model(FLASH)).insert()
		frappe.get_doc(_model(FULL)).insert()
		cloudflare.clear_catalogue()

		result = self.provider.sync_models()

		self.assertEqual(result["count"], len(native_models()))
		self.assertTrue(cloudflare.get_catalogue(fetch=False))
		(warning,) = result["warnings"]
		self.assertIn(FLASH, warning)
		self.assertIn("deprecat", warning)

	def test_sync_models_reports_a_failure(self):
		self.cloudflare.fail_with = 500

		with self.assertRaisesRegex(frappe.ValidationError, "HTTP 500"):
			self.provider.sync_models()

	def test_the_actions_need_write_permission_and_the_cloudflare_provider(self):
		frappe.set_user("Guest")
		for action in (self.provider.test_connection, self.provider.sync_models):
			with self.subTest(action=action.__name__), self.assertRaises(frappe.PermissionError):
				action()
		frappe.set_user("Administrator")

		other = cast(
			FlowProvider, frappe.get_doc({"doctype": "Flow Provider", "provider": "openai"}).insert()
		)
		for action in (other.test_connection, other.sync_models):
			with (
				self.subTest(action=action.__name__),
				self.assertRaisesRegex(frappe.ValidationError, "only applies to the cloudflare provider"),
			):
				action()


class TestActionsOnAClientSuppliedDocument(_CatalogueCase):
	"""Whitelisted methods run on the document the client sends, which was never validated."""

	def setUp(self):
		super().setUp()
		patcher = patch.object(frappe.local, "request", SimpleNamespace(method="POST"), create=True)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _run(self, method: str, base: FlowProvider | None = None, **changes: Any) -> None:
		"""Call the method as `/api/method/run_doc_method` does: on the document the client sent."""
		doc = {**(base or self.provider).as_dict(), **changes}
		run_doc_method(method=method, docs=json.dumps(doc, default=str))

	def test_the_saved_endpoint_is_used_not_the_one_in_the_payload(self):
		# The mock is the saved endpoint; the payload names somewhere else and a wrong account.
		for method in ("test_connection", "sync_models"):
			with self.subTest(method=method):
				self.cloudflare.reset()
				self._run(method, base_url="https://elsewhere.invalid/ai/v1", account_id="0" * 32)

				self.assertTrue(self.cloudflare.calls("/ai/models/search"))
				self.assertEqual(
					{r["headers"]["host"].split(":")[0] for r in self.cloudflare.requests}, {"127.0.0.1"}
				)

	def test_plain_http_stays_refused_after_developer_mode_is_switched_off(self):
		# The row was saved while developer mode was on (the class patch); the token must not go over http now.
		for method in ("test_connection", "sync_models"):
			with (
				self.subTest(method=method),
				patch.dict(frappe.conf, {"developer_mode": 0}),
				self.assertRaisesRegex(frappe.ValidationError, "must use https"),
			):
				self._run(method)
		self.assertEqual(self.cloudflare.requests, [])
		self.assertIsNone(cloudflare.get_catalogue(fetch=False))

	def test_a_payload_cannot_turn_another_provider_into_cloudflare(self):
		other = cast(
			FlowProvider, frappe.get_doc({"doctype": "Flow Provider", "provider": "openai"}).insert()
		)
		with self.assertRaisesRegex(frappe.ValidationError, "only applies to the cloudflare provider"):
			self._run("sync_models", other, provider="cloudflare", account_id=ACCOUNT_ID)


class TestPickerAndHints(_CatalogueCase):
	def test_a_cold_cache_never_reaches_cloudflare_for_the_picker(self):
		get_provider_models("cloudflare")
		get_model_hints(f"cloudflare/{FLASH}")

		self.assertEqual(self.cloudflare.requests, [])

	def test_a_cold_cache_falls_back_to_litellms_list(self):
		with patch("litellm.models_by_provider", {"cloudflare": {"@cf/stale/model"}}):
			self.assertEqual(get_provider_models("Cloudflare"), ["@cf/stale/model"])

	def test_the_picker_lists_the_synced_catalogue_with_descriptions(self):
		cloudflare.get_catalogue(fetch=True)

		models = get_provider_models(" Cloudflare ")

		self.assertEqual(len(models), len(native_models()))
		flash = next(m for m in models if isinstance(m, dict) and m["value"] == FLASH)
		self.assertTrue(str(flash["description"]).startswith("Text · 1,048,576 tokens · tools · reasoning"))

	def test_hints_cover_limits_efforts_and_deprecation(self):
		cloudflare.get_catalogue(fetch=True)

		hints = get_model_hints(f"cloudflare/{FLASH}")

		self.assertEqual(hints["context_window"], 1048576)
		self.assertEqual(hints["supported_efforts"], ["max", "high", "low"])
		self.assertTrue(hints["mandatory_effort"])
		self.assertIn("glm-5.3-flash", hints["deprecation"])

	def test_hints_are_empty_for_unknown_and_non_cloudflare_models(self):
		cloudflare.get_catalogue(fetch=True)

		self.assertEqual(get_model_hints(), {})
		self.assertEqual(get_model_hints("openai/gpt-4.1"), {})
		self.assertEqual(get_model_hints("cloudflare/@cf/nobody/nothing"), {})

	def test_the_picker_needs_read_permission(self):
		cloudflare.get_catalogue(fetch=True)
		frappe.set_user("Guest")

		for provider in ("cloudflare", "openai", None):
			with self.subTest(provider=provider), self.assertRaises(frappe.PermissionError):
				get_provider_models(provider)

	def test_hints_need_read_permission(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			get_model_hints(f"cloudflare/{FLASH}")


class TestModelsAgainstTheCatalogue(_CatalogueCase):
	def _insert(self, model_id: str = FLASH, **overrides: Any) -> FlowModel:
		return cast(FlowModel, frappe.get_doc(_model(model_id, **overrides)).insert())

	def test_the_context_window_comes_from_the_catalogue(self):
		self.assertEqual(self._insert(FLASH).context_window, 1048576)
		self.assertEqual(self._insert("@cf/zai-org/glm-4.7-flash").context_window, 131072)

	def test_a_typed_context_window_is_kept_over_the_catalogue(self):
		doc = self._insert(context_window=4096)

		self.assertEqual(doc.context_window, 4096)

	def test_changing_the_model_detects_the_new_window(self):
		doc = self._insert(FLASH)
		doc.model_id = "@cf/zai-org/glm-4.7-flash"
		doc.save()

		self.assertEqual(doc.context_window, 131072)

	def test_clearing_the_window_detects_it_again(self):
		doc = self._insert(FLASH, context_window=4096)
		doc.context_window = 0
		doc.save()

		self.assertEqual(doc.context_window, 1048576)

	def test_a_model_missing_from_the_catalogue_keeps_the_stored_window_or_zero(self):
		self.assertEqual(self._insert("@cf/nobody/nothing").context_window, 0)

	def test_saving_goes_on_when_cloudflare_cannot_be_reached(self):
		self.cloudflare.fail_with = 500

		doc = self._insert(FLASH)

		self.assertEqual(doc.context_window, 0)
		# The failure is remembered, so a second model doesn't wait for it again.
		calls = len(self.cloudflare.requests)
		self._insert(FULL)
		self.assertEqual(len(self.cloudflare.requests), calls)

	def test_a_rejected_token_does_not_stop_a_model_from_saving(self):
		self.provider.api_key = "wrong-token"
		self.provider.save()

		self.assertEqual(self._insert(FLASH).context_window, 0)

	def test_a_deprecating_model_warns_on_save(self):
		self._insert(FLASH)

		messages = [m for m in frappe.message_log if "deprecat" in str(m.get("message"))]
		self.assertTrue(messages)
		self.assertEqual(messages[-1]["indicator"], "orange")

	def test_a_healthy_model_does_not_warn(self):
		self._insert(FULL)

		self.assertEqual([m for m in frappe.message_log if "deprecat" in str(m.get("message"))], [])

	def test_an_id_outside_the_catalogue_warns_but_saves(self):
		cloudflare.get_catalogue(fetch=True)

		doc = self._insert("@cf/nobody/nothing")

		self.assertEqual(doc.model_id, "cloudflare/@cf/nobody/nothing")
		self.assertTrue(
			[
				m
				for m in frappe.message_log
				if "not in your Cloudflare model catalogue" in str(m.get("message"))
			]
		)

	def test_a_resave_does_not_repeat_the_warnings(self):
		doc = self._insert(FLASH)
		frappe.message_log.clear()

		doc.title = "Renamed"
		doc.save()

		self.assertEqual(frappe.message_log, [])

	def test_the_supported_reasoning_efforts_are_enforced(self):
		self._insert(FLASH, params=json.dumps({"reasoning_effort": "high"}))

		with self.assertRaisesRegex(frappe.ValidationError, "supports the reasoning efforts max, high, low"):
			self._insert(FLASH, title="Medium", params=json.dumps({"reasoning_effort": "medium"}))

	def test_efforts_are_not_checked_for_a_model_without_listed_efforts(self):
		self.assertTrue(
			self._insert(
				FULL.replace("5.3", "4.7-flash"), params=json.dumps({"reasoning_effort": "low"})
			).name
		)
