// Copyright (c) 2026, Frappe Technologies and contributors
// License: MIT. See LICENSE

/** @typedef {frappe.ui.form.Form<"Flow Knowledge Source">} SourceForm */

// The chunk settings each open form was last saved with, to tell on save whether they changed.
/** @type {WeakMap<SourceForm, { chunk_size: number | null | undefined, chunk_overlap: number | null | undefined }>} */
const chunking_baselines = new WeakMap();

frappe.ui.form.on("Flow Knowledge Source", {
	refresh(frm) {
		apply_file_restrictions(frm);

		if (frm.is_new()) {
			return;
		}

		chunking_baselines.set(frm, {
			chunk_size: frm.doc.chunk_size,
			chunk_overlap: frm.doc.chunk_overlap,
		});

		frm.add_custom_button(__("Resync"), async () => {
			await frm.call("resync", { rebuild: 0 });
			frappe.show_alert({ message: __("Resync started"), indicator: "blue" });
			frm.reload_doc();
		});

		if (frm.doc.source_type === "DocType") {
			frm.add_custom_button(__("Reconcile"), async () => {
				await frm.call("reconcile");
				frappe.show_alert({ message: __("Reconcile started"), indicator: "blue" });
			});
		}
	},

	after_save(frm) {
		const baseline = chunking_baselines.get(frm);
		if (!baseline || !frm.doc.chunk_count) {
			return;
		}
		const changed =
			baseline.chunk_size !== frm.doc.chunk_size || baseline.chunk_overlap !== frm.doc.chunk_overlap;
		if (!changed) {
			return;
		}
		chunking_baselines.set(frm, {
			chunk_size: frm.doc.chunk_size,
			chunk_overlap: frm.doc.chunk_overlap,
		});
		frappe.confirm(
			__(
				"Chunk settings changed. The {0} existing chunks for this source will be deleted and rebuilt with the new settings. Re-process now?",
				[frm.doc.chunk_count],
			),
			() => {
				frm.call("resync", { rebuild: 1 }).then(() => {
					frappe.show_alert({ message: __("Re-ingestion started"), indicator: "blue" });
					frm.reload_doc();
				});
			},
		);
	},

	source_type(frm) {
		apply_file_restrictions(frm);
	},
});

// Cap the File picker to the formats the ingest pipeline can actually extract.
// Types come from frappe.boot
/** @param {SourceForm} frm */
function apply_file_restrictions(frm) {
	if (frm.doc.source_type !== "File") {
		return;
	}
	const types = (frappe.boot.flow_supported_file_types || []).map((ext) => `.${ext}`);
	const field = frm.fields_dict.file;
	if (!field) {
		return;
	}
	field.df.options = {
		restrictions: { allowed_file_types: types },
	};
}
