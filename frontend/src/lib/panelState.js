// Copyright (c) 2026, Shrihari Mahabal and contributors
// For license information, please see license.txt

// Persists the panel's open/fullscreen/width/session across reloads.
const KEY = "flow-panel-state";

/**
 * @typedef {object} PanelState
 * @property {boolean} [open]
 * @property {boolean} [fullscreen]
 * @property {number} [width]
 * @property {string | null} [session]
 */

/** @returns {PanelState} */
export function readPanelState() {
	try {
		return JSON.parse(localStorage.getItem(KEY) || "null") || {};
	} catch {
		return {};
	}
}

/** @param {PanelState} state */
export function writePanelState(state) {
	try {
		localStorage.setItem(KEY, JSON.stringify(state));
	} catch {
		// storage unavailable — persistence is best-effort
	}
}
