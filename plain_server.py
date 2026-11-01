from __future__ import annotations

import html
import json
import os
from http.server import ThreadingHTTPServer

import server


class PlainHandler(server.Handler):
    server_version = "yomiyasu-mobile-gate/0.2"

    def send_json(self, status: int, payload: object):
        body = json.dumps(payload, ensure_ascii=False)
        page = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='robots' content='noindex,nofollow'><title>yomiyasu gate</title>"
            "</head><body><main><pre id='payload'>"
            + html.escape(body)
            + "</pre></main></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.end_headers()
        self.wfile.write(page)


def main() -> None:
    if not server.CHECK.exists():
        raise SystemExit("upstream yomiyasu v2.5.0 is missing; run the Render build command")
    port = int(os.environ.get("PORT", "10000"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), PlainHandler)
    print(f"yomiyasu mobile gate listening on :{port}{server.GATE_PREFIX}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
