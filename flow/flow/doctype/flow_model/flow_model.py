# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import json
import re
from typing import Any
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document

from flow.lib import cloudflare
from flow.lib.model import provider_of, resolve_call_kwargs

# The model part may start with one `@` for Cloudflare Workers AI ids such as `@cf/zai-org/glm-5.3-flash`.
MODEL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_\-]*\/@?[A-Za-z0-9][A-Za-z0-9_\-:.\/]*$")

RESERVED_PARAM_KEYS = frozenset(
	{"model", "api_key", "api_base", "base_url", "messages", "stream", "tools", "tool_choice"}
)


class FlowModel(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Password | None
		base_url: DF.Data | None
		context_window: DF.Int
		enabled: DF.Check
		model_id: DF.Data
		params: DF.JSON | None
		provider: DF.Link | None
		title: DF.Data
	# end: auto-generated types

	def validate(self):
		self._normalize()
		self._apply_provider()
		self._validate_model_id()
		self._validate_base_url()
		self._validate_params()
		self._validate_provider_known()
		self._validate_cloudflare()
		self._resolve_context_window()

	def after_insert(self):
		if not self.enabled:
			return
		from flow.assistant import sync_builtin_assistant

		sync_builtin_assistant(model=self.name)

	def _normalize(self):
		for field in ("title", "model_id", "base_url"):
			value = self.get(field)
			if isinstance(value, str):
				self.set(field, value.strip())
		if isinstance(self.api_key, str):
			self.api_key = self.api_key.strip()

	def _apply_provider(self):
		# A linked provider lets the user enter just the model; compose the
		# canonical provider/model id. Idempotent if the prefix is already present.
		if not self.provider:
			return
		prefix = f"{self.provider}/"
		if self.model_id and not self.model_id.startswith(prefix):
			self.model_id = prefix + self.model_id

	def _validate_model_id(self):
		if not MODEL_ID_PATTERN.match(self.model_id or ""):
			frappe.throw(
				_(
					"Model ID must be in <code>provider/model</code> form (e.g. <code>anthropic/claude-sonnet-4-6</code>). Only lowercase provider names and alphanumerics, dashes, dots, colons or slashes in the model part are allowed, with one leading <code>@</code> (e.g. <code>cloudflare/@cf/zai-org/glm-5.3-flash</code>)."
				),
				title=_("Invalid Model ID"),
			)

	def _validate_base_url(self):
		if not self.base_url:
			return
		parsed = urlparse(self.base_url)
		if parsed.scheme not in ("http", "https") or not parsed.netloc:
			frappe.throw(
				_("Base URL must be an absolute http(s) URL."),
				title=_("Invalid Base URL"),
			)

	def _validate_params(self):
		if not self.params:
			return
		try:
			parsed = json.loads(self.params)
		except TypeError, ValueError:
			frappe.throw(_("Params must be valid JSON."), title=_("Invalid Params"))
		if not isinstance(parsed, dict):
			frappe.throw(_("Params must be a JSON object."), title=_("Invalid Params"))
		conflicting = sorted(RESERVED_PARAM_KEYS.intersection(parsed))
		if conflicting:
			frappe.throw(
				_("Params may not include reserved keys: {0}.").format(", ".join(conflicting)),
				title=_("Reserved Params"),
			)

	def _resolve_context_window(self):
		# A value typed in this save wins. Otherwise detect when the model is new, its id changed or
		# the window is unknown, keeping the stored value when the model can't be resolved (0 means
		# unknown: callers fall back to a default). A plain re-save never overwrites an override.
		if self.context_window and self.has_value_changed("context_window"):
			return
		if self.is_new() or self.has_value_changed("model_id") or not self.context_window:
			self.context_window = _detect_context_window(self.model_id) or self.context_window or 0

	def _validate_provider_known(self):
		try:
			import litellm
		except ImportError:
			return
		try:
			litellm.get_llm_provider(self.model_id)
		except Exception as e:
			frappe.throw(str(e)[:500], title=_("Invalid Model ID"))

	def _validate_cloudflare(self):
		provider = provider_of(self.model_id)
		model_part = self.model_id.partition("/")[2]
		if provider != cloudflare.PROVIDER:
			# The old workaround (`openai/@cf/...` plus a per-model URL and key). Only enforced on a new or
			# changed id, so a row that has not been migrated yet can still be edited.
			if cloudflare.is_hosted_model(model_part) and (
				self.is_new() or self.has_value_changed("model_id")
			):
				frappe.throw(
					_(
						"Workers AI models use the Cloudflare provider: set Provider to <code>cloudflare</code>."
					),
					title=_("Invalid Model ID"),
				)
			return

		if not cloudflare.is_hosted_model(cloudflare.strip_provider(self.model_id)):
			frappe.throw(
				_(
					"Only Cloudflare-hosted models are supported, with an id like <code>@cf/zai-org/glm-5.3-flash</code>."
				),
				title=_("Invalid Model ID"),
			)
		if self.api_key or self.base_url:
			frappe.throw(
				_("Configure the endpoint and API token once on the Cloudflare provider, not on the model."),
				title=_("Invalid Model"),
			)
		if self.enabled and not frappe.db.get_value("Flow Provider", cloudflare.PROVIDER, "enabled"):
			frappe.throw(
				_("Set up and enable the cloudflare Flow Provider first."),
				title=_("Cloudflare Not Configured"),
			)
		params = cloudflare.validate_params(json.loads(self.params) if self.params else {}, title=_("Params"))

		entry = cloudflare.catalogue_entry(self.model_id, fetch=True)
		cloudflare.validate_reasoning_effort(entry, params)
		if self.is_new() or self.has_value_changed("model_id"):
			self._warn_about_catalogue(entry)

	def _warn_about_catalogue(self, entry: cloudflare.CatalogueEntry | None):
		catalogue = cloudflare.get_catalogue(fetch=False)
		if entry:
			notice = cloudflare.deprecation_notice(entry, catalogue or [])
			if notice:
				frappe.msgprint(notice, title=_("Deprecation"), indicator="orange")
		elif catalogue:
			frappe.msgprint(
				_(
					"{0} is not in your Cloudflare model catalogue. Sync models on the Cloudflare provider if it is new."
				).format(cloudflare.strip_provider(self.model_id)),
				title=_("Unknown Model"),
				indicator="orange",
			)

	@frappe.whitelist()
	def test_connection(self):
		self.check_permission("write")

		try:
			import litellm
		except ImportError:
			frappe.throw(
				_("LiteLLM is not installed. Run <code>bench setup requirements</code>."),
				title=_("Missing Dependency"),
			)

		secret = None
		try:
			kwargs = resolve_call_kwargs(
				self.model_id,
				api_key=self.get_password("api_key", raise_exception=False),
				base_url=self.base_url,
			)
			secret = kwargs.get("api_key")
			litellm.completion(
				**kwargs, messages=[{"role": "user", "content": "ping"}], max_tokens=1, timeout=15
			)
		except Exception as e:
			# Request details can echo the credentials, so scrub them from what the user sees.
			if provider_of(self.model_id) == cloudflare.PROVIDER:
				message = cloudflare.explain_error(e, secret)
			else:
				message = cloudflare.redact(str(e) or type(e).__name__, secret)[:500]
			frappe.throw(message, title=_(type(e).__name__))

		return {"ok": True, "message": _("Connection OK")}


def _detect_context_window(model_id: str) -> int:
	"""Max input tokens for `model_id`: from the Cloudflare catalogue for a Workers AI model, else per
	litellm. 0 if unknown/unmapped."""
	import litellm

	if provider_of(model_id) == cloudflare.PROVIDER:
		entry = cloudflare.catalogue_entry(model_id, fetch=True)
		if entry and entry.context_window:
			return entry.context_window

	try:
		info = litellm.get_model_info(model_id)
	except Exception:
		return 0
	return int(info.get("max_input_tokens") or 0)


@frappe.whitelist()
def get_provider_models(provider: str | None = None) -> list[str] | list[dict[str, str]]:
	frappe.has_permission("Flow Model", "read", throw=True)
	if not provider:
		return []

	name = provider.strip().lower()
	# The Cloudflare catalogue comes from the cache only: a fetch spends the stored token, and any
	# reader of Flow Models can call this. "Sync Models" on the provider fills it.
	if name == cloudflare.PROVIDER and (catalogue := cloudflare.get_catalogue(fetch=False)):
		return [{"value": entry.id, "description": cloudflare.describe(entry)} for entry in catalogue]

	import litellm

	return sorted(litellm.models_by_provider.get(name, set()))


@frappe.whitelist()
def get_model_hints(model_id: str | None = None) -> dict[str, Any]:
	"""Context window, reasoning efforts and deprecation of a Cloudflare model, for the form. Cached
	data only, so it never reaches Cloudflare."""
	frappe.has_permission("Flow Model", "read", throw=True)
	if not model_id or provider_of(model_id) != cloudflare.PROVIDER:
		return {}
	catalogue = cloudflare.get_catalogue(fetch=False) or []
	entry = cloudflare.catalogue_entry(model_id, fetch=False)
	return cloudflare.model_hints(entry, catalogue) if entry else {}
