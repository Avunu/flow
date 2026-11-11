// Copyright (c) 2026, Shrihari Mahabal and contributors
// For license information, please see license.txt

import { __ } from "@/lib/translate";

/** @import { Chip, FeedbackRun, Option, PausedRun, SessionDoc, SessionRow } from "@/lib/types" */

/**
 * @typedef {object} GetListOptions
 * @property {Record<string, unknown>} filters
 * @property {string[]} fields
 * @property {string} [order_by]
 * @property {number} limit
 */

// Read-only data fetches over the desk's whitelisted client API. The server returns rows of
// the fields asked for, so each caller types its own result.
/**
 * @param {string} doctype
 * @param {GetListOptions} options
 * @returns {Promise<unknown>}
 */
function getList(doctype, options) {
	return frappe.xcall("frappe.client.get_list", { doctype, ...options });
}

export const loadAgents = () =>
	/** @type {Promise<Option[]>} */ (
		getList("Flow Agent", { filters: { enabled: 1 }, fields: ["name", "title"], limit: 50 })
	);

export const loadModels = () =>
	/** @type {Promise<Option[]>} */ (
		getList("Flow Model", { filters: { enabled: 1 }, fields: ["name", "title"], limit: 50 })
	);

export const loadHistory = () =>
	/** @type {Promise<SessionRow[]>} */ (
		getList("Flow Session", {
			filters: { owner: frappe.session.user, source: ["!=", "Trigger"] },
			fields: ["name", "title", "modified"],
			order_by: "modified desc",
			limit: 15,
		})
	);

// Escape LIKE wildcards so a literal % or _ matches itself, not "anything".
/** @param {string} s */
const escapeLike = (s) => s.replace(/[\\%_]/g, "\\$&");

/** @param {string} query */
export const searchSessions = (query) =>
	/** @type {Promise<SessionRow[]>} */ (
		getList("Flow Session", {
			filters: {
				owner: frappe.session.user,
				source: ["!=", "Trigger"],
				title: ["like", `%${escapeLike(query)}%`],
			},
			fields: ["name", "title", "modified"],
			order_by: "modified desc",
			limit: 20,
		})
	);

/** @param {string} name */
export const getSession = (name) =>
	/** @type {Promise<SessionDoc>} */ (frappe.xcall("frappe.client.get", { doctype: "Flow Session", name }));

/** @param {string} session */
export const getPausedRun = (session) =>
	/** @type {Promise<PausedRun[]>} */ (
		getList("Flow Run", {
			filters: { session, status: "Paused" },
			fields: ["name", "questions"],
			order_by: "creation desc",
			limit: 1,
		})
	);

// Feedback the user already gave on this session's runs, to restore thumbs state on reload.
/** @param {string} session */
export const getRunFeedback = (session) =>
	/** @type {Promise<FeedbackRun[]>} */ (
		getList("Flow Run", {
			filters: { session, feedback_rating: ["is", "set"] },
			fields: ["name", "feedback_rating", "feedback_comment"],
			limit: 100,
		})
	);

// Record thumbs feedback on a run; optionally store a Down comment as agent memory.
/** @param {{ run_name: string | null, rating: string, comment: string | null }} args */
export const submitFeedback = (args) =>
	/** @type {Promise<{ memory?: boolean }>} */ (frappe.xcall("flow.api.submit_feedback", args)); // frappe-vulture:ignore

// Fail any Running run left behind by a stream that was cut off (refresh/navigation),
// so a reloaded session isn't blocked from starting the next turn.
/** @param {string} session */
export const recoverSession = (session) => frappe.xcall("flow.api.recover_session", { session }); // frappe-vulture:ignore

// Stop a run at the user's request: finalize an aborted stream's run or terminate a
// paused run so the agent won't continue.
/** @param {string} run_name */
export const stopRun = (run_name) => frappe.xcall("flow.api.stop_run", { run_name }); // frappe-vulture:ignore

// Map of the agent's tool slugs → requires_confirmation, so the panel can tell an
// approval tool call from an inline one.
/** @param {string} agent */
export const getAgentTools = (agent) =>
	/** @type {Promise<Record<string, boolean>>} */ (frappe.xcall("flow.api.get_agent_tools", { agent })); // frappe-vulture:ignore

// Upload a file as private, returning the created File doc. The chat attachment
// flow needs the File name to stage it via attachFile.
/**
 * @param {File} file
 * @returns {Promise<{ name: string }>}
 */
export async function uploadFile(file) {
	const form = new FormData();
	form.append("file", file, file.name);
	form.append("is_private", "1");

	const resp = await fetch("/api/method/upload_file", {
		method: "POST",
		headers: { "X-Frappe-CSRF-Token": frappe.csrf_token },
		body: form,
	});
	const data = await resp.json().catch(() => ({}));
	if (!resp.ok) throw new Error(serverMessage(data) || __("Upload failed ({0})", [resp.status]));
	return data.message;
}

// Validate and stage an uploaded File for use as a chat attachment. Returns chip
// metadata; throws (unsupported type, unreadable, …) which surfaces on the chip.
/** @param {string} file */
export const attachFile = (file) =>
	/** @type {Promise<Chip & { file: string }>} */ (frappe.xcall("flow.api.attach_file", { file })); // frappe-vulture:ignore

// Extract the human-readable message from a frappe error body.
/** @param {{ _server_messages?: string, exception?: string, _error_message?: string }} data */
export function serverMessage(data) {
	try {
		const msgs = JSON.parse(data._server_messages || "[]");
		if (msgs.length) return JSON.parse(msgs[0]).message;
	} catch {
		// fall through to other error fields
	}
	return data.exception || data._error_message || null;
}
