"""Streamable HTTP MCP endpoint for yomiyasu's official upstream check and verify.

Uses the same checked upstream checkout and session functions as server.py.
The old GET-only gate remains available through server.py and plain_server.py.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import server

PROTOCOL_VERSION = "2025-06-18"
MAX_REQUEST_BYTES = 3_200_000

TOOLS = [
    {
        "name": "yomiyasu_check",
        "description": (
            "Run the official nanaism/yomiyasu v3.1.0 lint/diff/check suite "
            "on a candidate Japanese response before sending it. "
            "Returns the upstream check output, forbidden-string findings, "
            "and a session_id required for yomiyasu_verify. "
            "Call again with the entire revised final text after any edit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Full original text, request, and relevant supporting context."},
                "final": {"type": "string", "description": "The exact complete Japanese answer intended for the user."},
                "material": {"type": "string", "description": "Optional reference material for the check."},
                "forbidden": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact prohibited strings specified for this conversation."
                },
                "doc_type": {"type": "string", "description": "Optional upstream document category."}
            },
            "required": ["source", "final"],
            "additionalProperties": False
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False}
    },
    {
        "name": "yomiyasu_verify",
        "description": (
            "Verify the outgoing answer byte-for-byte against the last successful "
            "yomiyasu_check session, including the official upstream verify step. "
            "Use the identical complete final text, never a paraphrase. "
            "Only send text after status PASS."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID returned by yomiyasu_check."},
                "final": {"type": "string", "description": "Exact complete Japanese answer to send."}
            },
            "required": ["session_id", "final"],
            "additionalProperties": False
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False}
    }
]


def _store(folder, kind: str, data: bytes) -> None:
    if len(data) > server.MAX_TEXT_BYTES:
        raise ValueError(f"{kind} exceeds the maximum text size")
    # Always create one chunk so an empty document is also representable.
    for index, start in enumerate(range(0, max(len(data), 1), server.MAX_CHUNK_BYTES)):
        server.write_chunk(folder, kind, index, data[start:start + server.MAX_CHUNK_BYTES])


def _required_str(args: dict, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def check(args: dict) -> dict:
    source = _required_str(args, "source")
    final = _required_str(args, "final")
    forbidden = args.get("forbidden", [])
    if not isinstance(forbidden, list) or not all(isinstance(x, str) for x in forbidden):
        raise ValueError("forbidden must be an array of strings")
    material = args.get("material")
    if material is not None and not isinstance(material, str):
        raise ValueError("material must be a string")
    doc_type = args.get("doc_type")
    if doc_type is not None and not isinstance(doc_type, str):
        raise ValueError("doc_type must be a string")

    sid = server.new_session()["session_id"]
    folder = server.session_dir(sid)
    _store(folder, "source", source.encode("utf-8"))
    _store(folder, "final", final.encode("utf-8"))
    _store(folder, "forbidden", json.dumps(forbidden, ensure_ascii=False).encode("utf-8"))
    if material is not None:
        _store(folder, "material", material.encode("utf-8"))
    return server.check_session(sid, doc_type)


def verify(args: dict) -> dict:
    sid = _required_str(args, "session_id")
    final = _required_str(args, "final")
    folder = server.session_dir(sid)
    if not folder.is_dir():
        return {"status": "FAIL", "message": "session not found"}
    _store(folder, "verify", final.encode("utf-8"))
    return server.verify_session(sid)


def rpc_error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class MCPHandler(BaseHTTPRequestHandler):
    server_version = "yomiyasu-mcp/0.4"

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _empty(self, status: int, allow: str | None = None) -> None:
        self.send_response(status)
        if allow:
            self.send_header("Allow", allow)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_json(self, status: int, payload: object) -> None:
        # Retain the existing chunked, GET-only inspection fallback for
        # clients that cannot invoke custom MCP tools (e.g. mobile Chat).
        self._json(status, payload)

    def do_GET(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith(server.GATE_PREFIX + "/"):
            return server.Handler.do_GET(self)

        if path == "":
            self._json(200, {"name": "yomiyasu-mcp", "status": "ok", "upstream": server.UPSTREAM_TAG})
        elif path == "/mcp":
            # The MCP Streamable HTTP specification permits 405 when SSE GET is unsupported.
            self._empty(405, "POST")
        else:
            self._empty(404)

    def do_POST(self) -> None:
        if urlparse(self.path).path.rstrip("/") != "/mcp":
            self._empty(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                self._json(413, rpc_error(None, -32600, "Invalid request size"))
                return
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                self._json(400, rpc_error(None, -32600, "Invalid JSON-RPC request"))
                return
            method = request.get("method")
            request_id = request.get("id")
            params = request.get("params", {})
            if not isinstance(params, dict):
                self._json(200, rpc_error(request_id, -32602, "Invalid params"))
                return
            if method == "notifications/initialized" or (
                isinstance(method, str) and method.startswith("notifications/")
            ):
                self._empty(202)
                return
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "yomiyasu-inspector", "version": "0.4.0"},
                    "instructions": "Call yomiyasu_check followed by yomiyasu_verify with the exact final text."
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict):
                    self._json(200, rpc_error(request_id, -32602, "Tool arguments must be an object"))
                    return
                if name not in ("yomiyasu_check", "yomiyasu_verify"):
                    self._json(200, rpc_error(request_id, -32602, "Unknown tool"))
                    return
                try:
                    payload = check(arguments) if name == "yomiyasu_check" else verify(arguments)
                except Exception as exc:
                    payload = {"status": "FAIL", "message": f"{type(exc).__name__}: {exc}"}
                result = {
                    "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                    "structuredContent": payload,
                    "isError": payload.get("status") == "FAIL"
                }
            else:
                self._json(200, rpc_error(request_id, -32601, "Method not found"))
                return

            if request_id is None:
                self._empty(202)
            else:
                self._json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, rpc_error(None, -32700, f"Invalid request: {exc}"))
        except Exception:
            self._json(500, rpc_error(None, -32603, "Internal server error"))

    def do_DELETE(self) -> None:
        self._empty(405, "POST")


def main() -> None:
    server.ensure_upstream()
    port = int(os.environ.get("PORT", "10000"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), MCPHandler)
    print(f"yomiyasu MCP listening on :{port}/mcp", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
