# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import frappe
import litellm
from frappe.tests import IntegrationTestCase, UnitTestCase

from flow.flow.doctype.flow_model.flow_model import FlowModel
from flow.flow.doctype.flow_provider.flow_provider import FlowProvider
from flow.knowledge import embedder
from flow.lib import cloudflare
from flow.lib.model import ChatResponse, Model, ToolCall, ToolCallBegin, resolve_call_kwargs
from flow.tests.cloudflare_mock import ACCOUNT_ID, TOKEN, MockCloudflare, offline_catalogue, provider_row

GLM = "@cf/zai-org/glm-5.3-flash"


def _model(**overrides: Any) -> dict[str, Any]:
	doc = {
		"doctype": "Flow Model",
		"title": "Cloudflare GLM",
		"provider": "cloudflare",
		"model_id": GLM,
		"enabled": 1,
	}
	return {**doc, **overrides}


def _drain(stream):
	yielded = []
	while True:
		try:
			yielded.append(next(stream))
		except StopIteration as e:
			return yielded, e.value


class TestValidators(UnitTestCase):
	def test_account_id_is_a_lowercased_32_char_hex_string(self):
		self.assertEqual(cloudflare.validate_account_id(f"  {ACCOUNT_ID.upper()} "), ACCOUNT_ID)

	def test_account_id_rejects_anything_else(self):
		for bad in (
			"",
			None,
			"abc",
			ACCOUNT_ID + "0",
			ACCOUNT_ID[:-1] + "g",
			"../" + ACCOUNT_ID,
			ACCOUNT_ID + "/../x",
			ACCOUNT_ID[:-2] + "?x",
			ACCOUNT_ID[:-1] + "\n",
		):
			with self.subTest(account_id=bad), self.assertRaises(frappe.ValidationError):
				cloudflare.validate_account_id(bad)

	def test_gateway_id_is_optional_and_restricted(self):
		self.assertEqual(cloudflare.validate_gateway_id(None), "")
		self.assertEqual(cloudflare.validate_gateway_id(" my-gateway_1 "), "my-gateway_1")
		for bad in ("-leading", "has space", "a\r\nX-Evil: 1", "a/b", "a" * 65, "g\nw"):
			with self.subTest(gateway_id=bad), self.assertRaises(frappe.ValidationError):
				cloudflare.validate_gateway_id(bad)

	def test_extra_headers_allow_only_gateway_controls(self):
		headers = cloudflare.validate_extra_headers(
			{"CF-AIG-Cache-TTL": "3600", "cf-aig-metadata": '{"a": 1}'}
		)
		self.assertEqual(headers, {"cf-aig-cache-ttl": "3600", "cf-aig-metadata": '{"a": 1}'})
		self.assertEqual(cloudflare.validate_extra_headers(None), {})

	def test_extra_headers_reject_everything_else(self):
		for bad in (
			["cf-aig-cache-ttl"],
			"cf-aig-cache-ttl: 1",
			{"authorization": "Bearer x"},
			{"Host": "evil.example"},
			{"cookie": "a=b"},
			{"content-type": "text/plain"},
			{"x-forwarded-for": "1.1.1.1"},
			{"cf-aig-authorization": "Bearer x"},
			{"CF-AIG-Gateway-Id": "other"},
			{"cf-aig-": "x"},
			{"cf-aig-a b": "x"},
			{"cf-aig-ttl": 3600},
			{"cf-aig-ttl": ["1"]},
			{"cf-aig-ttl": "a\r\nX-Evil: 1"},
			{"cf-aig-ttl": "a\nb"},
			{"cf-aig-ttl": "a\x00b"},
			{"cf-aig-ttl": "a" * 1025},
		):
			with self.subTest(headers=bad), self.assertRaises(frappe.ValidationError):
				cloudflare.validate_extra_headers(bad)

	def test_http_base_url_needs_developer_mode(self):
		with patch.dict(frappe.conf, {"developer_mode": 0}):
			cloudflare.check_base_url("https://proxy.example.com/ai/v1")
			cloudflare.check_base_url(None)
			with self.assertRaises(frappe.ValidationError):
				cloudflare.check_base_url("http://proxy.example.com/ai/v1")
			with self.assertRaises(frappe.ValidationError):
				cloudflare.check_base_url("HTTP://proxy.example.com/ai/v1")
		with patch.dict(frappe.conf, {"developer_mode": 1}):
			cloudflare.check_base_url("http://127.0.0.1:8000/ai/v1")

	def test_hosted_models_are_workers_ai_ids(self):
		self.assertTrue(cloudflare.is_hosted_model(GLM))
		self.assertTrue(cloudflare.is_hosted_model("@hf/thebloke/mistral-7b-instruct-v0.1-awq"))
		self.assertFalse(cloudflare.is_hosted_model("openai/gpt-4.1"))
		self.assertFalse(cloudflare.is_hosted_model("zai-org/glm-5.3-flash"))

	def test_redact_removes_the_secret(self):
		self.assertEqual(cloudflare.redact(f"bad token {TOKEN}!", TOKEN), "bad token ***!")
		self.assertEqual(cloudflare.redact("nothing", None), "nothing")


