# Copyright (c) 2026, Shrihari Mahabal and contributors
# For license information, please see license.txt

app_name = "flow"
app_title = "Flow"
app_publisher = "Shrihari Mahabal"
app_description = "Frappe Flow — native AI agents, tools, and triggers for Frappe"
app_email = "shriharimahabal08@gmail.com"
app_license = "agpl-3.0"

export_python_type_annotations = True

# The Vite-built (frontend/src) AI panel bundle: `yarn build` writes flow_panel.bundle.[hash].js and .css
# to public/dist/ and registers them in assets.json (scripts/vite-register.mjs), so the desk loads them by name.
app_include_js = ["flow_panel.bundle.js"]
app_include_css = ["flow_panel.bundle.css"]

doc_events = {
	"*": {
		"after_insert": "flow.triggers.dispatch",  # frappe-vulture:ignore
		"on_update": "flow.triggers.dispatch",  # frappe-vulture:ignore
		"on_submit": "flow.triggers.dispatch",  # frappe-vulture:ignore
		"on_cancel": "flow.triggers.dispatch",  # frappe-vulture:ignore
		"on_trash": "flow.triggers.dispatch",  # frappe-vulture:ignore
	}
}

# Flow's references to other docs are bookkeeping — they must never block deleting
# the referenced doc. A knowledge chunk indexes a doc; a Flow Run records the doc a
# trigger acted on. The incremental sweep removes orphaned chunks afterwards.
# A session's Flow Model reference is historical bookkeeping and must not block deletion.
ignore_links_on_delete = ["Flow Knowledge Chunk", "Flow Run", "Flow Session"]

default_log_clearing_doctypes = {
	"Flow Session": 90,
}

scheduler_events = {
	"daily": [
		"flow.knowledge.ingest.sync_due_sources",
	],
	"cron": {
		"*/5 * * * *": [
			"flow.triggers.dispatch_scheduled",  # frappe-vulture:ignore
		],
	},
}

after_migrate = ["flow.assistant.sync_builtin_assistant"]  # frappe-vulture:ignore

extend_bootinfo = "flow.boot.boot_session"
