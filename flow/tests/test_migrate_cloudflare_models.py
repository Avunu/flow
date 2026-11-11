# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import json
from typing import Any, cast
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import get_decrypted_password

from flow.flow.doctype.flow_model.flow_model import FlowModel
from flow.lib.model import Model
from flow.patches import migrate_cloudflare_models
from flow.tests.cloudflare_mock import ACCOUNT_ID, MockCloudflare, offline_catalogue, provider_row

OTHER_ACCOUNT = "fedcba9876543210fedcba9876543210"
API_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1"
OLD_ID = "openai/@cf/zai-org/glm-5.3-flash"
NEW_ID = "cloudflare/@cf/zai-org/glm-5.3-flash"
PROVIDER_TOKEN = "test-provider-token"
MODEL_TOKEN = "test-model-token"


def _seed_provider(*, name: str = "Cloudflare", token: str | None = PROVIDER_TOKEN, **values: Any) -> None:
	"""The old row: named with a capital C, a base URL and no account id."""
	frappe.get_doc({**provider_row(api_key=token or ""), "enabled": 1, **values}).insert()
	frappe.db.sql(
		"update `tabFlow Provider` set name = %s, provider = %s, account_id = %s, base_url = %s where name = 'cloudflare'",
		(name, name, values.get("account_id"), values.get("base_url", API_URL)),
	)
	frappe.db.sql("update `__Auth` set name = %s where doctype = 'Flow Provider'", (name,))


def _seed_model(
	title: str = "GLM 5.3",
	*,
	token: str | None = MODEL_TOKEN,
	model_id: str = OLD_ID,
	base_url: str | None = API_URL,
	**values: Any,
) -> str:
	"""A row as the workaround left it. Validation refuses that shape today, so write it directly."""
	doc = cast(
		FlowModel,
		frappe.get_doc(
			{
				"doctype": "Flow Model",
				"title": title,
				"model_id": "openai/gpt-4o-mini",
				"api_key": token or "",
			}
		).insert(),
	)
	frappe.db.set_value(
		"Flow Model",
		doc.name,
		{"model_id": model_id, "base_url": base_url, "context_window": 1048576, **values},
	)
	return str(doc.name)


def _model_state(name: str) -> dict[str, Any]:
	row = frappe.db.get_value(
		"Flow Model", name, ["provider", "model_id", "base_url", "context_window", "modified"], as_dict=True
	)
	return {**row, "token": get_decrypted_password("Flow Model", name, "api_key", raise_exception=False)}


def _provider_state() -> dict[str, Any] | None:
	row = frappe.db.get_value(
		"Flow Provider",
		{"provider": ("like", "cloudflare")},
		["name", "provider", "account_id", "gateway_id", "base_url", "enabled"],
		as_dict=True,
	)
	if not row:
		return None
	token = get_decrypted_password("Flow Provider", row.name, "api_key", raise_exception=False)
	return {**row, "token": token}


class _MigrationCase(IntegrationTestCase):
	def setUp(self):
		self.logged: list[str] = []
		patcher = patch.object(migrate_cloudflare_models, "_log", self.logged.append)
		patcher.start()
		self.addCleanup(patcher.stop)

	def tearDown(self):
		frappe.db.rollback()

	def migrate(self) -> None:
		migrate_cloudflare_models.execute()

	def assertNoSecretsLogged(self) -> None:
		text = "\n".join(self.logged)
		for secret in (PROVIDER_TOKEN, MODEL_TOKEN):
			self.assertNotIn(secret, text)


