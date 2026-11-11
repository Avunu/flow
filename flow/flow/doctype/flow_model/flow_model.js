// Copyright (c) 2026, Frappe Technologies and contributors
// License: MIT. See LICENSE

/** @type {string | undefined} */
let default_params_description;

frappe.ui.form.on("Flow Model", {
	refresh(frm) {
		load_provider_models(frm);
		show_cloudflare_hints(frm);

		if (frm.is_new()) return;

		frm.add_custom_button(__("Test Connection"), async () => {
			if (frm.is_dirty()) {
				frappe.msgprint({
					title: __("Unsaved Changes"),
					message: __("Save the document before testing the connection."),
					indicator: "orange",
				});
				return;
			}

			frappe.dom.freeze(__("Testing connection…"));
			try {
				const r = await frm.call("test_connection");
				const result = /** @type {{ ok?: boolean, message?: string } | undefined} */ (r?.message);
				if (result?.ok) {
					frappe.show_alert({
						message: result.message || __("Connection OK"),
						indicator: "green",
					});
				}
			} finally {
				frappe.dom.unfreeze();
			}
		});
	},

	provider(frm) {
		load_provider_models(frm);
		show_cloudflare_hints(frm);
	},

	model_id(frm) {
		show_cloudflare_hints(frm);
	},
});

/** @param {frappe.ui.form.Form<"Flow Model">} frm */
function load_provider_models(frm) {
	const field = /** @type {frappe.ui.form.ControlAutocomplete} */ (frm.fields_dict.model_id);
	// Suggestions are hints, never a restriction — allow any typed model_id.
	field.df.ignore_validation = true;
	if (!frm.doc.provider) {
		field.set_data([]);
		return;
	}
	frappe.call({
		method: "flow.flow.doctype.flow_model.flow_model.get_provider_models",
		args: { provider: frm.doc.provider },
		callback: ({ message }) => field.set_data(message || []),
	});
}

/**
 * The model's id as the server stores it (`cloudflare/@cf/...`), or "" for a non-Cloudflare model.
 * @param {frappe.ui.form.Form<"Flow Model">} frm
 */
function cloudflare_model_id(frm) {
	const model_id = frm.doc.model_id || "";
	if (model_id.startsWith("cloudflare/")) return model_id;
	return frm.doc.provider === "cloudflare" && model_id ? `cloudflare/${model_id}` : "";
}

/**
 * What the catalogue says about a Cloudflare model: where its credentials come from, a deprecation
 * warning, and the reasoning efforts its params may use. Cached data only, never a Cloudflare call.
 * @param {frappe.ui.form.Form<"Flow Model">} frm
 */
function show_cloudflare_hints(frm) {
	default_params_description ??= frm.fields_dict.params?.df.description;
	const model_id = cloudflare_model_id(frm);
	if (!model_id) {
		frm.set_intro("");
		frm.set_df_property("params", "description", default_params_description);
		return;
	}

	frm.set_intro(__("Endpoint and token come from the Cloudflare provider."), "blue");
	frappe.call({
		method: "flow.flow.doctype.flow_model.flow_model.get_model_hints",
		args: { model_id },
		callback: ({ message }) => {
			const hints =
				/** @type {{ supported_efforts?: string[], mandatory_effort?: boolean, deprecation?: string | null } | undefined} */ (
					message
				);
			if (cloudflare_model_id(frm) !== model_id) return; // the form moved on

			if (hints?.deprecation) frm.set_intro(frappe.utils.escape_html(hints.deprecation), "orange");

			const efforts = hints?.supported_efforts || [];
			frm.set_df_property(
				"params",
				"description",
				efforts.length
					? `${default_params_description} ${__("Supported reasoning_effort values: {0}.", [efforts.join(", ")])}${
							hints?.mandatory_effort ? ` ${__("Reasoning cannot be turned off.")}` : ""
						}`
					: default_params_description,
			);
		},
	});
}
