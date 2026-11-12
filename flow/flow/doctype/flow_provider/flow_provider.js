// Copyright (c) 2026, Frappe Technologies and contributors
// License: MIT. See LICENSE

const CLOUDFLARE_TOKEN_HELP = __(
	"A Cloudflare API token for this account with only Account > Workers AI > Read, plus AI Gateway > Run when the gateway is authenticated. Never grant a Write permission.",
);

/** @type {string | undefined} */
let default_key_description;

frappe.ui.form.on("Flow Provider", {
	refresh(frm) {
		set_key_description(frm);
		if (frm.is_new() || frm.doc.provider !== "cloudflare") return;

		add_action(frm, __("Test Connection"), "test_connection", __("Testing connection…"));
		add_action(frm, __("Sync Models"), "sync_models", __("Syncing models…"));
	},

	provider(frm) {
		// The Cloudflare fields key off the exact name, so fold what was typed to the canonical form.
		const provider = (frm.doc.provider || "").trim().toLowerCase();
		if (provider !== frm.doc.provider) frm.set_value("provider", provider);
		set_key_description(frm);
	},
});

/** @param {frappe.ui.form.Form<"Flow Provider">} frm */
function set_key_description(frm) {
	default_key_description ??= frm.fields_dict.api_key?.df.description;
	frm.set_df_property(
		"api_key",
		"description",
		frm.doc.provider === "cloudflare" ? CLOUDFLARE_TOKEN_HELP : default_key_description,
	);
}

/**
 * @param {frappe.ui.form.Form<"Flow Provider">} frm
 * @param {string} label
 * @param {"test_connection" | "sync_models"} method
 * @param {string} freeze_message
 */
function add_action(frm, label, method, freeze_message) {
	frm.add_custom_button(label, async () => {
		if (frm.is_dirty()) {
			frappe.msgprint({
				title: __("Unsaved Changes"),
				message: __("Save the document first: this uses the saved account and token."),
				indicator: "orange",
			});
			return;
		}

		frappe.dom.freeze(freeze_message);
		try {
			const r = await frm.call(method);
			const result = /** @type {{ ok?: boolean, message?: string, warnings?: string[] } | undefined} */ (
				r?.message
			);
			if (result?.ok) {
				frappe.show_alert({ message: result.message || __("Done"), indicator: "green" });
			}
			if (result?.warnings?.length) {
				frappe.msgprint({
					title: __("Models Being Deprecated"),
					message: result.warnings.map((w) => frappe.utils.escape_html(w)).join("<br>"),
					indicator: "orange",
				});
			}
		} finally {
			frappe.dom.unfreeze();
		}
	});
}
