"""Tests for belfryscad.window.ai_mcp -- the in-process MCP server that
exposes our tools to the `claude` CLI coprocess.

Exercises the JSON-RPC surface directly via dispatch(), no socket needed.
The full path (real CLI connecting over HTTP and calling tools) was
verified live during development.
"""
import base64
import pathlib

from belfryscad.window.ai_mcp import PROTOCOL_VERSION, McpToolServer
from belfryscad.window.ai_tools import AIToolContext, TabSnapshot


def _server(tmp_path, tabs=None, proposals=None):
    srv = McpToolServer.__new__(McpToolServer)   # no socket bind needed
    srv.context = AIToolContext(
        library_dir=tmp_path,
        open_tabs=tabs or [],
        on_proposal=(proposals.append if proposals is not None else None),
    )
    return srv


def _call(srv, method, params=None, req_id=1):
    return srv.dispatch({"jsonrpc": "2.0", "id": req_id,
                         "method": method, "params": params or {}})


class TestHandshake:
    def test_initialize(self, tmp_path):
        r = _call(_server(tmp_path), "initialize")
        assert r["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert "tools" in r["result"]["capabilities"]

    def test_initialized_notification_gets_no_reply(self, tmp_path):
        srv = _server(tmp_path)
        assert srv.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    def test_any_id_less_request_is_a_notification(self, tmp_path):
        assert _server(tmp_path).dispatch({"jsonrpc": "2.0", "method": "tools/list"}) is None

    def test_unknown_method_errors(self, tmp_path):
        r = _call(_server(tmp_path), "nonsense/thing")
        assert r["error"]["code"] == -32601


class TestToolsList:
    def test_lists_every_tool(self, tmp_path):
        tools = _call(_server(tmp_path), "tools/list")["result"]["tools"]
        assert {t["name"] for t in tools} == {
            "list_library_files", "read_library_file", "list_open_scripts",
            "read_open_script", "propose_script_edit", "propose_new_script",
            "view_viewport", "describe_geometry", "schedule_followup", "read_console",
        }

    def test_uses_mcp_input_schema_key(self, tmp_path):
        # MCP calls it inputSchema; OpenAI calls the same JSON Schema
        # "parameters". The shared spec is stored under json_schema.
        tools = _call(_server(tmp_path), "tools/list")["result"]["tools"]
        assert all("inputSchema" in t for t in tools)


class TestToolsCall:
    def test_reads_an_open_script(self, tmp_path):
        srv = _server(tmp_path, tabs=[TabSnapshot(1, "a.scad", None, False, "cube(1);")])
        r = _call(srv, "tools/call",
                  {"name": "read_open_script", "arguments": {"id": 1}})
        assert r["result"]["content"][0]["text"] == "cube(1);"
        assert r["result"]["isError"] is False

    def test_proposal_reaches_the_callback(self, tmp_path):
        proposals = []
        srv = _server(tmp_path,
                      tabs=[TabSnapshot(1, "a.scad", None, False, "cube(1);\n")],
                      proposals=proposals)
        r = _call(srv, "tools/call", {"name": "propose_script_edit", "arguments": {
            "id": 1, "new_content": "cube(2);\n", "summary": "Bigger"}})
        assert "review" in r["result"]["content"][0]["text"]
        assert len(proposals) == 1 and proposals[0].new_content == "cube(2);\n"

    def test_tool_error_is_content_not_jsonrpc_error(self, tmp_path):
        # The model must be able to read and correct its own mistake.
        srv = _server(tmp_path)
        r = _call(srv, "tools/call",
                  {"name": "read_open_script", "arguments": {"id": 99}})
        assert "error" not in r
        assert r["result"]["isError"] is True
        assert "no open script" in r["result"]["content"][0]["text"]

    def test_scad_restriction_still_applies(self, tmp_path):
        (tmp_path / "secret.txt").write_text("hunter2")
        srv = _server(tmp_path)
        r = _call(srv, "tools/call",
                  {"name": "read_library_file", "arguments": {"path": "secret.txt"}})
        text = r["result"]["content"][0]["text"]
        assert "only .scad files" in text and "hunter2" not in text

    def test_no_context_is_reported_not_crashed(self, tmp_path):
        srv = _server(tmp_path)
        srv.context = None
        r = _call(srv, "tools/call", {"name": "list_open_scripts", "arguments": {}})
        assert "no active BelfrySCAD session" in r["result"]["content"][0]["text"]


class TestImageResults:
    def test_image_returned_as_mcp_image_content(self, tmp_path):
        # MCP, unlike either chat protocol, carries images in a tool result
        # directly -- so the CLI transport needs no follow-up user message.
        srv = _server(tmp_path)
        srv.context.viewport_png = b"png-bytes"
        r = _call(srv, "tools/call", {"name": "view_viewport", "arguments": {}})
        block = r["result"]["content"][0]
        assert block["type"] == "image"
        assert block["mimeType"] == "image/png"
        assert base64.b64decode(block["data"]) == b"png-bytes"
        assert r["result"]["isError"] is False

    def test_missing_render_still_reports_as_text(self, tmp_path):
        srv = _server(tmp_path)
        r = _call(srv, "tools/call", {"name": "view_viewport", "arguments": {}})
        assert r["result"]["content"][0]["type"] == "text"
        assert r["result"]["isError"] is True
