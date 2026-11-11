// Copyright (c) 2026, Shrihari Mahabal and contributors
// For license information, please see license.txt

import "./scripts/vue-sfc-typescript.mjs";
import { fileURLToPath, URL } from "node:url";
import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

// Builds the AI panel as a single self-contained IIFE bundle + one CSS file, named
// `flow_panel.bundle.[hash]` under public/dist/ with a manifest, so scripts/vite-register.mjs
// registers them in assets.json and the desk loads them by name via `app_include_js` /
// `app_include_css` (see flow/hooks.py). Real frappe-ui components are pulled from source
// (`frappe-ui/src/...`) and tree-shaken.
export default defineConfig({
	define: {
		"process.env.NODE_ENV": JSON.stringify("production"),
		__VUE_OPTIONS_API__: "true",
		__VUE_PROD_DEVTOOLS__: "false",
		__VUE_PROD_HYDRATION_MISMATCH_DETAILS__: "false",
	},
	plugins: [vue()],
	resolve: {
		alias: [
			{ find: "@", replacement: fileURLToPath(new URL("./frontend/src", import.meta.url)) },
			// frappe-ui's package `exports` map hides its source; alias it so we can import
			// individual components without dragging in the whole index (tiptap, echarts, …).
			{
				find: "frappe-ui/src",
				replacement: fileURLToPath(new URL("./node_modules/frappe-ui/src", import.meta.url)),
			},
		],
	},
	build: {
		outDir: fileURLToPath(new URL("./flow/public/dist", import.meta.url)),
		// frappe's esbuild writes its bundles to the same directory.
		emptyOutDir: false,
		manifest: true,
		cssCodeSplit: false,
		sourcemap: false,
		target: "es2017",
		lib: {
			entry: fileURLToPath(new URL("./frontend/src/main.js", import.meta.url)),
			formats: ["iife"],
			name: "FlowPanel",
		},
		rollupOptions: {
			output: {
				entryFileNames: "js/flow_panel.bundle.[hash].js",
				assetFileNames: "css/flow_panel.bundle.[hash][extname]",
			},
		},
	},
});
