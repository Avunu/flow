// Copyright (c) 2026, Shrihari Mahabal and contributors
// For license information, please see license.txt

// Thin wrapper over the desk's global translator so components can stay
// translatable without depending on it being present (e.g. in isolation).
/**
 * @param {string} message
 * @param {unknown[]} [args]
 * @returns {string}
 */
export function __(message, args) {
	if (typeof window !== "undefined" && typeof window.__ === "function") {
		return window.__(message, args);
	}
	return message;
}
