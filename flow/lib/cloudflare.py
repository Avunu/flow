# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""Cloudflare Workers AI: input validation, the litellm call arguments and the model catalogue.

One Flow Provider named `cloudflare` holds the account id, the optional AI Gateway id and the API
token for the whole site. Flow talks to Cloudflare's OpenAI-compatible endpoint itself and tells
litellm `openai/<model>`, so it does not depend on litellm's own `cloudflare` provider route. That
route rejects `reasoning_effort` (mandatory on the GLM 5.3 family) and has no embeddings.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, cast

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime

from flow.utils.errors import throw

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
		throw(
			_("Account ID must be the 32-character hexadecimal id of your Cloudflare account."),
			title=_("Invalid Account ID"),
		)
	return account_id


def validate_gateway_id(value: str | None) -> str:
	gateway_id = (value or "").strip()
	if gateway_id and not GATEWAY_ID_PATTERN.fullmatch(gateway_id):
		throw(
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
		throw(_("extra_headers must be a JSON object."), title=_("Invalid Headers"))

	headers: dict[str, str] = {}
	for raw_name, raw_value in value.items():
		name = str(raw_name).lower()
		if not HEADER_NAME_PATTERN.fullmatch(name) or name in DENIED_HEADERS:
			throw(
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
			throw(
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
		throw(
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


# --- Model catalogue -------------------------------------------------------------------------
#
# `GET /accounts/{account}/ai/models/search` lists the models the account can use. The native shape
# carries the task and a `properties` list whose values are all strings; `format=openrouter` is the
# cleaner source for limits, features and deprecation. Flow reads both and lets the second win.

CACHE_KEY = "flow:cloudflare_models"
ERROR_KEY = "flow:cloudflare_models_error"
CACHE_TTL = 24 * 60 * 60
# After a failed fetch, don't try again for a few minutes: saving a model must not wait on an outage.
ERROR_TTL = 5 * 60
REQUEST_TIMEOUT = 10
MAX_PAGES = 20
PER_PAGE = 100
DEPRECATION_WARNING_DAYS = 60
KINDS = {"Text Generation": "generation", "Text Embeddings": "embedding"}


class CatalogueError(Exception):
	"""The catalogue could not be read; the message is safe to show (it never holds the token)."""


@dataclass(frozen=True)
class CatalogueEntry:
	id: str
	kind: str
	description: str = ""
	context_window: int | None = None
	max_output: int | None = None
	tools: bool = False
	reasoning: bool = False
	vision: bool = False
	deprecation_date: str | None = None
	supported_efforts: tuple[str, ...] = ()
	mandatory_effort: bool = False


def _cache():
	"""The site cache, which frappe.init sets and the framework types as optional."""
	cache = frappe.cache
	if cache is None:
		raise RuntimeError("frappe.init has not run: there is no site cache")
	return cache


def _int(value: object) -> int | None:
	try:
		number = int(str(value).strip())
	except ValueError:
		return None
	return number if number > 0 else None


def _flag(value: object) -> bool:
	return str(value).strip().lower() == "true"


def _iso_date(value: object) -> str | None:
	try:
		return date.fromisoformat(str(value).strip()).isoformat()
	except ValueError:
		return None


def _efforts(value: object) -> tuple[tuple[str, ...], bool]:
	"""The reasoning efforts a model accepts, and whether reasoning can't be switched off."""
	if not isinstance(value, dict):
		return (), False
	supported = value.get("supported_efforts")
	efforts = tuple(str(effort) for effort in supported) if isinstance(supported, list) else ()
	return efforts, value.get("mandatory") is True


def _native_entry(item: dict[str, Any]) -> CatalogueEntry | None:
	name = item.get("name")
	task = item.get("task")
	kind = KINDS.get(task.get("name", "")) if isinstance(task, dict) else None
	if not isinstance(name, str) or not name or kind is None:
		return None

	properties = {
		p.get("property_id"): p.get("value") for p in item.get("properties") or [] if isinstance(p, dict)
	}
	# An embedding model's usable input is its max_input_tokens, when it states one.
	limit = _int(properties.get("max_input_tokens")) if kind == "embedding" else None
	supported, mandatory = _efforts(properties.get("reasoning_effort"))
	return CatalogueEntry(
		id=name,
		kind=kind,
		description=str(item.get("description") or ""),
		context_window=limit or _int(properties.get("context_window")),
		tools=_flag(properties.get("function_calling")),
		reasoning=_flag(properties.get("reasoning")),
		vision=_flag(properties.get("vision")),
		deprecation_date=_iso_date(properties.get("planned_deprecation_date")),
		supported_efforts=supported,
		mandatory_effort=mandatory,
	)


def _merge_openrouter(entry: CatalogueEntry, item: dict[str, Any]) -> CatalogueEntry:
	"""`entry` with the facts a `format=openrouter` item states; what it leaves out stays."""
	features = item.get("supported_features") or []
	modalities = item.get("input_modalities") or []
	supported, mandatory = _efforts(item.get("reasoning"))
	return CatalogueEntry(
		id=entry.id,
		kind=entry.kind,
		description=entry.description,
		context_window=_int(item.get("context_length")) or entry.context_window,
		max_output=_int(item.get("max_output_length")) or entry.max_output,
		tools=entry.tools or "tools" in features,
		reasoning=entry.reasoning or "reasoning" in features,
		vision=entry.vision or "image" in modalities,
		deprecation_date=_iso_date(item.get("deprecation_date")) or entry.deprecation_date,
		supported_efforts=supported or entry.supported_efforts,
		mandatory_effort=entry.mandatory_effort or mandatory,
	)


def catalogue_url(provider: FlowProvider) -> str | None:
	"""`.../ai/models/search` next to the OpenAI-compatible root, or None for a custom endpoint."""
	root = api_root(provider)
	suffix = "/ai/v1"
	return root.removesuffix(suffix) + "/ai/models/search" if root.endswith(suffix) else None


def get_page(url: str, token: str, params: dict[str, Any]) -> dict[str, Any]:
	try:
		response = requests.get(
			url,
			params=params,
			headers={"Authorization": f"Bearer {token}"},
			timeout=REQUEST_TIMEOUT,
			allow_redirects=False,
		)
	except requests.RequestException as e:
		raise CatalogueError(_("Could not reach Cloudflare ({0}).").format(type(e).__name__)) from None

	if response.status_code in (401, 403):
		raise CatalogueError(_("Cloudflare rejected the token. {0}").format(TOKEN_HINT))
	if response.status_code == 404:
		raise CatalogueError(_("Cloudflare does not know that account. {0}").format(STATUS_HINTS[404]))
	if not response.ok:
		raise CatalogueError(_("Cloudflare answered with HTTP {0}.").format(response.status_code))
	try:
		body = response.json()
	except ValueError:
		raise CatalogueError(_("Cloudflare answered with something other than JSON.")) from None
	if not isinstance(body, dict):
		raise CatalogueError(_("Cloudflare answered with something unexpected."))
	return body


def _fetch_pages(url: str, token: str, params: dict[str, Any], key: str) -> list[dict[str, Any]]:
	"""Every item of a paginated catalogue call. Cloudflare picks the page size whatever `per_page`
	says, so page until the reported total, an empty page or a page with nothing new."""
	items: list[dict[str, Any]] = []
	seen: set[object] = set()
	for page in range(1, MAX_PAGES + 1):
		body = get_page(url, token, {**params, "page": page, "per_page": PER_PAGE})
		batch = body.get(key)
		if not isinstance(batch, list):
			break
		fresh = [item for item in batch if isinstance(item, dict) and item.get("id") not in seen]
		if not fresh:
			break
		seen.update(item.get("id") for item in fresh)
		items.extend(fresh)
		info = body.get("result_info")
		total = info.get("total_count") if isinstance(info, dict) else None
		if isinstance(total, int) and len(items) >= total:
			break
	return items


def fetch_catalogue(provider: FlowProvider) -> list[CatalogueEntry]:
	"""The account's text generation and embedding models, from Cloudflare."""
	url = catalogue_url(provider)
	token = provider.get_password("api_key", raise_exception=False)
	if url is None or not token:
		return []

	entries: dict[str, CatalogueEntry] = {}
	for item in _fetch_pages(url, token, {}, "result"):
		if entry := _native_entry(item):
			entries[entry.id] = entry
	try:
		detailed = _fetch_pages(url, token, {"format": "openrouter"}, "data")
	except CatalogueError:
		detailed = []  # the native shape already answers everything but the finer limits
	for item in detailed:
		known = entries.get(str(item.get("id")))
		if known:
			entries[known.id] = _merge_openrouter(known, item)
	return sorted(entries.values(), key=lambda entry: entry.id)


def store_catalogue(entries: list[CatalogueEntry]) -> None:
	_cache().set_value(CACHE_KEY, [asdict(entry) for entry in entries], expires_in_sec=CACHE_TTL)
	_cache().delete_value(ERROR_KEY)


def sync_catalogue(provider: FlowProvider) -> list[CatalogueEntry]:
	"""Fetch the catalogue and cache it. Raises `CatalogueError`."""
	entries = fetch_catalogue(provider)
	store_catalogue(entries)
	return entries


def clear_catalogue() -> None:
	_cache().delete_value(CACHE_KEY)
	_cache().delete_value(ERROR_KEY)


def _entry_from_row(row: dict[str, Any]) -> CatalogueEntry:
	"""A cached entry. Reads each field by name, so a cache written by an older shape still loads."""
	return CatalogueEntry(
		id=row["id"],
		kind=row["kind"],
		description=row.get("description", ""),
		context_window=row.get("context_window"),
		max_output=row.get("max_output"),
		tools=row.get("tools", False),
		reasoning=row.get("reasoning", False),
		vision=row.get("vision", False),
		deprecation_date=row.get("deprecation_date"),
		supported_efforts=tuple(row.get("supported_efforts") or ()),
		mandatory_effort=row.get("mandatory_effort", False),
	)


def get_catalogue(*, fetch: bool) -> list[CatalogueEntry] | None:
	"""The cached catalogue. With `fetch`, a cold cache is filled from Cloudflare when the provider is
	enabled and the last attempt didn't just fail. None means unknown, never an error."""
	cached = _cache().get_value(CACHE_KEY)
	if cached is not None:
		return [_entry_from_row(row) for row in cached]
	if not fetch or _cache().get_value(ERROR_KEY):
		return None
	if not frappe.db.get_value("Flow Provider", PROVIDER, "enabled"):
		return None

	provider = cast("FlowProvider", frappe.get_doc("Flow Provider", PROVIDER))
	try:
		return sync_catalogue(provider)
	except CatalogueError as e:
		frappe.logger("flow").warning("Cloudflare catalogue unavailable: %s", e)
		_cache().set_value(ERROR_KEY, 1, expires_in_sec=ERROR_TTL)
		return None


def catalogue_entry(model_id: str, *, fetch: bool) -> CatalogueEntry | None:
	"""The catalogue entry for `cloudflare/@cf/...` (or the bare `@cf/...` id)."""
	name = strip_provider(model_id)
	return next((entry for entry in get_catalogue(fetch=fetch) or [] if entry.id == name), None)


def describe(entry: CatalogueEntry) -> str:
	"""A one-line summary for the model picker."""
	parts = [_("Embeddings") if entry.kind == "embedding" else _("Text")]
	if entry.context_window:
		parts.append(_("{0} tokens").format(f"{entry.context_window:,}"))
	if entry.tools:
		parts.append(_("tools"))
	if entry.reasoning:
		parts.append(_("reasoning"))
	if entry.deprecation_date:
		parts.append(_("deprecates {0}").format(entry.deprecation_date))
	return " · ".join(str(part) for part in parts)


def deprecation_notice(
	entry: CatalogueEntry, entries: list[CatalogueEntry], *, today: date | None = None
) -> str | None:
	"""A warning when the model is deprecated or will be within 60 days, naming a likely successor:
	the same family one step up (`glm-5.3-flash` -> `glm-5.3`) that isn't deprecated itself."""
	if not entry.deprecation_date:
		return None
	days = (date.fromisoformat(entry.deprecation_date) - (today or now_datetime().date())).days
	if days > DEPRECATION_WARNING_DAYS:
		return None

	when = (
		_("was deprecated on {0}").format(entry.deprecation_date)
		if days < 0
		else _("is planned for deprecation on {0}").format(entry.deprecation_date)
	)
	notice = _("{0} {1}.").format(entry.id, when)
	family = entry.id.rpartition("-")[0]
	successor = next((e for e in entries if e.id == family and not e.deprecation_date), None)
	if successor:
		notice += " " + _("{0} is its likely successor.").format(successor.id)
	return notice


def validate_reasoning_effort(entry: CatalogueEntry | None, params: dict[str, Any]) -> None:
	"""A model's `reasoning_effort` must be one the catalogue lists for it (when it lists any)."""
	if entry is None or not entry.supported_efforts or params.get("reasoning_effort") is None:
		return
	if params["reasoning_effort"] not in entry.supported_efforts:
		frappe.throw(
			_("{0} supports the reasoning efforts {1}, not {2}.").format(
				entry.id, ", ".join(entry.supported_efforts), frappe.bold(str(params["reasoning_effort"]))
			),
			title=_("Invalid Params"),
		)


def model_hints(entry: CatalogueEntry, entries: list[CatalogueEntry]) -> dict[str, Any]:
	"""What the Flow Model form shows about a model: its limits, efforts and any deprecation."""
	return {
		"context_window": entry.context_window,
		"supported_efforts": list(entry.supported_efforts),
		"mandatory_effort": entry.mandatory_effort,
		"deprecation": deprecation_notice(entry, entries),
	}
