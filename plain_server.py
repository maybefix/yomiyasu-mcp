from __future__ import annotations

import json
import os
import sys
from http.server import ThreadingHTTPServer

import server


class PlainHandler(server.Handler):
    server_version = "yomiyasu-mobile-gate/0.1"

    def send_json(self, status: int, payload: object):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    if not server.CHECK.exists():
        raise SystemExit("upstream yomiyasu v2.5.0 is missing; run the Render build command")
    port = int(os.environ.get("PORT", "10000"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), PlainHandler)
    print(f"yomiyasu mobile gate listening on :{port}{server.GATE_PREFIX}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
