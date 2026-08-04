"""A minimal in-process MCP server exposing this app's AI tools over
localhost HTTP, so the `claude` CLI coprocess gets exactly the same six
tools as the direct-API path -- including live unsaved editor buffers and
the review-before-apply proposal flow.

The CLI can't be handed our tool schemas directly (it brings its own
Read/Write/Edit), and its own file tools would both miss unsaved buffers
and write straight to disk, bypassing the review dialog. Pointing it at
this server with --strict-mcp-config is what keeps the two transports
behaviourally identical.

Only the slice of MCP that Claude Code actually exercises is implemented:
initialize, notifications/initialized, tools/list, tools/call. It binds to
127.0.0.1 on an ephemeral port, so it isn't reachable off the machine.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from belfryscad.window.ai_tools import TOOLS, AIToolContext, ToolImage, run_tool

PROTOCOL_VERSION = "2024-11-05"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass   # don't spew request lines onto the app's stderr

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "parse error"}})
            return
        reply = self.server.dispatch(req)
        if reply is None:
            # A notification: acknowledge with 202 and no JSON-RPC body.
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(reply)

    def _send(self, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class McpToolServer(ThreadingHTTPServer):
    """Serves TOOLS over MCP. `context` is swapped per turn by the pane --
    the same frozen AIToolContext snapshot the direct-API path uses, so
    tool handlers still only ever see plain data (never a live Qt widget)
    even though they now run on an HTTP worker thread."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.context: AIToolContext | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/mcp"

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self.serve_forever,
                                            daemon=True)
            self._thread.start()

    def stop(self):
        self.shutdown()
        self.server_close()
        self._thread = None

    # -- JSON-RPC ----------------------------------------------------------

    def dispatch(self, req: dict) -> dict | None:
        method = req.get("method")
        req_id = req.get("id")
        if method == "notifications/initialized" or req_id is None:
            return None      # notification: no reply
        if method == "initialize":
            return self._ok(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "belfryscad", "version": "1"},
            })
        if method == "tools/list":
            return self._ok(req_id, {"tools": [
                {"name": t["name"], "description": t["description"],
                 "inputSchema": t["json_schema"]}
                for t in TOOLS
            ]})
        if method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name", "")
            args = params.get("arguments") or {}
            if self.context is None:
                result = "Error: no active BelfrySCAD session."
            else:
                result = run_tool(self.context, name, args)
            if isinstance(result, ToolImage):
                # MCP, unlike either chat protocol, does let a tool result
                # carry an image directly.
                return self._ok(req_id, {"content": [
                    {"type": "image", "data": result.data_b64,
                     "mimeType": result.mime},
                ], "isError": False})
            # Tool-level failures come back as ordinary content (so the
            # model can read and correct them), not as JSON-RPC errors.
            return self._ok(req_id, {
                "content": [{"type": "text", "text": result}],
                "isError": result.startswith("Error:"),
            })
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"unknown method {method}"}}

    @staticmethod
    def _ok(req_id, result) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
