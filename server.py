from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
UPSTREAM = ROOT / "upstream"
CHECK = UPSTREAM / "scripts" / "yomiyasu_check.py"
LINT = UPSTREAM / "scripts" / "yomiyasu_lint.py"
SESSION_ROOT = Path(os.environ.get("YOMIYASU_SESSION_DIR", "/tmp/yomiyasu-mcp"))
SESSION_ROOT.mkdir(parents=True, exist_ok=True)
MAX_TEXT = 300_000
MAX_OUTPUT = 80_000
SESSION_TTL = 6 * 60 * 60
PATH_SECRET = os.environ.get("MCP_PATH_SECRET", "").strip("/")
MCP_PATH = f"/{PATH_SECRET}/mcp" if PATH_SECRET else "/mcp"

TOOLS = [
    {
        "name": "yomiyasu_lint",
        "description": "Run nanaism/yomiyasu v2.5.0 yomiyasu_lint.py against a Japanese final-text candidate. Also scan literal user-specific forbidden strings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "doc_type": {"type": "string"},
                "forbidden": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "yomiyasu_check",
        "description": "Run nanaism/yomiyasu v2.5.0 yomiyasu_check.py run on source and final text. Returns a session_id for a later exact-final verification. Also scans literal user-specific forbidden strings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "final": {"type": "string"},
                "material": {"type": "string"},
                "doc_type": {"type": "string"},
                "forbidden": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["source", "final"],
            "additionalProperties": False,
        },
    },
    {
        "name": "yomiyasu_verify",
        "description": "Verify that the exact final text still matches the text inspected by yomiyasu_check, using both the upstream verify command and an independent SHA-256 check. Call immediately before emitting the answer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "final": {"type": "string"},
            },
            "required": ["session_id", "final"],
            "additionalProperties": False,
        },
    },
]


def bounded(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value) > MAX_TEXT:
        raise ValueError(f"{name} exceeds {MAX_TEXT} characters")
    return value


def normalize_forbidden(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("forbidden must be an array")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("forbidden entries must be strings")
        if item and item not in out:
            out.append(item)
    return out


def forbidden_hits(text: str, words: list[str]) -> list[dict]:
    hits: list[dict] = []
    for word in words:
        start = 0
        while word:
            idx = text.find(word, start)
            if idx < 0:
                break
            hits.append({"word": word, "offset": idx})
            start = idx + len(word)
    return hits


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_cmd(args: list[str], cwd: Path, timeout: int = 100) -> dict:
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout[-MAX_OUTPUT:],
        "stderr": proc.stderr[-MAX_OUTPUT:],
    }