class TestProviderValidation(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def _insert(self, **overrides: Any) -> FlowProvider:
		return cast(FlowProvider, frappe.get_doc(provider_row(**overrides)).insert())

	def test_a_cloudflare_provider_saves(self):
		doc = self._insert(provider="Cloudflare", account_id=ACCOUNT_ID.upper(), gateway_id=" my-gw ")

		self.assertEqual(doc.name, "cloudflare")
		self.assertEqual(doc.account_id, ACCOUNT_ID)
		self.assertEqual(doc.gateway_id, "my-gw")

	def test_account_id_is_required(self):
		with self.assertRaisesRegex(frappe.ValidationError, "Account ID is required"):
			self._insert(account_id="")

	def test_a_malformed_account_id_is_rejected(self):
		for bad in ("../x", ACCOUNT_ID[:-1], ACCOUNT_ID + "/../..", "not-hex-" + ACCOUNT_ID[8:]):
			with self.subTest(account_id=bad), self.assertRaisesRegex(frappe.ValidationError, "Account ID"):
				self._insert(account_id=bad)

	def test_a_malformed_gateway_id_is_rejected(self):
		for bad in ("has space", "x\r\ny", "-x"):
			with self.subTest(gateway_id=bad), self.assertRaisesRegex(frappe.ValidationError, "Gateway ID"):
				self._insert(gateway_id=bad)

	def test_a_token_is_required_when_enabled(self):
		with self.assertRaisesRegex(frappe.ValidationError, "API token is required"):
			self._insert(api_key="")

	def test_a_disabled_provider_needs_no_token(self):
		self.assertEqual(self._insert(api_key="", enabled=0).enabled, 0)

	def test_the_token_survives_a_resave(self):
		doc = self._insert()
		doc = cast(FlowProvider, frappe.get_doc("Flow Provider", "cloudflare"))
		doc.gateway_id = "gw"
		doc.save()

		self.assertEqual(doc.get_password("api_key"), TOKEN)

	def test_the_cloudflare_fields_are_rejected_on_other_providers(self):
		for field, value in (("account_id", ACCOUNT_ID), ("gateway_id", "gw")):
			with (
				self.subTest(field=field),
				self.assertRaisesRegex(frappe.ValidationError, "only apply to the cloudflare provider"),
			):
				frappe.get_doc({"doctype": "Flow Provider", "provider": "openai", field: value}).insert()

	def test_plain_http_is_refused_outside_developer_mode(self):
		with (
			patch.dict(frappe.conf, {"developer_mode": 0}),
			self.assertRaisesRegex(frappe.ValidationError, "must use https"),
		):
			self._insert(base_url="http://127.0.0.1:9/client/v4/accounts/x/ai/v1")

	def test_plain_http_is_allowed_in_developer_mode(self):
		with patch.dict(frappe.conf, {"developer_mode": 1}):
			doc = self._insert(base_url="http://127.0.0.1:9/client/v4/accounts/x/ai/v1")
		self.assertTrue(doc.base_url)

	def test_extra_headers_are_validated(self):
		doc = self._insert(extra_params=json.dumps({"extra_headers": {"cf-aig-cache-ttl": "60"}}))
		self.assertTrue(doc.name)
		frappe.db.rollback()

		for bad in ({"authorization": "x"}, {"cf-aig-gateway-id": "other"}, {"cf-aig-ttl": "a\r\nb"}):
			with self.subTest(headers=bad), self.assertRaisesRegex(frappe.ValidationError, "Header|header"):
				self._insert(extra_params=json.dumps({"extra_headers": bad}))


class TestModelValidation(IntegrationTestCase):
	def setUp(self):
		frappe.get_doc(provider_row()).insert()
		offline_catalogue()

	def tearDown(self):
		frappe.db.rollback()

	def _insert(self, **overrides: Any) -> FlowModel:
		return cast(FlowModel, frappe.get_doc(_model(**overrides)).insert())

	def test_the_provider_link_composes_a_workers_ai_id(self):
		self.assertEqual(self._insert().model_id, f"cloudflare/{GLM}")

	def test_a_full_workers_ai_id_without_the_link_is_accepted(self):
		doc = self._insert(provider=None, model_id=f"cloudflare/{GLM}")
		self.assertEqual(doc.model_id, f"cloudflare/{GLM}")

	def test_hf_ids_are_accepted(self):
		doc = self._insert(model_id="@hf/thebloke/mistral-7b-instruct-v0.1-awq")
		self.assertEqual(doc.model_id, "cloudflare/@hf/thebloke/mistral-7b-instruct-v0.1-awq")

	def test_only_cloudflare_hosted_models_are_accepted(self):
		for bad in ("openai/gpt-4.1", "anthropic/claude-sonnet-4", "zai-org/glm-5.3-flash"):
			with (
				self.subTest(model_id=bad),
				self.assertRaisesRegex(frappe.ValidationError, "Cloudflare-hosted"),
			):
				self._insert(model_id=bad)

	def test_malformed_at_ids_are_rejected(self):
		for bad in ("@", "@@cf/x", "x@cf/y"):
			with self.subTest(model_id=bad), self.assertRaises(frappe.ValidationError):
				self._insert(model_id=bad)

	def test_the_old_workaround_id_is_rejected_on_a_new_model(self):
		with self.assertRaisesRegex(frappe.ValidationError, "use the Cloudflare provider"):
			self._insert(provider=None, model_id=f"openai/{GLM}")

	def test_an_unmigrated_row_can_still_be_edited(self):
		doc = self._insert(provider=None, model_id="openai/gpt-4o-mini", title="Legacy")
		# Seed the old shape directly: validation would refuse it today.
		frappe.db.set_value("Flow Model", doc.name, "model_id", f"openai/{GLM}")
		doc = cast(FlowModel, frappe.get_doc("Flow Model", str(doc.name)))
		doc.enabled = 0
		doc.save()

		self.assertEqual(doc.model_id, f"openai/{GLM}")

	def test_changing_an_unmigrated_rows_id_to_another_workers_ai_id_is_rejected(self):
		doc = self._insert(provider=None, model_id="openai/gpt-4o-mini", title="Legacy")
		frappe.db.set_value("Flow Model", doc.name, "model_id", f"openai/{GLM}")
		doc = cast(FlowModel, frappe.get_doc("Flow Model", str(doc.name)))
		doc.model_id = "openai/@cf/zai-org/glm-5.3"
		with self.assertRaisesRegex(frappe.ValidationError, "use the Cloudflare provider"):
			doc.save()

	def test_the_endpoint_and_token_cannot_be_set_on_the_model(self):
		with self.assertRaisesRegex(frappe.ValidationError, "once on the Cloudflare provider"):
			self._insert(api_key="test-key")
		with self.assertRaisesRegex(frappe.ValidationError, "once on the Cloudflare provider"):
			self._insert(base_url="https://example.com/ai/v1")

	def test_the_provider_must_exist_and_be_enabled(self):
		frappe.db.set_value("Flow Provider", "cloudflare", "enabled", 0)
		with self.assertRaisesRegex(frappe.ValidationError, "enable the cloudflare Flow Provider"):
			self._insert()
		# A disabled model can be saved while the provider is off.
		self.assertEqual(self._insert(enabled=0).enabled, 0)

	def test_extra_headers_in_params_are_validated(self):
		doc = self._insert(params=json.dumps({"extra_headers": {"cf-aig-skip-cache": "true"}}))
		self.assertTrue(doc.name)
		frappe.db.rollback()
		frappe.get_doc(provider_row()).insert()
		offline_catalogue()

		for bad in ({"authorization": "Bearer x"}, {"cf-aig-authorization": "x"}, {"cf-aig-a": "x\r\ny"}):
			with self.subTest(headers=bad), self.assertRaisesRegex(frappe.ValidationError, "Header|header"):
				self._insert(params=json.dumps({"extra_headers": bad}))


class TestResolver(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_a_cloudflare_model_maps_onto_the_openai_compatible_endpoint(self):
		frappe.get_doc(provider_row(gateway_id="my-gw")).insert()

		kwargs = resolve_call_kwargs(f"cloudflare/{GLM}", params={"temperature": 0.2})

		self.assertEqual(
			kwargs,
			{
				"model": f"openai/{GLM}",
				"api_key": TOKEN,
				"api_base": f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1",
				"temperature": 0.2,
				"extra_headers": {"cf-aig-gateway-id": "my-gw"},
			},
		)

	def test_the_gateway_header_is_omitted_without_a_gateway(self):
		frappe.get_doc(provider_row()).insert()

		self.assertNotIn("extra_headers", resolve_call_kwargs(f"cloudflare/{GLM}"))

	def test_a_base_url_override_replaces_the_derived_root(self):
		frappe.get_doc(provider_row(base_url="https://proxy.example.com/ai/v1/")).insert()

		self.assertEqual(
			resolve_call_kwargs(f"cloudflare/{GLM}")["api_base"], "https://proxy.example.com/ai/v1"
		)

	def test_provider_and_model_headers_merge_with_the_gateway_last(self):
		extra = {
			"extra_headers": {"cf-aig-cache-ttl": "60", "cf-aig-skip-cache": "false"},
			"temperature": 0.1,
		}
		frappe.get_doc(provider_row(gateway_id="my-gw", extra_params=json.dumps(extra))).insert()

		kwargs = resolve_call_kwargs(
			f"cloudflare/{GLM}", params={"extra_headers": {"cf-aig-skip-cache": "true"}, "max_tokens": 5}
		)

		self.assertEqual(
			kwargs["extra_headers"],
			{"cf-aig-cache-ttl": "60", "cf-aig-skip-cache": "true", "cf-aig-gateway-id": "my-gw"},
		)
		self.assertEqual((kwargs["temperature"], kwargs["max_tokens"]), (0.1, 5))

	def test_a_gateway_header_in_params_is_refused(self):
		frappe.get_doc(provider_row(gateway_id="my-gw")).insert()

		with self.assertRaises(frappe.ValidationError):
			resolve_call_kwargs(f"cloudflare/{GLM}", params={"extra_headers": {"cf-aig-gateway-id": "other"}})

	def test_plain_http_is_refused_at_call_time_outside_developer_mode(self):
		with patch.dict(frappe.conf, {"developer_mode": 1}):
			frappe.get_doc(provider_row(base_url="http://127.0.0.1:9/client/v4/accounts/x/ai/v1")).insert()

		with patch.dict(frappe.conf, {"developer_mode": 0}), self.assertRaises(frappe.ValidationError):
			resolve_call_kwargs(f"cloudflare/{GLM}")

	def test_a_missing_or_disabled_provider_is_a_clear_error(self):
		with self.assertRaisesRegex(ValueError, "cloudflare Flow Provider"):
			resolve_call_kwargs(f"cloudflare/{GLM}")

		frappe.get_doc(provider_row(enabled=0)).insert()
		with self.assertRaisesRegex(ValueError, "cloudflare Flow Provider"):
			resolve_call_kwargs(f"cloudflare/{GLM}")

	def test_cloudflare_models_take_no_credentials_of_their_own(self):
		frappe.get_doc(provider_row()).insert()

		with self.assertRaisesRegex(ValueError, "from the cloudflare Flow Provider"):
			resolve_call_kwargs(f"cloudflare/{GLM}", api_key="test-key")
		with self.assertRaisesRegex(ValueError, "from the cloudflare Flow Provider"):
			resolve_call_kwargs(f"cloudflare/{GLM}", base_url="https://example.com")

	def test_only_workers_ai_ids_resolve(self):
		frappe.get_doc(provider_row()).insert()

		with self.assertRaisesRegex(ValueError, "not a Cloudflare-hosted model"):
			resolve_call_kwargs("cloudflare/openai/gpt-4.1")

	def test_the_token_is_missing_from_a_provider_without_one(self):
		provider = SimpleNamespace(
			get_password=lambda *_args, **_kwargs: None,  # frappe-vulture:ignore
			base_url=None,
			extra_params=None,
			account_id=ACCOUNT_ID,
			gateway_id=None,
		)
		with self.assertRaisesRegex(ValueError, "no API token"):
			cloudflare.call_kwargs(cast(FlowProvider, provider), f"cloudflare/{GLM}", {})

	def test_other_providers_resolve_as_before(self):
		kwargs = resolve_call_kwargs("openai/gpt-4.1", params={"temperature": 0.3})
		self.assertEqual(kwargs, {"model": "openai/gpt-4.1", "api_key": None, "temperature": 0.3})

		kwargs = resolve_call_kwargs("ollama/llama3.1", api_key="k", base_url="http://localhost:11434")
		self.assertEqual(
			kwargs, {"model": "ollama/llama3.1", "api_key": "k", "api_base": "http://localhost:11434"}
		)

		frappe.get_doc(
			{
				"doctype": "Flow Provider",
				"provider": "openai",
				"api_key": "sk-provider",
				"base_url": "https://gateway.example.com",
				"extra_params": '{"api_version": "v1", "temperature": 0.1}',
			}
		).insert()
		self.assertEqual(
			resolve_call_kwargs("openai/gpt-4.1", params={"temperature": 0.9}),
			{
				"model": "openai/gpt-4.1",
				"api_key": "sk-provider",
				"api_base": "https://gateway.example.com",
				"api_version": "v1",
				"temperature": 0.9,
			},
		)
		self.assertEqual(
			resolve_call_kwargs("openai/gpt-4.1", api_key="sk-own", base_url="https://own.example.com")[
				"api_key"
			],
			"sk-own",
		)


class TestAgainstCloudflare(IntegrationTestCase):
	"""Real litellm against a stdlib mock of the Cloudflare endpoints."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cloudflare = cls.enterClassContext(MockCloudflare())
		cls.enterClassContext(patch.dict(frappe.conf, {"developer_mode": 1}))

	def setUp(self):
		self.cloudflare.reset()
		self.provider = self._provider()

	def tearDown(self):
		frappe.db.rollback()

	def _provider(self, **overrides: Any) -> FlowProvider:
		return cast(
			FlowProvider, frappe.get_doc(provider_row(self.cloudflare.base_url, **overrides)).insert()
		)

	def _model(self, **overrides: Any) -> Model:
		return Model(cast(FlowModel, frappe.get_doc(_model(**overrides)).insert()).name)

	def test_chat_reaches_the_account_endpoint_with_the_token_and_the_bare_model_name(self):
		response = self._model().chat("hi")

		(request,) = self.cloudflare.calls("/chat/completions")
		self.assertEqual(request["path"], f"/client/v4/accounts/{ACCOUNT_ID}/ai/v1/chat/completions")
		self.assertEqual(request["headers"]["authorization"], f"Bearer {TOKEN}")
		self.assertEqual(request["body"]["model"], GLM)
		self.assertNotIn("cf-aig-gateway-id", request["headers"])
		self.assertIsInstance(response, ChatResponse)
		self.assertEqual(response.content, "Hello from the mock")
		self.assertEqual(response.usage, {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})

	def test_the_gateway_header_is_sent_when_a_gateway_is_set(self):
		self.provider.gateway_id = "my-gw"
		self.provider.save()

		self._model().chat("hi")

		(request,) = self.cloudflare.calls("/chat/completions")
		self.assertEqual(request["headers"]["cf-aig-gateway-id"], "my-gw")

	def test_gateway_controls_from_the_provider_and_the_model_reach_cloudflare(self):
		self.provider.extra_params = json.dumps({"extra_headers": {"cf-aig-cache-ttl": "60"}})
		self.provider.save()
		model = self._model(params=json.dumps({"extra_headers": {"cf-aig-collect-log-payload": "false"}}))

		model.chat("hi")

		(request,) = self.cloudflare.calls("/chat/completions")
		self.assertEqual(request["headers"]["cf-aig-cache-ttl"], "60")
		self.assertEqual(request["headers"]["cf-aig-collect-log-payload"], "false")

	def test_reasoning_effort_and_other_params_are_forwarded(self):
		self._model(params=json.dumps({"reasoning_effort": "high", "max_tokens": 50})).chat("hi")

		(request,) = self.cloudflare.calls("/chat/completions")
		self.assertEqual(request["body"]["reasoning_effort"], "high")
		self.assertEqual(request["body"]["max_tokens"], 50)

	def test_a_tool_call_comes_back_as_a_tool_call(self):
		tools = [{"type": "function", "function": {"name": "add", "parameters": {"type": "object"}}}]

		response = self._model().chat("please use-tool", tools=tools)

		self.assertEqual(response.finish_reason, "tool_calls")
		(call,) = response.tool_calls
		self.assertIsInstance(call, ToolCall)
		self.assertEqual((call.id, call.name, call.arguments), ("call_1", "add", {"a": 1, "b": 2}))
		(request,) = self.cloudflare.calls("/chat/completions")
		self.assertEqual(request["body"]["tools"], tools)

	def test_streaming_yields_text_and_reports_usage(self):
		yielded, response = _drain(self._model().chat("hi", stream=True))

		self.assertEqual(yielded, ["Hello", " from the mock"])
		self.assertEqual(response.content, "Hello from the mock")
		self.assertEqual(response.usage, {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
		(request,) = self.cloudflare.calls("/chat/completions")
		self.assertEqual(request["body"]["stream_options"], {"include_usage": True})

	def test_streaming_assembles_a_tool_call_split_across_chunks(self):
		tools = [{"type": "function", "function": {"name": "add", "parameters": {"type": "object"}}}]

		yielded, response = _drain(self._model().chat("use-tool", tools=tools, stream=True))

		self.assertEqual(yielded, [ToolCallBegin(id="call_1", name="add")])
		(call,) = response.tool_calls
		self.assertEqual(call.arguments, {"a": 1, "b": 2})
		self.assertEqual(response.usage["total_tokens"], 18)

	def test_a_model_that_rejects_stream_options_fails_the_stream(self):
		# The panel streams with include_usage on every call. If Workers AI ever rejects it for a
		# model, this is what the user would see: a per-provider switch would then be needed.
		self.cloudflare.reject_stream_options = True

		with self.assertRaises(litellm.BadRequestError):
			_drain(self._model().chat("hi", stream=True))

		(request,) = self.cloudflare.calls("/chat/completions")
		self.assertIn("stream_options", request["body"])

	def test_test_connection_pings_the_model_through_the_provider(self):
		model = cast(FlowModel, frappe.get_doc(_model()).insert())

		self.assertTrue(model.test_connection()["ok"])

		(request,) = self.cloudflare.calls("/chat/completions")
		self.assertEqual(request["body"]["max_tokens"], 1)
		self.assertEqual(request["body"]["model"], GLM)

	def test_an_unknown_gateway_is_reported(self):
		self.provider.gateway_id = "boom"
		self.provider.save()
		model = cast(FlowModel, frappe.get_doc(_model()).insert())

		with self.assertRaisesRegex(frappe.ValidationError, "Unknown gateway"):
			model.test_connection()

	def test_a_rejected_token_is_reported_without_leaking_it(self):
		self.provider.api_key = "wrong-token"
		self.provider.save()
		model = cast(FlowModel, frappe.get_doc(_model()).insert())

		with self.assertRaises(frappe.ValidationError):
			model.test_connection()

		self.assertTrue(self.cloudflare.calls("/chat/completions"))
		shown = str(frappe.message_log)
		self.assertIn("Authentication error", shown)
		self.assertNotIn("wrong-token", shown)

	def test_embeddings_go_through_the_same_provider(self):
		embedding_model = cast(
			FlowModel,
			frappe.get_doc(_model(title="Cloudflare Embed", model_id="@cf/baai/bge-m3")).insert(),
		)

		self.assertEqual(embedder.probe_dimension(str(embedding_model.name)), 4)

		(request,) = self.cloudflare.calls("/embeddings")
		self.assertEqual(request["path"], f"/client/v4/accounts/{ACCOUNT_ID}/ai/v1/embeddings")
		self.assertEqual(request["headers"]["authorization"], f"Bearer {TOKEN}")
		self.assertEqual(request["body"]["model"], "@cf/baai/bge-m3")

	def test_embeddings_in_a_batch_keep_their_order(self):
		embedding_model = cast(
			FlowModel,
			frappe.get_doc(_model(title="Cloudflare Embed", model_id="@cf/baai/bge-m3")).insert(),
		)
		frappe.db.set_single_value("Flow Knowledge Settings", "embedding_model", embedding_model.name)
		frappe.clear_document_cache("Flow Knowledge Settings", "Flow Knowledge Settings")

		vectors = embedder.embed_texts(["a", "b", "c"])

		self.assertEqual([v[0] for v in vectors], [0.0, 1.0, 2.0])

	def test_an_embedding_model_without_a_cloudflare_provider_reports_it(self):
		embedding_model = cast(
			FlowModel,
			frappe.get_doc(_model(title="Cloudflare Embed", model_id="@cf/baai/bge-m3")).insert(),
		)
		frappe.db.set_value("Flow Provider", "cloudflare", "enabled", 0)

		with self.assertRaisesRegex(frappe.ValidationError, "enable the cloudflare Flow Provider"):
			embedder.probe_dimension(str(embedding_model.name))
