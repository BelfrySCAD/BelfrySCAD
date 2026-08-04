"""Tests for belfryscad.window.ai_cli -- the `claude` CLI coprocess
transport -- and ai_chat's transport-resolution order.

The command construction and stdout-event translation are covered here
without spawning a real CLI; the live path (real coprocess + MCP tool
calls + a proposal reaching the review dialog) was verified end-to-end
during development.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from belfryscad.window import ai_chat, ai_cli
from belfryscad.window.ai_cli import ClaudeCliSession, _short_tool_name


class _FakeProc:
    """Stands in for the Popen: records what was written, replays lines."""

    def __init__(self, lines):
        self.written = []
        self.stdout = iter(lines)
        self.stderr = None
        self._alive = True

        class _Stdin:
            closed = False

            def write(_s, data):
                self.written.append(data)

            def flush(_s):
                pass
        self.stdin = _Stdin()

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self._alive = False

    def kill(self):
        self._alive = False

    def wait(self, timeout=None):
        return 0


def _session_with(lines):
    s = ClaudeCliSession("sys", "http://127.0.0.1:1/mcp")
    s._proc = _FakeProc(lines)
    return s


class TestCommand:
    def test_uses_streaming_json_both_ways(self):
        cmd = ClaudeCliSession("sys", "http://x/mcp")._command()
        assert "--input-format" in cmd and "--output-format" in cmd
        assert cmd[cmd.index("--input-format") + 1] == "stream-json"
        assert cmd[cmd.index("--output-format") + 1] == "stream-json"

    def test_mcp_config_points_at_our_server(self):
        cmd = ClaudeCliSession("sys", "http://127.0.0.1:1234/mcp")._command()
        cfg = cmd[cmd.index("--mcp-config") + 1]
        assert "127.0.0.1:1234/mcp" in cfg
        assert "--strict-mcp-config" in cmd

    def test_our_tools_allowed(self):
        cmd = ClaudeCliSession("sys", "http://x/mcp")._command()
        assert "mcp__belfryscad__propose_script_edit" in cmd

    def test_cli_own_file_tools_denied(self):
        # They'd miss unsaved buffers and bypass the review dialog.
        cmd = ClaudeCliSession("sys", "http://x/mcp")._command()
        denied = cmd[cmd.index("--disallowedTools") + 1:]
        for name in ("Write", "Edit", "Bash", "Read"):
            assert name in denied

    def test_system_prompt_replaces_default(self):
        cmd = ClaudeCliSession("PROMPT", "http://x/mcp")._command()
        assert cmd[cmd.index("--system-prompt") + 1] == "PROMPT"

    def test_model_only_passed_when_set(self):
        assert "--model" not in ClaudeCliSession("s", "u")._command()
        assert "--model" in ClaudeCliSession("s", "u", model="claude-opus-5")._command()


class TestSendTurn:
    def test_sends_user_message_as_json_line(self):
        s = _session_with(['{"type":"result","is_error":false}'])
        list(s.send_turn("hello"))
        assert '"role": "user"' in s._proc.written[0]
        assert "hello" in s._proc.written[0]
        assert s._proc.written[0].endswith("\n")

    def test_text_deltas_extracted_from_stream_event(self):
        s = _session_with([
            '{"type":"stream_event","event":{"type":"content_block_delta",'
            '"delta":{"type":"text_delta","text":"hi"}}}',
            '{"type":"result","is_error":false}',
        ])
        evs = list(s.send_turn("x"))
        assert [e.text for e in evs if e.kind == "text_delta"] == ["hi"]
        assert evs[-1].kind == "done"

    def test_tool_use_becomes_report_only_event(self):
        # Must NOT come back as "tool_calls" -- the CLI already ran it via
        # MCP; re-running it in our own loop would double-apply.
        s = _session_with([
            '{"type":"stream_event","event":{"type":"content_block_start",'
            '"content_block":{"type":"tool_use","id":"t",'
            '"name":"mcp__belfryscad__read_open_script"}}}',
            '{"type":"result","is_error":false}',
        ])
        evs = list(s.send_turn("x"))
        assert not any(e.kind == "tool_calls" for e in evs)
        running = [e for e in evs if e.kind == "tool_running"]
        assert running and running[0].text == "read_open_script"

    def test_error_result_surfaces(self):
        s = _session_with(['{"type":"result","is_error":true,"result":"boom"}'])
        evs = list(s.send_turn("x"))
        assert evs[-1].kind == "error" and "boom" in evs[-1].error

    def test_stdout_closing_without_result_is_an_error(self):
        s = _session_with([])
        evs = list(s.send_turn("x"))
        assert evs[-1].kind == "error" and "exited unexpectedly" in evs[-1].error

    def test_malformed_line_skipped(self):
        s = _session_with(["not json", '{"type":"result","is_error":false}'])
        assert list(s.send_turn("x"))[-1].kind == "done"

    def test_cancel_stops_the_process(self):
        # The CLI has no mid-turn cancel over stdin, so cancelling has to
        # drop the whole process -- otherwise its remaining output would
        # bleed into the next turn.
        import threading
        cancel = threading.Event()
        cancel.set()
        s = _session_with([
            '{"type":"stream_event","event":{"type":"content_block_delta",'
            '"delta":{"type":"text_delta","text":"never seen"}}}',
            '{"type":"result","is_error":false}',
        ])
        assert list(s.send_turn("x", cancel)) == []
        assert s._proc is None


class TestShortToolName:
    def test_strips_mcp_prefix(self):
        assert _short_tool_name("mcp__belfryscad__read_open_script") == "read_open_script"

    def test_plain_name_unchanged(self):
        assert _short_tool_name("Read") == "Read"


class TestTransportResolution:
    """Order the user asked for: env var, then the CLI, then a stored key."""

    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
        monkeypatch.setattr(ai_chat, "find_claude_cli", lambda: "/usr/bin/claude")
        monkeypatch.setattr(ai_chat, "get_api_key", lambda p: "sk-stored")
        assert ai_chat.resolve_anthropic_transport() == ("http", "sk-env")

    def test_cli_used_when_no_env_var(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(ai_chat, "find_claude_cli", lambda: "/usr/bin/claude")
        monkeypatch.setattr(ai_chat, "get_api_key", lambda p: "sk-stored")
        assert ai_chat.resolve_anthropic_transport() == ("cli", "")

    def test_stored_key_is_the_last_resort(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(ai_chat, "find_claude_cli", lambda: None)
        monkeypatch.setattr(ai_chat, "get_api_key", lambda p: "sk-stored")
        assert ai_chat.resolve_anthropic_transport() == ("http", "sk-stored")

    def test_nothing_available(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(ai_chat, "find_claude_cli", lambda: None)
        monkeypatch.setattr(ai_chat, "get_api_key", lambda p: None)
        assert ai_chat.resolve_anthropic_transport() == ("none", "")

    def test_blank_env_var_ignored(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
        monkeypatch.setattr(ai_chat, "find_claude_cli", lambda: None)
        monkeypatch.setattr(ai_chat, "get_api_key", lambda p: "sk-stored")
        assert ai_chat.resolve_anthropic_transport() == ("http", "sk-stored")