def parse_json_output(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def cleanup_sessions() -> None:
    now = time.time()
    for child in SESSION_ROOT.iterdir():
        try:
            if child.is_dir() and now - child.stat().st_mtime > SESSION_TTL:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


def tool_lint(arguments: dict) -> dict:
    text = bounded(arguments.get("text", ""), "text")
    forbidden = normalize_forbidden(arguments.get("forbidden"))
    with TemporaryDirectory(prefix="yomiyasu-lint-") as temp:
        folder = Path(temp)
        final_path = folder / "final.md"
        final_path.write_text(text, encoding="utf-8")
        cmd = [sys.executable, "-B", str(LINT), str(final_path), "--json"]
        doc_type = arguments.get("doc_type")
        if doc_type:
            cmd.extend(["--doc-type", str(doc_type)])
        result = run_cmd(cmd, folder)
    parsed = parse_json_output(result["stdout"])
    hits = forbidden_hits(text, forbidden)
    return {
        "status": "FAIL" if hits or result["returncode"] not in (0, 1) else "EXECUTED",
        "upstream_returncode": result["returncode"],
        "upstream": parsed if parsed is not None else result["stdout"],
        "stderr": result["stderr"],
        "forbidden_findings": hits,
        "final_sha256": sha256_text(text),
    }


def tool_check(arguments: dict) -> dict:
    cleanup_sessions()
    source = bounded(arguments.get("source", ""), "source")
    final = bounded(arguments.get("final", ""), "final")
    material_value = arguments.get("material")
    material = bounded(material_value, "material") if material_value is not None else None
    forbidden = normalize_forbidden(arguments.get("forbidden"))

    session_id = uuid.uuid4().hex
    folder = SESSION_ROOT / session_id
    folder.mkdir(parents=True, exist_ok=False)
    source_path = folder / "source.md"
    final_path = folder / "final.md"
    state_path = folder / "state"
    source_path.write_text(source, encoding="utf-8")
    final_path.write_text(final, encoding="utf-8")

    cmd = [
        sys.executable,
        "-B",
        str(CHECK),
        "run",
        "--source",
        str(source_path),
        "--final",
        str(final_path),
        "--state-dir",
        str(state_path),
        "--json",
    ]
    if material is not None:
        material_path = folder / "material.md"
        material_path.write_text(material, encoding="utf-8")
        cmd.extend(["--material", str(material_path)])
    doc_type = arguments.get("doc_type")
    if doc_type:
        cmd.extend(["--doc-type", str(doc_type)])

    result = run_cmd(cmd, folder)
    parsed = parse_json_output(result["stdout"])
    hits = forbidden_hits(final, forbidden)
    meta = {
        "session_id": session_id,
        "final_sha256": sha256_text(final),
        "forbidden": forbidden,
        "created_at": time.time(),
    }
    (folder / "mcp-state.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    upstream_status = parsed.get("status") if isinstance(parsed, dict) else None
    status = "EXECUTED"
    if hits or result["returncode"] != 0 or upstream_status in {"partial", "not_run", "error"}:
        status = "FAIL"

    return {
        "status": status,
        "session_id": session_id,
        "upstream_returncode": result["returncode"],
        "upstream": parsed if parsed is not None else result["stdout"],
        "stderr": result["stderr"],
        "forbidden_findings": hits,
        "final_sha256": meta["final_sha256"],
        "instruction": "If the final text changes, call yomiyasu_check again. If it does not change, call yomiyasu_verify immediately before emission.",
    }


def tool_verify(arguments: dict) -> dict:
    cleanup_sessions()
    session_id = arguments.get("session_id")
    if not isinstance(session_id, str) or not session_id or any(c not in "0123456789abcdef" for c in session_id.lower()):
        raise ValueError("invalid session_id")
    final = bounded(arguments.get("final", ""), "final")
    folder = SESSION_ROOT / session_id
    meta_path = folder / "mcp-state.json"
    final_path = folder / "final.md"
    state_path = folder / "state"
    if not meta_path.exists() or not final_path.exists():
        return {"status": "FAIL", "message": "inspection session not found; rerun yomiyasu_check"}

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    actual_hash = sha256_text(final)
    if actual_hash != meta.get("final_sha256"):
        return {
            "status": "FAIL",
            "message": "final text changed after yomiyasu_check; rerun yomiyasu_check",
            "expected_sha256": meta.get("final_sha256"),
            "actual_sha256": actual_hash,
        }

    final_path.write_text(final, encoding="utf-8")
    cmd = [
        sys.executable,
        "-B",
        str(CHECK),
        "verify",
        "--final",
        str(final_path),
        "--state-dir",
        str(state_path),
        "--json",
    ]
    result = run_cmd(cmd, folder)
    parsed = parse_json_output(result["stdout"])
    upstream_status = parsed.get("status") if isinstance(parsed, dict) else None
    ok = result["returncode"] == 0 and upstream_status not in {"no_record", "final_changed", "tool_changed", "partial", "not_run", "error"}
    return {
        "status": "PASS" if ok else "FAIL",
        "message": "exact inspected final text verified" if ok else "upstream verification did not complete; rerun yomiyasu_check",
        "upstream_returncode": result["returncode"],
        "upstream": parsed if parsed is not None else result["stdout"],
        "stderr": result["stderr"],
        "final_sha256": actual_hash,
    }


def call_tool(name: str, arguments: dict) -> dict:
    if name == "yomiyasu_lint":
        return tool_lint(arguments)
    if name == "yomiyasu_check":
        return tool_check(arguments)
    if name == "yomiyasu_verify":
        return tool_verify(arguments)
    raise ValueError(f"unknown tool: {name}")


class TemporaryDirectory:
    def __init__(self, prefix: str):
        import tempfile
        self._inner = tempfile.TemporaryDirectory(prefix=prefix)

    def __enter__(self):
        return self._inner.__enter__()

    def __exit__(self, exc_type, exc, tb):
        return self._inner.__exit__(exc_type, exc, tb)


class Handler(BaseHTTPRequestHandler):
    server_version = "yomiyasu-mcp/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, status: int, payload: object):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.send_json(200, {"name": "yomiyasu-mcp", "status": "ok", "upstream": "nanaism/yomiyasu v2.5.0"})
            return
        if path == MCP_PATH:
            self.send_json(405, {"error": "use POST for MCP JSON-RPC"})
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != MCP_PATH:
            self.send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if isinstance(body, list):
                responses = [self.handle_rpc(item) for item in body if isinstance(item, dict) and "id" in item]
                self.send_json(200, responses)
                return
            if not isinstance(body, dict):
                raise ValueError("JSON-RPC object required")
            if "id" not in body:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_json(200, self.handle_rpc(body))
        except Exception as exc:
            self.send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": f"{type(exc).__name__}: {exc}"}})

    def handle_rpc(self, request: dict) -> dict:
        rid = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        try:
            if method == "initialize":
                protocol = params.get("protocolVersion") or "2025-06-18"
                result = {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "yomiyasu-mcp", "version": "0.1.0"},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                payload = call_tool(params.get("name"), params.get("arguments") or {})
                result = {
                    "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
                    "isError": payload.get("status") == "ERROR",
                }
            else:
                return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except subprocess.TimeoutExpired:
            payload = {"status": "ERROR", "message": "yomiyasu inspection timed out"}
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": True}}
        except Exception as exc:
            payload = {"status": "ERROR", "message": f"{type(exc).__name__}: {exc}"}
            return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": True}}


def main() -> None:
    if not CHECK.exists() or not LINT.exists():
        raise SystemExit("upstream yomiyasu v2.5.0 is missing; run the Render build command")
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"yomiyasu MCP listening on :{port}{MCP_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
