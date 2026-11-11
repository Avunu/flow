# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import json
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document

from flow.lib import cloudflare

# Keys litellm derives itself or that have dedicated fields — not allowed in extra_params.
RESERVED_PARAM_KEYS = frozenset(
	{"model", "api_key", "api_base", "base_url", "messages", "stream", "tools", "tool_choice"}
)


class FlowProvider(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		account_id: DF.Data | None
		api_key: DF.Password | None
		base_url: DF.Data | None
		enabled: DF.Check
		extra_params: DF.JSON | None
		gateway_id: DF.Data | None
		provider: DF.Data
	# end: auto-generated types

	def autoname(self):
		# Normalize before naming so the docname is the canonical lowercase provider,
		# matching what litellm.get_llm_provider returns for credential lookup.
		self.provider = (self.provider or "").strip().lower()
		self.name = self.provider

	def validate(self):
		self._normalize()
		self._validate_provider_known()
		self._validate_base_url()
		self._validate_extra_params()
		self._validate_cloudflare()

	def _normalize(self):
		self.provider = (self.provider or "").strip().lower()
		for field in ("base_url", "account_id", "gateway_id"):
			value = self.get(field)
			if isinstance(value, str):
				self.set(field, value.strip())
		if self.account_id:
			self.account_id = self.account_id.lower()
		if isinstance(self.api_key, str):
			self.api_key = self.api_key.strip()

	def _validate_provider_known(self):
		try:
			import litellm
		except ImportError:
			return
		known = {p if isinstance(p, str) else p.value for p in litellm.provider_list}
		if self.provider not in known:
			frappe.throw(
				_(
					"Unknown provider {0}. It must be a LiteLLM provider name (e.g. openai, anthropic, openrouter, gemini)."
				).format(frappe.bold(self.provider)),
				title=_("Invalid Provider"),
			)

	def _validate_base_url(self):
		if not self.base_url:
			return
		parsed = urlparse(self.base_url)
		if parsed.scheme not in ("http", "https") or not parsed.netloc:
			frappe.throw(_("Base URL must be an absolute http(s) URL."), title=_("Invalid Base URL"))

	def _validate_extra_params(self):
		if not self.extra_params:
			return
		try:
			parsed = json.loads(self.extra_params)
		except TypeError, ValueError:
			frappe.throw(_("Extra Params must be valid JSON."), title=_("Invalid Params"))
		if not isinstance(parsed, dict):
			frappe.throw(_("Extra Params must be a JSON object."), title=_("Invalid Params"))
		conflicting = sorted(RESERVED_PARAM_KEYS.intersection(parsed))
		if conflicting:
			frappe.throw(
				_("Extra Params may not include reserved keys: {0}.").format(", ".join(conflicting)),
				title=_("Reserved Params"),
			)

	def _validate_cloudflare(self):
		if self.provider != cloudflare.PROVIDER:
			if self.account_id or self.gateway_id:
				frappe.throw(
					_("Account ID and Gateway ID only apply to the cloudflare provider."),
					title=_("Invalid Provider"),
				)
			return

		if not self.account_id:
			frappe.throw(
				_("Account ID is required for the cloudflare provider."), title=_("Missing Account ID")
			)
		cloudflare.validate_account_id(self.account_id)
		cloudflare.validate_gateway_id(self.gateway_id)
		cloudflare.check_base_url(self.base_url)
		if self.extra_params:
			cloudflare.validate_extra_headers(json.loads(self.extra_params).get("extra_headers"))
		# The doc value is masked after the first save, so ask for the stored password.
		if self.enabled and not self.get_password("api_key", raise_exception=False):
			frappe.throw(
				_("An API token is required for the cloudflare provider."), title=_("Missing API Token")
			)
