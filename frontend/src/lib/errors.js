// Copyright (c) 2026, Shrihari Mahabal and contributors
// For license information, please see license.txt

// Failures reach the panel as Errors, DOMExceptions (an aborted fetch) or whatever
// frappe.xcall rejects with, so these check only for the fields they read.

/**
 * Whether `e` is what an aborted fetch rejects with.
 * @param {unknown} e
 */
export const isAbort = (e) => typeof e === "object" && e !== null && "name" in e && e.name === "AbortError";

/**
 * The failure's message, or "" when it has none.
 * @param {unknown} e
 */
export const errorMessage = (e) =>
	typeof e === "object" && e !== null && "message" in e && typeof e.message === "string" ? e.message : "";
