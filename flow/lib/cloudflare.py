# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""Cloudflare Workers AI: input validation and the litellm call arguments.

One Flow Provider named `cloudflare` holds the account id, the optional AI Gateway id and the API
token for the whole site. Flow talks to Cloudflare's OpenAI-compatible endpoint itself and tells
litellm `openai/<model>`, so it does not depend on litellm's own `cloudflare` provider route. That
route rejects `reasoning_effort` (mandatory on the GLM 5.3 family) and has no embeddings.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import frappe
from frappe import _

if TYPE_CHECKING:
	from flow.flow.doctype.flow_provider.flow_provider import FlowProvider

PROVIDER = "cloudflare"
# The Workers AI id prefixes. Flow supports Cloudflare-hosted models only.
HOSTED_PREFIXES = ("@cf/", "@hf/")
API_ROOT = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"

# The account id is interpolated into a URL path and the gateway id becomes a header value, so both
# are locked down to what Cloudflare issues: this also rules out path traversal and CR/LF injection.
ACCOUNT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
GATEWAY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

HEADER_NAME_PATTERN = re.compile(r"^cf-aig-[a-z0-9-]+$")
# Flow sets these from fields; they can't come from JSON.
DENIED_HEADERS = frozenset({"cf-aig-authorization", "cf-aig-gateway-id"})
GATEWAY_HEADER = "cf-aig-gateway-id"
MAX_HEADER_VALUE_LENGTH = 1024


def is_hosted_model(model_part: str) -> bool:
	"""Whether `model_part` (the id after `cloudflare/`) is a Cloudflare-hosted Workers AI model."""
	return model_part.startswith(HOSTED_PREFIXES)


def strip_provider(model_id: str) -> str:
	return model_id.removeprefix(f"{PROVIDER}/")


def validate_account_id(value: str | None) -> str:
	account_id = (value or "").strip().lower()
	if not ACCOUNT_ID_PATTERN.fullmatch(account_id):
		frappe.throw(
			_("Account ID must be the 32-character hexadecimal id of your Cloudflare account."),
			title=_("Invalid Account ID"),
		)
	return account_id


def validate_gateway_id(value: str | None) -> str:
	gateway_id = (value or "").strip()
	if gateway_id and not GATEWAY_ID_PATTERN.fullmatch(gateway_id):
		frappe.throw(
			_("Gateway ID may only contain letters, digits, dashes and underscores (up to 64 characters)."),
			title=_("Invalid Gateway ID"),
		)
	return gateway_id


def validate_extra_headers(value: object) -> dict[str, str]:
	"""Check the `extra_headers` JSON of a Cloudflare provider or model; return it lowercased.

	Only `cf-aig-*` gateway controls (cache TTL, retries, metadata) are allowed, never credentials,
	the gateway selector (set from the provider's field), Host or anything else on the request."""
	if value is None:
		return {}
	if not isinstance(value, dict):
		frappe.throw(_("extra_headers must be a JSON object."), title=_("Invalid Headers"))

	headers: dict[str, str] = {}
	for raw_name, raw_value in value.items():
		name = str(raw_name).lower()
		if not HEADER_NAME_PATTERN.fullmatch(name) or name in DENIED_HEADERS:
			frappe.throw(
				_("Header {0} is not allowed. Only cf-aig-* gateway controls can be set, not {1}.").format(
					frappe.bold(str(raw_name)), ", ".join(sorted(DENIED_HEADERS))
				),
				title=_("Invalid Headers"),
			)
		if (
			not isinstance(raw_value, str)
			or len(raw_value) > MAX_HEADER_VALUE_LENGTH
			or any(ord(char) < 32 or ord(char) == 127 for char in raw_value)
		):
			frappe.throw(
				_(
					"The value of header {0} must be a string of at most {1} characters without control characters or line breaks."
				).format(frappe.bold(str(raw_name)), MAX_HEADER_VALUE_LENGTH),
				title=_("Invalid Headers"),
			)
		headers[name] = raw_value
	return headers


def check_base_url(base_url: str | None) -> None:
	"""The token goes wherever the base URL points, so plain http is for developer mode only."""
	if base_url and base_url.lower().startswith("http://") and not frappe.conf.developer_mode:
		frappe.throw(
			_("Base URL must use https: the Cloudflare API token is sent to it."), title=_("Invalid Base URL")
		)


def api_root(provider: FlowProvider) -> str:
	"""The OpenAI-compatible root, `<...>/ai/v1`: the Base URL override, else Cloudflare's own."""
	if provider.base_url:
		return provider.base_url.rstrip("/")
	return API_ROOT.format(account_id=validate_account_id(provider.account_id))


def _json_object(raw: str | None) -> dict[str, Any]:
	parsed = json.loads(raw) if raw else {}
	return parsed if isinstance(parsed, dict) else {}


def call_kwargs(provider: FlowProvider, model_id: str, params: dict[str, Any]) -> dict[str, Any]:
	"""litellm arguments for a `cloudflare/@cf/...` model, with its credentials from the provider row.

	`params` are the model's own; the provider's `extra_params` sit under them, except `extra_headers`,
	which merge (the model's win) and always end with the gateway selector."""
	model_part = strip_provider(model_id)
	if not is_hosted_model(model_part):
		raise ValueError(
			f"{model_id!r} is not a Cloudflare-hosted model: Workers AI ids start with {' or '.join(HOSTED_PREFIXES)}"
		)
	token = provider.get_password("api_key", raise_exception=False)
	if not token:
		raise ValueError("The cloudflare Flow Provider has no API token")
	check_base_url(provider.base_url)

	merged = {**_json_object(provider.extra_params), **params}
	headers = {
		**validate_extra_headers(_json_object(provider.extra_params).get("extra_headers")),
		**validate_extra_headers(params.get("extra_headers")),
	}
	gateway_id = validate_gateway_id(provider.gateway_id)
	if gateway_id:
		headers[GATEWAY_HEADER] = gateway_id
	merged.pop("extra_headers", None)

	kwargs: dict[str, Any] = {
		**merged,
		"model": f"openai/{model_part}",
		"api_key": token,
		"api_base": api_root(provider),
	}
	if headers:
		kwargs["extra_headers"] = headers
	return kwargs


def redact(text: str, secret: str | None) -> str:
	"""`text` without the secret, for error messages that may echo request details."""
	return text.replace(secret, "***") if secret else text


TOKEN_HINT = "Check the API token: it needs Account > Workers AI > Read, and AI Gateway > Run when the gateway is authenticated."
STATUS_HINTS = {
	401: TOKEN_HINT,
	403: TOKEN_HINT,
	404: "Check the Account ID and the model id.",
}


def _envelope_message(response: Any) -> str | None:
	"""The first message of Cloudflare's `{"errors": [...]}` envelope: litellm keeps only the status."""
	try:
		errors = response.json()["errors"]
		return f"{errors[0]['message']} (code {errors[0]['code']})"
	except Exception:
		return None


def explain_error(error: Exception, token: str | None) -> str:
	"""What to show for a failed Cloudflare call: Cloudflare's own message and a hint for the usual
	causes, with the token scrubbed from whatever the client echoed."""
	text = _envelope_message(getattr(error, "response", None)) or str(error) or type(error).__name__
	status = getattr(error, "status_code", None)
	hint = STATUS_HINTS.get(status) if isinstance(status, int) else None
	return redact(f"{text} {hint}" if hint else text, token)[:500]
