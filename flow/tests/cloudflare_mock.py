# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""A stdlib mock of the Cloudflare Workers AI endpoints Flow calls, for tests that run the real
litellm and `requests` against a local socket instead of Cloudflare.

It serves the model catalogue (`ai/models/search`, native and `format=openrouter`, paginated the way
the live API is: the page size is fixed whatever `per_page` says), chat completions (plain, tool call
and streaming) and embeddings. Every request is recorded for the tests to assert on. The data comes
from `fixtures/cloudflare/`, which are real catalogue responses (model data only).
"""

from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).parent / "fixtures" / "cloudflare"
PAGE_SIZE = 3

# Obviously fake: nothing here is, or looks like, a real credential.
TOKEN = "test-token"
ACCOUNT_ID = "0123456789abcdef0123456789abcdef"
EMBEDDING_WIDTH = 4


def _load(name: str) -> dict[str, Any]:
	return json.loads((FIXTURES / name).read_text())


def native_models() -> list[dict[str, Any]]:
	return _load("models_search_glm.json")["result"] + _load("models_search_embeddings.json")["result"]


def _to_openrouter(item: dict[str, Any]) -> dict[str, Any]:
	"""The `format=openrouter` shape of a native catalogue item. The real captured response is used
	for the model it covers; the others are derived from their `properties` the same way."""
	properties = {p["property_id"]: p["value"] for p in item["properties"]}
	features = []
	if properties.get("function_calling") == "true":
		features.append("tools")
	if properties.get("reasoning") == "true":
		features.append("reasoning")
	out: dict[str, Any] = {
		"id": item["name"],
		"name": item["name"],
		"input_modalities": ["text", "image"] if properties.get("vision") == "true" else ["text"],
		"output_modalities": ["text"],
		"supported_features": features,
		"description": item["description"],
	}
	if "context_window" in properties:
		out["context_length"] = int(properties["context_window"])
		out["max_output_length"] = int(properties["context_window"])
	if "planned_deprecation_date" in properties:
		out["deprecation_date"] = properties["planned_deprecation_date"]
	if "reasoning_effort" in properties:
		out["reasoning"] = properties["reasoning_effort"]
	return out


def openrouter_models() -> list[dict[str, Any]]:
	captured = {item["id"]: item for item in _load("models_search_openrouter.json")["data"]}
	return [captured.get(item["name"]) or _to_openrouter(item) for item in native_models()]


def _error(code: int, message: str) -> dict[str, Any]:
	return {"success": False, "errors": [{"code": code, "message": message}], "messages": [], "result": None}


class MockCloudflare:
	"""Run with `with MockCloudflare() as cloudflare:`; `cloudflare.requests` is every request seen."""

	def __init__(self, *, token: str = TOKEN, account_id: str = ACCOUNT_ID):
		self.token = token
		self.account_id = account_id
		self.requests: list[dict[str, Any]] = []
		self.native = native_models()
		self.openrouter = openrouter_models()
		# Set to a status code to fail the next call with Cloudflare's error envelope.
		self.fail_with: int | None = None
		# Reject `stream_options`, as a model that doesn't accept it would.
		self.reject_stream_options = False
		self.server: ThreadingHTTPServer | None = None

	def __enter__(self) -> MockCloudflare:
		mock = self

		class Handler(BaseHTTPRequestHandler):
			def log_message(self, format: str, *args: Any) -> None:
				pass

			def do_GET(self) -> None:
				mock.handle(self, "GET")

			def do_POST(self) -> None:
				mock.handle(self, "POST")

		self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
		self.server.daemon_threads = True
		threading.Thread(target=self.server.serve_forever, daemon=True).start()
		return self

	def __exit__(self, *exc: object) -> None:
		if self.server:
			self.server.shutdown()
			self.server.server_close()

	@property
	def base_url(self) -> str:
		assert self.server
		return f"http://127.0.0.1:{self.server.server_port}/client/v4/accounts/{self.account_id}/ai/v1"

	def calls(self, suffix: str) -> list[dict[str, Any]]:
		return [r for r in self.requests if r["path"].endswith(suffix)]

	def handle(self, request: BaseHTTPRequestHandler, method: str) -> None:
		url = urlparse(request.path)
		length = int(request.headers.get("content-length") or 0)
		body = json.loads(request.rfile.read(length)) if length else None
		record = {
			"method": method,
			"path": url.path,
			"query": {k: v[0] for k, v in parse_qs(url.query).items()},
			"headers": {k.lower(): v for k, v in request.headers.items()},
			"body": body,
		}
		self.requests.append(record)

		if self.fail_with:
			status, self.fail_with = self.fail_with, None
			return self._json(request, status, _error(status * 10, "Injected failure"))
		if record["headers"].get("authorization") != f"Bearer {self.token}":
			return self._json(request, 401, _error(10000, "Authentication error"))
		if f"/accounts/{self.account_id}/" not in url.path:
			return self._json(request, 403, _error(7003, "Could not route to the account"))

		if method == "GET" and url.path.endswith("/ai/models/search"):
			return self._catalogue(request, record)
		if method == "POST" and url.path.endswith("/ai/v1/chat/completions"):
			return self._chat(request, record)
		if method == "POST" and url.path.endswith("/ai/v1/embeddings"):
			return self._embeddings(request, record)
		self._json(request, 404, _error(7000, "No route for that URL"))

	def _json(self, request: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
		data = json.dumps(payload).encode()
		request.send_response(status)
		request.send_header("Content-Type", "application/json")
		request.send_header("Content-Length", str(len(data)))
		request.end_headers()
		request.wfile.write(data)

	def _catalogue(self, request: BaseHTTPRequestHandler, record: dict[str, Any]) -> None:
		query = record["query"]
		page = int(query.get("page", 1))
		openrouter = query.get("format") == "openrouter"
		items = copy.deepcopy(self.openrouter if openrouter else self.native)
		window = items[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
		if openrouter:
			return self._json(request, 200, {"data": window})
		info = {"count": len(window), "page": page, "per_page": 100, "total_count": len(items)}
		self._json(
			request,
			200,
			{"success": True, "result": window, "errors": [], "messages": [], "result_info": info},
		)

	def _chat(self, request: BaseHTTPRequestHandler, record: dict[str, Any]) -> None:
		body = record["body"]
		if not str(body["model"]).startswith("@cf/"):
			return self._json(request, 404, _error(5007, f"No such model {body['model']}"))
		if record["headers"].get("cf-aig-gateway-id") == "boom":
			return self._json(request, 400, _error(2001, "Unknown gateway"))
		if self.reject_stream_options and "stream_options" in body:
			return self._json(request, 400, _error(5006, "stream_options is not supported"))

		wants_tool = bool(body.get("tools")) and "use-tool" in json.dumps(body["messages"])
		usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
		if body.get("stream"):
			return self._stream(request, body, wants_tool, usage)

		if wants_tool:
			message: dict[str, Any] = {
				"role": "assistant",
				"content": None,
				"tool_calls": [
					{
						"id": "call_1",
						"type": "function",
						"function": {"name": "add", "arguments": json.dumps({"a": 1, "b": 2})},
					}
				],
			}
			finish = "tool_calls"
		else:
			message = {"role": "assistant", "content": "Hello from the mock"}
			finish = "stop"
		self._json(
			request,
			200,
			{
				"id": "chatcmpl-mock",
				"object": "chat.completion",
				"created": 1,
				"model": body["model"],
				"choices": [{"index": 0, "message": message, "finish_reason": finish}],
				"usage": usage,
			},
		)

	def _stream(
		self, request: BaseHTTPRequestHandler, body: dict[str, Any], wants_tool: bool, usage: dict[str, int]
	) -> None:
		def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
			return {
				"id": "chatcmpl-mock",
				"object": "chat.completion.chunk",
				"created": 1,
				"model": body["model"],
				"choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
			}

		if wants_tool:
			call = {"index": 0, "id": "call_1", "type": "function"}
			events = [
				chunk(
					{
						"role": "assistant",
						"tool_calls": [{**call, "function": {"name": "add", "arguments": '{"a":'}}],
					}
				),
				chunk({"tool_calls": [{"index": 0, "function": {"arguments": " 1, "}}]}),
				chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"b": 2}'}}]}),
				chunk({}, "tool_calls"),
			]
		else:
			events = [
				chunk({"role": "assistant", "content": "Hello"}),
				chunk({"content": " from the mock"}),
				chunk({}, "stop"),
			]
		if (body.get("stream_options") or {}).get("include_usage"):
			events.append({**chunk({}), "choices": [], "usage": usage})

		request.send_response(200)
		request.send_header("Content-Type", "text/event-stream")
		request.end_headers()
		for event in events:
			request.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
		request.wfile.write(b"data: [DONE]\n\n")

	def _embeddings(self, request: BaseHTTPRequestHandler, record: dict[str, Any]) -> None:
		body = record["body"]
		texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
		data = [
			{"object": "embedding", "index": i, "embedding": [float(i)] * EMBEDDING_WIDTH}
			for i, _ in enumerate(texts)
		]
		self._json(
			request,
			200,
			{
				"object": "list",
				"data": data,
				"model": body["model"],
				"usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
			},
		)


def provider_row(base_url: str | None = None, **overrides: Any) -> dict[str, Any]:
	"""A `cloudflare` Flow Provider with the fake token, pointed at `base_url` (a mock) when given."""
	row: dict[str, Any] = {
		"doctype": "Flow Provider",
		"provider": "cloudflare",
		"account_id": ACCOUNT_ID,
		"api_key": TOKEN,
		"enabled": 1,
	}
	if base_url:
		row["base_url"] = base_url
	return {**row, **overrides}
