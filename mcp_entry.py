from __future__ import annotations

import urllib.request

URL = "https://raw.githubusercontent.com/maybefix/yomiyasu-mcp/9f35738de28486bcae8b653f084d2f6012b105e2/server.py"

with urllib.request.urlopen(URL, timeout=30) as response:
    source = response.read()

code = compile(source, URL, "exec")
exec(code, {"__name__": "__main__", "__file__": "mcp_entry.py"})
