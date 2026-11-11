# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""Move the old Workers AI workaround onto the cloudflare Flow Provider.

Cloudflare models used to be `openai/@cf/...` rows with their own Base URL and API key. They now
use the one `cloudflare` provider (account id, optional gateway id, token) and a `cloudflare/@cf/...`
id. This reads and writes the database only: no network, no document hooks, and nothing secret is
logged (rows are named, never their tokens).

A row is left exactly as it was, and logged, when it can't be moved without guessing: a different
account or gateway than the site's, a token different from the provider's, or its own extra headers.
Those rows keep working until their id is edited. Idempotent: a moved row no longer matches.
"""

import json
import re
from collections import defaultdict
from typing import TYPE_CHECKING, cast

import frappe
from frappe.utils.password import get_decrypted_password, remove_encrypted_password, set_encrypted_password

from flow.lib import cloudflare

PROVIDER = cloudflare.PROVIDER

# `.../ai/v1` (or the older `/ai/run`) on the API, and the Workers AI route of an AI Gateway.
API_URL = re.compile(r"https://api\.cloudflare\.com/client/v4/accounts/([0-9a-f]{32})/ai/(?:v1|run)/?")
GATEWAY_URL = re.compile(
	r"https://gateway\.ai\.cloudflare\.com/v1/([0-9a-f]{32})/([A-Za-z0-9][A-Za-z0-9_-]{0,63})"
	r"/(?:workers-ai|workersai)(?:/v1)?/?"
)

if TYPE_CHECKING:
	from flow.flow.doctype.flow_provider.flow_provider import FlowProvider

Endpoint = tuple[str, str]  # (account id, gateway id or "")


def _log(message: str) -> None:
	frappe.logger("flow").info("migrate_cloudflare_models: %s", message)


def _endpoint(url: str | None) -> Endpoint | None:
	url = (url or "").strip()
	if match := API_URL.fullmatch(url):
		return match.group(1), ""
	if match := GATEWAY_URL.fullmatch(url):
		return match.group(1), match.group(2)
	return None


def _new_model_id(model_id: str | None) -> str | None:
	"""`cloudflare/@cf/...` for an id the workaround used (`openai/@cf/...`), else None."""
	model_id = (model_id or "").strip()
	for prefix in ("openai/", f"{PROVIDER}/"):
		if model_id.startswith(prefix) and cloudflare.is_hosted_model(model_id.removeprefix(prefix)):
			return f"{PROVIDER}/{model_id.removeprefix(prefix)}"
	return None


def _sets_extra_headers(params: str | None) -> bool:
	try:
		return "extra_headers" in (json.loads(params) if params else {})
	except TypeError, ValueError:
		return False


def _find_provider() -> str | None:
	"""The Cloudflare provider row, whatever its capitalisation (an old one is named `Cloudflare`)."""
	names = frappe.db.sql_list("select name from `tabFlow Provider` where lower(name) = %s", (PROVIDER,))
	return PROVIDER if PROVIDER in names else (names[0] if names else None)


def _candidates() -> dict[str, dict]:
	"""Workaround rows by name: the endpoint in their Base URL and the id they should have."""
	rows = frappe.get_all(
		"Flow Model",
		filters={"base_url": ("like", "%cloudflare.com/%")},
		fields=["name", "model_id", "base_url", "provider", "params"],
	)
	found: dict[str, dict] = {}
	for row in rows:
		endpoint = _endpoint(row.base_url)
		new_id = _new_model_id(row.model_id)
		if endpoint is None or new_id is None or (row.provider or PROVIDER).lower() != PROVIDER:
			continue
		if _sets_extra_headers(row.params):
			_log(f"left {row.name!r} alone: it sets its own extra_headers")
			continue
		found[row.name] = {"endpoint": endpoint, "model_id": new_id}
	return found


def _choose_endpoint(candidates: dict[str, dict], provider_account: str, provider_gateway: str) -> Endpoint:
	"""The one account and gateway the site keeps: the provider's if it has one, else the most used."""
	groups: dict[Endpoint, list[str]] = defaultdict(list)
	for name, candidate in candidates.items():
		groups[candidate["endpoint"]].append(name)
	return min(
		groups,
		key=lambda endpoint: (
			endpoint[0] != provider_account,
			endpoint[1] != provider_gateway,
			-len(groups[endpoint]),
			endpoint,
		),
	)


