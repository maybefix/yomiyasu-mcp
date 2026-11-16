# yomiyasu-mcp

Remote MCP wrapper for the official [nanaism/yomiyasu](https://github.com/nanaism/yomiyasu) v3.1.0.

The Render build step still initially clones v2.5.0. At process startup, `server.ensure_upstream()` fetches the v3.1.0 tag, checks out its verified commit `db78b9bc5e868549b517a037df7a4d1c93edac23`, and fails startup on any version mismatch. This also applies to the mobile and dependency MCP entrypoints. The ChatGPT plugin requires explicit yomiyasu invocation.

Upstream tag: [v3.1.0](https://github.com/nanaism/yomiyasu/releases/tag/v3.1.0).