class TestTheUsersRows(_MigrationCase):
	def setUp(self):
		super().setUp()
		# The same token on both rows: the model was given a copy of the provider's.
		_seed_provider(token=PROVIDER_TOKEN)
		self.name = _seed_model(token=PROVIDER_TOKEN)
		self.before = _model_state(self.name)

	def test_the_provider_and_the_model_end_up_as_designed(self):
		self.migrate()

		self.assertEqual(
			_provider_state(),
			{
				"name": "cloudflare",
				"provider": "cloudflare",
				"account_id": ACCOUNT_ID,
				"gateway_id": None,
				"base_url": None,
				"enabled": 1,
				"token": PROVIDER_TOKEN,
			},
		)
		after = _model_state(self.name)
		self.assertEqual(after["provider"], "cloudflare")
		self.assertEqual(after["model_id"], NEW_ID)
		self.assertIsNone(after["base_url"])
		self.assertIsNone(after["token"])
		# Kept: the name, the hand-set window and the modified timestamp (it is a data fix).
		self.assertEqual(self.name, "GLM 5.3")
		self.assertEqual(after["context_window"], 1048576)
		self.assertEqual(after["modified"], self.before["modified"])
		# The built-in Flow agent was pointed at the model when it was created.
		self.assertEqual(frappe.db.get_value("Flow Agent", "Flow", "model"), "GLM 5.3")
		self.assertTrue(frappe.db.exists("Flow Provider", "cloudflare"))

	def test_the_migrated_model_is_valid_and_resolves_through_the_provider(self):
		self.migrate()
		offline_catalogue()

		doc = cast(FlowModel, frappe.get_doc("Flow Model", self.name))
		doc.save()

		self.assertEqual((doc.model_id, doc.context_window), (NEW_ID, 1048576))
		kwargs = Model(self.name).call_kwargs
		self.assertEqual(kwargs["model"], "openai/@cf/zai-org/glm-5.3-flash")
		self.assertEqual(kwargs["api_base"], API_URL)
		self.assertEqual(kwargs["api_key"], PROVIDER_TOKEN)

	def test_running_it_twice_changes_nothing_more(self):
		self.migrate()
		provider, model = _provider_state(), _model_state(self.name)
		self.logged.clear()

		self.migrate()

		self.assertEqual((_provider_state(), _model_state(self.name)), (provider, model))
		self.assertEqual(self.logged, [])

	def test_it_logs_names_and_never_a_token(self):
		self.migrate()

		self.assertIn("'GLM 5.3'", "\n".join(self.logged))
		self.assertNoSecretsLogged()

	def test_it_reads_the_database_only(self):
		with patch("requests.get") as get, patch("litellm.completion") as completion:
			self.migrate()

		get.assert_not_called()
		completion.assert_not_called()


class TestAgainstAMock(IntegrationTestCase):
	def test_a_migrated_model_calls_cloudflare_through_the_provider(self):
		_seed_provider(token="test-token")
		name = _seed_model(token="test-token")
		with MockCloudflare() as mock, patch.dict(frappe.conf, {"developer_mode": 1}):
			migrate_cloudflare_models.execute()
			frappe.db.set_value("Flow Provider", "cloudflare", "base_url", mock.base_url)

			response = Model(name).chat("hi")

			(request,) = mock.calls("/chat/completions")
			self.assertEqual(request["body"]["model"], "@cf/zai-org/glm-5.3-flash")
			self.assertEqual(request["headers"]["authorization"], "Bearer test-token")
			self.assertEqual(response.content, "Hello from the mock")
		frappe.db.rollback()


