// Copyright (c) 2026, Shrihari Mahabal and contributors
// For license information, please see license.txt

// The shapes the panel passes around. The panel is JavaScript checked with checkJs; these are
// declarations only, imported by its JSDoc: `/** @import { Message } from "@/lib/types" */`.

/** A row of the agent and model pickers. */
export interface Option {
	name: string;
	title: string;
}

/** A row of the session history. */
export interface SessionRow {
	name: string;
	title: string;
	modified: string;
}

/** Attachment metadata shown on a sent message. */
export interface Chip {
	file_name: string;
	file_size: number;
}

/** A row of a Flow Session's messages table. */
export interface SessionMessage {
	role: "user" | "assistant" | "tool";
	content?: string | null;
	run?: string | null;
	/** A JSON array of OpenAI-style tool calls. */
	tool_calls?: string | null;
	tool_call_id?: string | null;
}

/** A Flow Session as `frappe.client.get` returns it, with the child rows the panel reads. */
export interface SessionDoc {
	agent: string;
	model?: string | null;
	attachments?: { run: string; file_name: string; file_size: number }[];
	messages?: SessionMessage[];
}

/** A Paused run: its questions are a JSON array. */
export interface PausedRun {
	name: string;
	questions: string | null;
}

/** A run the user rated. */
export interface FeedbackRun {
	name: string;
	feedback_rating: string;
	feedback_comment: string | null;
}

/** A composer attachment staged for the next turn. */
export interface StagedAttachment {
	uid: string;
	/** The File doc name once staged. */
	file: string | null;
	file_name: string;
	file_size: number;
	status: "uploading" | "ready" | "error";
	error: string;
}

/**
 * A question the agent put to the user: a tool confirmation or a free-text ask. The
 * `_`-prefixed fields are local UI state.
 */
export interface Question {
	/** The id of the tool call it confirms. */
	key: string;
	prompt: string;
	options: string[];
	allow_other?: boolean;
	_showOther: boolean;
	_otherText: string;
	_answer: string | undefined;
}

export interface TextPart {
	id: string;
	type: "text";
	text: string;
}

export type Approval = "approved" | "denied" | "redirected";

export interface ToolPart {
	id: string;
	type: "tool";
	name: string;
	arguments: string | Record<string, unknown> | null | undefined;
	result: string | null;
	approval: Approval | null;
}

export type Part = TextPart | ToolPart;

export interface Feedback {
	rating: string;
	comment: string;
}

export interface UserMsg {
	id: string;
	role: "user";
	content: string;
	attachments: Chip[];
	interrupted?: boolean;
}

export interface AssistantMsg {
	id: string;
	role: "assistant";
	parts: Part[];
	pending: boolean;
	questions: Question[];
	runName: string | null;
	feedback: Feedback | null;
}

export type Message = UserMsg | AssistantMsg;

/** What the run stream sends, one event per `data:` block. */
export type RunEvent =
	| { type: "run_started"; name: string; session: string }
	| { type: "text"; delta: string }
	| { type: "tool_started"; id: string; name: string; arguments: string }
	| { type: "tool_ended"; id: string; result: string }
	| { type: "done"; status: string; questions: Question[] }
	| { type: "error"; message: string };

/** An item of a dropdown menu. */
export interface MenuItem {
	value: string | null;
	label: string;
}
