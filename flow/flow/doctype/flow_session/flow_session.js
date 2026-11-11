// Copyright (c) 2026, Frappe Technologies and contributors
// License: MIT. See LICENSE

frappe.ui.form.on("Flow Session", {
	refresh(frm) {
		/** @param {unknown} v */
		const parse = (v) => {
			try {
				return JSON.parse(String(v));
			} catch {
				return null;
			}
		};
		// Pretty-print JSON strings; leave plain text untouched.
		/** @param {unknown} v */
		const pretty = (v) => {
			const p = parse(v);
			return p !== null ? JSON.stringify(p, null, 2) : v;
		};

		const items = (frm.doc.messages || []).map((r) => {
			/** @type {{ role: string | null | undefined, content: string | null, calls: { name: string, args: unknown }[], output?: unknown }} */
			const item = {
				role: r.role,
				content: r.role === "tool" ? null : (r.content || "").trim() || null,
				calls: [],
			};
			if (r.role === "tool") item.output = pretty(r.content);
			for (const c of parse(r.tool_calls) || []) {
				const fn = c.function || c;
				item.calls.push({ name: fn.name, args: pretty(fn.arguments) });
			}
			return item;
		});

		const transcript = /** @type {frappe.ui.form.ControlHTML} */ (frm.fields_dict.transcript_html);
		transcript.$wrapper.html(frappe.render_template("flow_session_transcript", { items }));
	},
});
