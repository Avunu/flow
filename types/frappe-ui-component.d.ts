// Copyright (c) 2026, Shrihari Mahabal and contributors
// For license information, please see license.txt

// The frappe-ui components the panel imports from source (frontend/src/lib/ui.js): Button,
// FeatherIcon, Spinner and Badge.
//
// frappe-ui ships raw Vue sources that don't pass this repo's strict compiler settings, so
// frontend/tsconfig.json maps each of those imports to this declaration instead of type-checking
// the library (as messaging and ui_text_editor do for frappe-ui/editor). Vite still bundles the
// real sources: it doesn't read tsconfig paths. Their props are the library's own and unchecked
// here: add a typed declaration per component when the panel starts relying on a prop's type.

import type { DefineComponent } from "vue";

declare const component: DefineComponent<Record<string, unknown>>;
export default component;
