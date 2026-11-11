// Copyright (c) 2026, Frappe Technologies and contributors
// License: MIT. See LICENSE

frappe.ui.form.on("Flow Run", {
	refresh(frm) {
		if (frm.doc.output) {
			const trimmed = frm.doc.output.trim();
			if (trimmed !== frm.doc.output) {
				frm.doc.output = trimmed;
				frm.refresh_field("output");
			}
		}

		/** @param {unknown} value */
		const parse = (value) => {
			if (!value) return null;
			if (typeof value === "object") return value;
			try {
				return JSON.parse(String(value));
			} catch {
				return null;
			}
		};

		const detail = /** @type {frappe.ui.form.ControlHTML} */ (frm.fields_dict.detail_html);
		detail.$wrapper.html(
			frappe.render_template("flow_run_detail", {
				usage: parse(frm.doc.usage),
				config: parse(frm.doc.config_snapshot),
				calls: parse(frm.doc.tool_calls),
				questions: parse(frm.doc.questions),
			}),
		);
	},
});
