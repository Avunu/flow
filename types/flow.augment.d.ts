// Copyright (c) 2026, Shrihari Mahabal and contributors
// For license information, please see license.txt

// What this app adds to the desk's globals, for the panel (frontend/) and the desk scripts.
// frappe's own globals come from frappe-types; nothing here redeclares them.

import "frappe-types";

declare module "frappe-types" {
	interface Frappe {
		/** frappe/public/js/frappe/utils/markdown.js: markdown to HTML, absent until the desk has loaded it. */
		markdown?: (text: string) => string;
		/** Set by the panel's bundle (frontend/src/main.js). */
		flow: { panel?: object };
	}

	interface FrappeBoot {
		/** flow/boot.py: the file types the ingest pipeline can extract. */
		flow_supported_file_types?: string[];
	}
}

declare global {
	interface Window {
		/** The desk loads moment on demand; the panel falls back to the raw timestamp without it. */
		moment?: (date: string) => { fromNow(): string };
	}
}