class TestOtherShapes(_MigrationCase):
	def test_with_no_provider_row_one_is_created_from_the_model(self):
		name = _seed_model(token=MODEL_TOKEN)

		self.migrate()

		self.assertEqual(
			_provider_state(),
			{
				"name": "cloudflare",
				"provider": "cloudflare",
				"account_id": ACCOUNT_ID,
				"gateway_id": None,
				"base_url": None,
				"enabled": 1,
				"token": MODEL_TOKEN,
			},
		)
		self.assertEqual(_model_state(name)["model_id"], NEW_ID)

	def test_with_no_token_anywhere_the_provider_is_created_switched_off_and_the_log_says_so(self):
		name = _seed_model(token=None)

		self.migrate()

		provider = _provider_state()
		assert provider
		self.assertEqual((provider["enabled"], provider["token"]), (0, None))
		self.assertEqual(_model_state(name)["model_id"], NEW_ID)
		self.assertIn("no API token", "\n".join(self.logged))

	def test_a_gateway_url_gives_the_gateway_id(self):
		url = f"https://gateway.ai.cloudflare.com/v1/{ACCOUNT_ID}/my-gateway/workers-ai"
		_seed_model(token=MODEL_TOKEN, base_url=url)

		self.migrate()

		provider = _provider_state()
		assert provider
		self.assertEqual((provider["account_id"], provider["gateway_id"]), (ACCOUNT_ID, "my-gateway"))

	def test_the_older_run_url_is_recognised(self):
		name = _seed_model(token=MODEL_TOKEN, base_url=API_URL.replace("/ai/v1", "/ai/run/"))

		self.migrate()

		self.assertEqual(_model_state(name)["provider"], "cloudflare")

	def test_a_row_with_a_different_token_is_left_alone_and_logged_by_name(self):
		_seed_provider(token=PROVIDER_TOKEN)
		same = _seed_model("Same Token", token=PROVIDER_TOKEN)
		different = _seed_model("Other Token", token=MODEL_TOKEN)
		before = _model_state(different)

		self.migrate()

		self.assertEqual(_model_state(same)["model_id"], NEW_ID)
		self.assertEqual(_model_state(different), before)
		self.assertEqual(_model_state(different)["token"], MODEL_TOKEN)
		provider = _provider_state()
		assert provider
		self.assertEqual(provider["token"], PROVIDER_TOKEN)
		self.assertIn("'Other Token'", "\n".join(self.logged))
		self.assertNoSecretsLogged()

	def test_when_every_row_differs_nothing_is_touched(self):
		_seed_provider(token=PROVIDER_TOKEN)
		name = _seed_model(token=MODEL_TOKEN)
		provider, model = _provider_state(), _model_state(name)

		self.migrate()

		self.assertEqual((_provider_state(), _model_state(name)), (provider, model))

	def test_a_second_account_is_left_alone_and_logged_by_name(self):
		_seed_provider(token=PROVIDER_TOKEN)
		home = _seed_model("Home", token=PROVIDER_TOKEN)
		away = _seed_model("Away", token=PROVIDER_TOKEN, base_url=API_URL.replace(ACCOUNT_ID, OTHER_ACCOUNT))
		before = _model_state(away)

		self.migrate()

		self.assertEqual(_model_state(home)["model_id"], NEW_ID)
		self.assertEqual(_model_state(away), before)
		provider = _provider_state()
		assert provider
		self.assertEqual(provider["account_id"], ACCOUNT_ID)
		self.assertIn("'Away'", "\n".join(self.logged))
		self.assertNoSecretsLogged()

	def test_without_a_provider_the_most_used_account_wins(self):
		_seed_model("One", token=MODEL_TOKEN)
		_seed_model("Two", token=MODEL_TOKEN)
		other = _seed_model("Three", token=MODEL_TOKEN, base_url=API_URL.replace(ACCOUNT_ID, OTHER_ACCOUNT))

		self.migrate()

		provider = _provider_state()
		assert provider
		self.assertEqual(provider["account_id"], ACCOUNT_ID)
		self.assertEqual(_model_state(other)["provider"], None)

	def test_a_provider_set_up_for_another_account_is_not_overwritten(self):
		frappe.get_doc(provider_row(account_id=OTHER_ACCOUNT)).insert()
		name = _seed_model(token=MODEL_TOKEN)
		provider, model = _provider_state(), _model_state(name)

		self.migrate()

		self.assertEqual((_provider_state(), _model_state(name)), (provider, model))
		self.assertIn("none uses the account", "\n".join(self.logged))

	def test_a_provider_already_set_up_keeps_its_gateway_and_custom_endpoint(self):
		frappe.get_doc(
			provider_row(
				gateway_id="mine", base_url="https://proxy.example.com/ai/v1", api_key=PROVIDER_TOKEN
			)
		).insert()
		name = _seed_model(token=PROVIDER_TOKEN)

		self.migrate()

		provider = _provider_state()
		assert provider
		self.assertEqual(
			(provider["gateway_id"], provider["base_url"]), ("mine", "https://proxy.example.com/ai/v1")
		)
		self.assertEqual(_model_state(name)["model_id"], NEW_ID)

	def test_a_disabled_provider_is_switched_on_for_the_models_it_takes_over(self):
		_seed_provider(token=PROVIDER_TOKEN, enabled=0)
		_seed_model(token=PROVIDER_TOKEN)

		self.migrate()

		provider = _provider_state()
		assert provider
		self.assertEqual(provider["enabled"], 1)

	def test_a_row_with_its_own_extra_headers_is_left_alone(self):
		name = _seed_model(
			token=MODEL_TOKEN, params=json.dumps({"extra_headers": {"cf-aig-cache-ttl": "60"}})
		)
		before = _model_state(name)

		self.migrate()

		self.assertEqual(_model_state(name), before)
		self.assertIsNone(_provider_state())
		self.assertIn("'GLM 5.3'", "\n".join(self.logged))

	def test_other_params_do_not_stop_the_migration(self):
		name = _seed_model(token=MODEL_TOKEN, params=json.dumps({"reasoning_effort": "high"}))

		self.migrate()

		self.assertEqual(_model_state(name)["model_id"], NEW_ID)

	def test_other_models_are_untouched(self):
		openai = _seed_model("Proxy", token=MODEL_TOKEN, model_id="openai/gpt-4o-mini", base_url=API_URL)
		ollama = _seed_model(
			"Local", token=None, model_id="ollama/llama3.1", base_url="http://localhost:11434"
		)
		plain = _seed_model("Plain", token=MODEL_TOKEN, model_id="openai/gpt-4o-mini", base_url=None)
		states = {n: _model_state(n) for n in (openai, ollama, plain)}

		self.migrate()

		self.assertEqual({n: _model_state(n) for n in states}, states)
		self.assertIsNone(_provider_state())

	def test_models_already_on_the_provider_are_left_alone(self):
		frappe.get_doc(provider_row()).insert()
		frappe.get_doc(
			{
				"doctype": "Flow Model",
				"title": "Done",
				"provider": "cloudflare",
				"model_id": "@cf/zai-org/glm-5.3-flash",
				"context_window": 4096,
			}
		).insert()
		offline_catalogue()
		before = _model_state("Done")

		self.migrate()

		self.assertEqual(_model_state("Done"), before)
		self.assertEqual(self.logged, [])
