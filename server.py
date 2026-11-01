from __future__ import annotations

import base64
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
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
UPSTREAM = ROOT / "upstream"
CHECK = UPSTREAM / "scripts" / "yomiyasu_check.py"
SESSION_ROOT = Path(os.environ.get("YOMIYASU_SESSION_DIR", "/tmp/yomiyasu-web-gate"))
SESSION_ROOT.mkdir(parents=True, exist_ok=True)
MAX_TEXT_BYTES = 1_500_000
MAX_CHUNK_BYTES = 4096
MAX_OUTPUT = 120_000
SESSION_TTL = 6 * 60 * 60
PATH_SECRET = os.environ.get("MCP_PATH_SECRET", "").strip("/")
GATE_PREFIX = f"/{PATH_SECRET}/gate" if PATH_SECRET else "/gate"
VALID_KINDS = {"source", "final", "material", "forbidden", "verify"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_chunk(value: str) -> bytes:
    if len(value) > 7000:
        raise ValueError("encoded chunk is too large")
    padding = "=" * ((4 - len(value) % 4) % 4)
    try:
        data = base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except Exception as exc:
        raise ValueError("invalid base64url chunk") from exc
    if len(data) > MAX_CHUNK_BYTES:
        raise ValueError("decoded chunk is too large")
    return data


def validate_sid(sid: str) -> str:
    if len(sid) != 32 or any(c not in "0123456789abcdef" for c in sid.lower()):
        raise ValueError("invalid session id")
    return sid.lower()


def session_dir(sid: str) -> Path:
    return SESSION_ROOT / validate_sid(sid)


def cleanup_sessions() -> None:
    now = time.time()
    for child in SESSION_ROOT.iterdir():
        try:
            if child.is_dir() and now - child.stat().st_mtime > SESSION_TTL:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


def chunk_dir(folder: Path, kind: str) -> Path:
    if kind not in VALID_KINDS:
        raise ValueError("invalid kind")
    path = folder / "chunks" / kind
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_chunk(folder: Path, kind: str, seq: int, data: bytes) -> None:
    if seq < 0 or seq > 99999:
        raise ValueError("invalid sequence")
    path = chunk_dir(folder, kind) / f"{seq:05d}.part"
    path.write_bytes(data)


def assemble(folder: Path, kind: str, required: bool = True) -> bytes | None:
    directory = folder / "chunks" / kind
    if not directory.exists():
        if required:
            raise ValueError(f"missing {kind}")
        return None
    parts = sorted(directory.glob("*.part"))
    if not parts:
        if required:
            raise ValueError(f"missing {kind}")
        return None
    data = b"".join(p.read_bytes() for p in parts)
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError(f"{kind} is too large")
    return data


def decode_text(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name} is not utf-8") from exc


def forbidden_hits(text: str, words: list[str]) -> list[dict]:
    hits: list[dict] = []
    for word in words:
        if not isinstance(word, str) or not word:
            continue
        start = 0
        while True:
            index = text.find(word, start)
            if index < 0:
                break
            hits.append({"word": word, "offset": index})
            start = index + len(word)
    return hits


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


def parse_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def new_session() -> dict:
    cleanup_sessions()
    sid = uuid.uuid4().hex
    folder = SESSION_ROOT / sid
    folder.mkdir(parents=True, exist_ok=False)
    return {"status": "OK", "session_id": sid, "expires_in_seconds": SESSION_TTL}


def check_session(sid: str, doc_type: str | None) -> dict:
    cleanup_sessions()
    folder = session_dir(sid)
    if not folder.exists():
        return {"status": "FAIL", "message": "session not found"}

    source_bytes = assemble(folder, "source", required=True)
    final_bytes = assemble(folder, "final", required=True)
    material_bytes = assemble(folder, "material", required=False)
    forbidden_bytes = assemble(folder, "forbidden", required=False)

    source = decode_text(source_bytes or b"", "source")
    final = decode_text(final_bytes or b"", "final")
    material = decode_text(material_bytes, "material") if material_bytes is not None else None
    forbidden: list[str] = []
    if forbidden_bytes is not None:
        raw = decode_text(forbidden_bytes, "forbidden")
        parsed = json.loads(raw)
        if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
            raise ValueError("forbidden must decode to a JSON string array")
        forbidden = parsed

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
    if doc_type:
        cmd.extend(["--doc-type", doc_type])

    result = run_cmd(cmd, folder)
    upstream = parse_json(result["stdout"])
    hits = forbidden_hits(final, forbidden)
    upstream_status = upstream.get("status") if isinstance(upstream, dict) else None
    status = "EXECUTED"
    if hits or result["returncode"] != 0 or upstream_status in {"partial", "not_run", "error"}:
        status = "FAIL"

    metadata = {
        "session_id": sid,
        "checked_final_sha256": sha256_bytes(final_bytes or b""),
        "created_at": time.time(),
        "forbidden": forbidden,
    }
    (folder / "gate-state.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    verify_dir = folder / "chunks" / "verify"
    if verify_dir.exists():
        shutil.rmtree(verify_dir, ignore_errors=True)

    return {
        "status": status,
        "session_id": sid,
        "upstream_returncode": result["returncode"],
        "upstream": upstream if upstream is not None else result["stdout"],
        "stderr": result["stderr"],
        "forbidden_findings": hits,
        "final_sha256": metadata["checked_final_sha256"],
        "instruction": "If the final text changes, create a new session and run check again. Otherwise upload the exact outgoing text as kind=verify, then call verify.",
    }


def verify_session(sid: str) -> dict:
    cleanup_sessions()
    folder = session_dir(sid)
    metadata_path = folder / "gate-state.json"
    if not folder.exists() or not metadata_path.exists():
        return {"status": "FAIL", "message": "checked session not found"}

    checked_final = assemble(folder, "final", required=True) or b""
    verify_bytes = assemble(folder, "verify", required=True) or b""
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    checked_hash = sha256_bytes(checked_final)
    verify_hash = sha256_bytes(verify_bytes)
    if checked_hash != metadata.get("checked_final_sha256") or verify_bytes != checked_final:
        return {
            "status": "FAIL",
            "message": "outgoing text differs from the checked final; run a new check",
            "checked_sha256": checked_hash,
            "outgoing_sha256": verify_hash,
        }

    final_path = folder / "final.md"
    final_path.write_bytes(verify_bytes)
    state_path = folder / "state"
    result = run_cmd([
        sys.executable,
        "-B",
        str(CHECK),
        "verify",
        "--final",
        str(final_path),
        "--state-dir",
        str(state_path),
        "--json",
    ], folder)
    upstream = parse_json(result["stdout"])
    upstream_status = upstream.get("status") if isinstance(upstream, dict) else None
    ok = result["returncode"] == 0 and upstream_status not in {
        "no_record", "final_changed", "tool_changed", "partial", "not_run", "error"
    }
    return {
        "status": "PASS" if ok else "FAIL",
        "message": "exact outgoing text verified" if ok else "upstream verify did not complete; run a new check",
        "upstream_returncode": result["returncode"],
        "upstream": upstream if upstream is not None else result["stdout"],
        "stderr": result["stderr"],
        "final_sha256": verify_hash,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "yomiyasu-web-gate/0.2"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, status: int, payload: object):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.end_headers()
        self.wfile.write(raw)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path.rstrip("/")
        query = parse_qs(parsed_url.query, keep_blank_values=True)
        try:
            if path == "":
                self.send_json(200, {"name": "yomiyasu-web-gate", "status": "ok", "upstream": "nanaism/yomiyasu v2.5.0"})
                return
            if path == f"{GATE_PREFIX}/new":
                self.send_json(200, new_session())
                return
            if path == f"{GATE_PREFIX}/put":
                sid = validate_sid(query.get("sid", [""])[0])
                kind = query.get("kind", [""])[0]
                seq = int(query.get("seq", ["-1"])[0])
                encoded = query.get("data", [""])[0]
                folder = session_dir(sid)
                if not folder.exists():
                    self.send_json(404, {"status": "FAIL", "message": "session not found"})
                    return
                data = decode_chunk(encoded)
                write_chunk(folder, kind, seq, data)
                self.send_json(200, {"status": "OK", "session_id": sid, "kind": kind, "seq": seq, "bytes": len(data)})
                return
            if path == f"{GATE_PREFIX}/check":
                sid = validate_sid(query.get("sid", [""])[0])
                doc_type = query.get("doc_type", [None])[0]
                self.send_json(200, check_session(sid, doc_type))
                return
            if path == f"{GATE_PREFIX}/verify":
                sid = validate_sid(query.get("sid", [""])[0])
                self.send_json(200, verify_session(sid))
                return
            self.send_json(404, {"status": "FAIL", "message": "not found"})
        except subprocess.TimeoutExpired:
            self.send_json(504, {"status": "FAIL", "message": "yomiyasu inspection timed out"})
        except Exception as exc:
            self.send_json(400, {"status": "FAIL", "message": f"{type(exc).__name__}: {exc}"})

    def do_POST(self):
        self.send_json(405, {"status": "FAIL", "message": "POST is disabled; use the chunked GET gate"})


def main() -> None:
    if not CHECK.exists():
        raise SystemExit("upstream yomiyasu v2.5.0 is missing; run the Render build command")
    port = int(os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"yomiyasu web gate listening on :{port}{GATE_PREFIX}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