def _lowercase_provider(name: str) -> None:
	"""Name the row `cloudflare`, as the autoname does. SQL, because a rename that only changes the
	case isn't reliable through the ORM, and the token lives in `__Auth` under the old name."""
	if name == PROVIDER:
		return
	frappe.db.sql(
		"update `tabFlow Provider` set name = %s, provider = %s where name = %s", (PROVIDER, PROVIDER, name)
	)
	frappe.db.sql(
		"update `__Auth` set name = %s where doctype = 'Flow Provider' and name = %s", (PROVIDER, name)
	)
	frappe.db.sql("update `tabFlow Model` set provider = %s where provider = %s", (PROVIDER, name))
	frappe.clear_document_cache("Flow Provider", name)


def _create_provider(*, enabled: bool) -> None:
	doc = cast("FlowProvider", frappe.new_doc("Flow Provider"))
	doc.provider = PROVIDER
	doc.enabled = int(enabled)
	doc.set_new_name()
	doc.set_user_and_timestamp()
	doc.db_insert()


def _set_up_provider(endpoint: Endpoint, token: str | None, *, was_legacy_url: bool) -> None:
	account, gateway = endpoint
	values: dict = {"account_id": account}
	if token:
		values["enabled"] = 1  # the models it takes over were working, so it must be on
	if gateway and not frappe.db.get_value("Flow Provider", PROVIDER, "gateway_id"):
		values["gateway_id"] = gateway
	if was_legacy_url:
		values["base_url"] = None  # now derived from the account id
	frappe.db.set_value("Flow Provider", PROVIDER, values, update_modified=False)
	if token and not get_decrypted_password("Flow Provider", PROVIDER, "api_key", raise_exception=False):
		set_encrypted_password("Flow Provider", PROVIDER, token, "api_key")


def execute():
	candidates = _candidates()
	if not candidates:
		return

	provider = _find_provider()
	existing = (
		frappe.db.get_value("Flow Provider", provider, ["account_id", "gateway_id", "base_url"], as_dict=True)
		if provider
		else None
	)
	account = (existing.account_id if existing else None) or ""
	if existing and not account and (endpoint := _endpoint(existing.base_url)):
		account = endpoint[0]
	gateway = (existing.gateway_id if existing else None) or ""

	chosen = _choose_endpoint(candidates, account, gateway)
	if account and chosen[0] != account:
		_log("left every row alone: none uses the account the cloudflare provider is set up for")
		return

	token = (
		get_decrypted_password("Flow Provider", provider, "api_key", raise_exception=False)
		if provider
		else None
	)
	tokens = {
		name: get_decrypted_password("Flow Model", name, "api_key", raise_exception=False)
		for name in sorted(candidates)
		if candidates[name]["endpoint"] == chosen
	}
	token = token or next((t for t in tokens.values() if t), None)

	moving = []
	for name in sorted(candidates):
		if candidates[name]["endpoint"] != chosen:
			_log(f"left {name!r} alone: it uses another Cloudflare account or gateway")
		elif tokens[name] and tokens[name] != token:
			_log(f"left {name!r} alone: its token differs from the cloudflare provider's")
		else:
			moving.append(name)
	if not moving:
		return

	if provider:
		_lowercase_provider(provider)
	else:
		_create_provider(enabled=bool(token))
	_set_up_provider(chosen, token, was_legacy_url=bool(existing and _endpoint(existing.base_url)))

	for name in moving:
		frappe.db.set_value(
			"Flow Model",
			name,
			{
				"provider": PROVIDER,
				"model_id": candidates[name]["model_id"],
				"base_url": None,
				"api_key": None,
			},
			update_modified=False,
		)
		remove_encrypted_password("Flow Model", name, "api_key")
	_log(
		f"moved {len(moving)} Flow Model(s) to the cloudflare provider: {', '.join(repr(n) for n in moving)}"
	)
	if not token:
		_log("the cloudflare provider has no API token: set one before using Cloudflare models")
