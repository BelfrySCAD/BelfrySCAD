"""Tests for belfryscad.window.ai_providers -- the OpenAI-protocol and
Anthropic streaming clients.

Only the pure SSE parsers and wire-format translators are tested here (fed
canned lines); the network transport itself isn't. The fiddly part these
cover is tool-call argument accumulation: both providers stream a tool
call's JSON arguments as fragments across many chunks, keyed by index, and
reassembling them wrong is the most likely place for a silent bug.
"""
import json

from belfryscad.window import ai_providers
from belfryscad.window.ai_providers import (
    ChatMessage, ToolCall,
    _anthropic_messages, _openai_messages,
    _http_error_message, _normalize_openai_base,
    _parse_anthropic_sse_lines, _parse_openai_sse_lines,
    stream_openai,
)


def _events(parser, lines):
    return list(parser(lines))


class TestOpenAIParser:
    def test_text_deltas(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"Hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            "data: [DONE]",
        ]
        evs = _events(_parse_openai_sse_lines, lines)
        assert [e.text for e in evs if e.kind == "text_delta"] == ["Hel", "lo"]
        assert evs[-1].kind == "done"

    def test_tool_call_arguments_accumulate_across_chunks(self):
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_abc",'
            '"type":"function","function":{"name":"read_open_script","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"{\\"id\\": 1"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"arguments":"2}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
        ]
        evs = _events(_parse_openai_sse_lines, lines)
        calls = next(e.tool_calls for e in evs if e.kind == "tool_calls")
        assert calls == [ToolCall(id="call_abc", name="read_open_script",
                                   arguments={"id": 12})]

    def test_two_parallel_tool_calls_kept_separate_by_index(self):
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":['
            '{"index":0,"id":"a","function":{"name":"list_open_scripts","arguments":"{}"}},'
            '{"index":1,"id":"b","function":{"name":"list_library_files","arguments":"{}"}}'
            ']}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        ]
        evs = _events(_parse_openai_sse_lines, lines)
        calls = next(e.tool_calls for e in evs if e.kind == "tool_calls")
        assert [c.id for c in calls] == ["a", "b"]
        assert [c.name for c in calls] == ["list_open_scripts", "list_library_files"]

    def test_tool_calls_emitted_even_without_finish_reason(self):
        # Defensive: a stream that ends without the finish_reason marker
        # must still surface the accumulated call rather than dropping it.
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"a",'
            '"function":{"name":"list_open_scripts","arguments":"{}"}}]}}]}',
            "data: [DONE]",
        ]
        evs = _events(_parse_openai_sse_lines, lines)
        assert any(e.kind == "tool_calls" for e in evs)

    def test_malformed_json_chunk_is_skipped(self):
        lines = [
            "data: {not json",
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            "data: [DONE]",
        ]
        evs = _events(_parse_openai_sse_lines, lines)
        assert [e.text for e in evs if e.kind == "text_delta"] == ["ok"]

    def test_non_data_lines_ignored(self):
        lines = ["", ": keep-alive", 'data: {"choices":[{"delta":{"content":"x"}}]}']
        evs = _events(_parse_openai_sse_lines, lines)
        assert [e.text for e in evs if e.kind == "text_delta"] == ["x"]


class TestAnthropicParser:
    def test_text_deltas(self):
        lines = [
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"Hel"}}',
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"lo"}}',
            'data: {"type":"message_stop"}',
        ]
        evs = _events(_parse_anthropic_sse_lines, lines)
        assert [e.text for e in evs if e.kind == "text_delta"] == ["Hel", "lo"]
        assert evs[-1].kind == "done"

    def test_tool_call_partial_json_accumulates(self):
        lines = [
            'data: {"type":"content_block_start","index":1,"content_block":'
            '{"type":"tool_use","id":"toolu_01","name":"read_open_script","input":{}}}',
            'data: {"type":"content_block_delta","index":1,'
            '"delta":{"type":"input_json_delta","partial_json":"{\\"id\\": 1"}}',
            'data: {"type":"content_block_delta","index":1,'
            '"delta":{"type":"input_json_delta","partial_json":"2}"}}',
            'data: {"type":"content_block_stop","index":1}',
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
        ]
        evs = _events(_parse_anthropic_sse_lines, lines)
        calls = next(e.tool_calls for e in evs if e.kind == "tool_calls")
        assert calls == [ToolCall(id="toolu_01", name="read_open_script",
                                   arguments={"id": 12})]

    def test_text_and_tool_use_blocks_interleaved(self):
        lines = [
            'data: {"type":"content_block_start","index":0,'
            '"content_block":{"type":"text","text":""}}',
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"Let me look."}}',
            'data: {"type":"content_block_start","index":1,"content_block":'
            '{"type":"tool_use","id":"toolu_02","name":"list_open_scripts","input":{}}}',
            'data: {"type":"content_block_delta","index":1,'
            '"delta":{"type":"input_json_delta","partial_json":"{}"}}',
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
        ]
        evs = _events(_parse_anthropic_sse_lines, lines)
        assert [e.text for e in evs if e.kind == "text_delta"] == ["Let me look."]
        calls = next(e.tool_calls for e in evs if e.kind == "tool_calls")
        assert calls[0].name == "list_open_scripts"

    def test_error_event_stops_the_stream(self):
        lines = [
            'data: {"type":"error","error":{"message":"overloaded"}}',
            'data: {"type":"content_block_delta","index":0,'
            '"delta":{"type":"text_delta","text":"never seen"}}',
        ]
        evs = _events(_parse_anthropic_sse_lines, lines)
        assert evs[-1].kind == "error" and evs[-1].error == "overloaded"
        assert not any(e.kind == "text_delta" for e in evs)

    def test_empty_arguments_degrade_to_empty_dict(self):
        lines = [
            'data: {"type":"content_block_start","index":0,"content_block":'
            '{"type":"tool_use","id":"t","name":"list_open_scripts","input":{}}}',
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
        ]
        evs = _events(_parse_anthropic_sse_lines, lines)
        calls = next(e.tool_calls for e in evs if e.kind == "tool_calls")
        assert calls[0].arguments == {}


class TestWireFormatTranslation:
    def test_openai_tool_result_shape(self):
        msgs = [ChatMessage(role="tool", text="cube(1);",
                            tool_call_id="call_1", tool_name="read_open_script")]
        assert _openai_messages(msgs) == [
            {"role": "tool", "tool_call_id": "call_1", "content": "cube(1);"},
        ]

    def test_openai_assistant_tool_calls_shape(self):
        msgs = [ChatMessage(role="assistant", text="",
                            tool_calls=[ToolCall("c1", "read_open_script", {"id": 1})])]
        wire = _openai_messages(msgs)[0]
        assert wire["role"] == "assistant"
        assert wire["tool_calls"][0]["function"]["name"] == "read_open_script"
        assert json.loads(wire["tool_calls"][0]["function"]["arguments"]) == {"id": 1}

    def test_anthropic_tool_result_is_a_user_block(self):
        msgs = [ChatMessage(role="tool", text="cube(1);", tool_call_id="toolu_1")]
        assert _anthropic_messages(msgs) == [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "cube(1);"},
            ]},
        ]

    def test_anthropic_assistant_tool_use_blocks(self):
        msgs = [ChatMessage(role="assistant", text="Looking.",
                            tool_calls=[ToolCall("t1", "list_open_scripts", {})])]
        blocks = _anthropic_messages(msgs)[0]["content"]
        assert blocks[0] == {"type": "text", "text": "Looking."}
        assert blocks[1] == {"type": "tool_use", "id": "t1",
                             "name": "list_open_scripts", "input": {}}

    def test_plain_messages_pass_through(self):
        msgs = [ChatMessage(role="user", text="hi")]
        assert _openai_messages(msgs) == [{"role": "user", "content": "hi"}]
        assert _anthropic_messages(msgs) == [{"role": "user", "content": "hi"}]


class TestOpenAIRequestBuilding:
    """The Authorization header must be omitted entirely when no key is set,
    so local OpenAI-protocol servers (Ollama, LM Studio, llama.cpp) work --
    verified live against a real Ollama server during development."""

    def _capture(self, monkeypatch, api_key):
        captured = {}

        class _FakeResp:
            def __enter__(self):
                return iter([b'data: [DONE]\n'])

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return _FakeResp()

        monkeypatch.setattr(ai_providers, "urlopen", fake_urlopen)
        list(stream_openai("http://localhost:11434/v1", api_key, "m",
                           [ChatMessage(role="user", text="hi")], []))
        return captured["req"]

    def test_no_auth_header_when_key_is_empty(self, monkeypatch):
        req = self._capture(monkeypatch, "")
        assert not any(h.lower() == "authorization" for h in req.headers)

    def test_auth_header_present_when_key_is_set(self, monkeypatch):
        req = self._capture(monkeypatch, "sk-abc")
        assert req.headers["Authorization"] == "Bearer sk-abc"

    def test_url_is_chat_completions(self, monkeypatch):
        req = self._capture(monkeypatch, "")
        assert req.full_url == "http://localhost:11434/v1/chat/completions"

    def test_trailing_slash_in_base_url_tolerated(self, monkeypatch):
        captured = {}

        class _FakeResp:
            def __enter__(self):
                return iter([b'data: [DONE]\n'])

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return _FakeResp()

        monkeypatch.setattr(ai_providers, "urlopen", fake_urlopen)
        list(stream_openai("http://localhost:11434/v1/", "", "m",
                           [ChatMessage(role="user", text="hi")], []))
        assert captured["req"].full_url == "http://localhost:11434/v1/chat/completions"


class TestBaseUrlNormalization:
    """A bare host (no path) gets the conventional /v1 appended. Typing
    "http://localhost:11434" for Ollama is the natural thing to do, and
    without this it fails with a router-level "404 page not found" that
    names nothing useful. Confirmed against a real Ollama server: the bare
    form 404s, the /v1 form works."""

    def test_bare_host_gets_v1(self):
        assert _normalize_openai_base("http://localhost:11434") == "http://localhost:11434/v1"

    def test_trailing_slash_bare_host_gets_v1(self):
        assert _normalize_openai_base("http://localhost:11434/") == "http://localhost:11434/v1"

    def test_existing_v1_untouched(self):
        assert _normalize_openai_base("https://api.openai.com/v1") == "https://api.openai.com/v1"

    def test_custom_path_untouched(self):
        assert _normalize_openai_base("http://host/openai/v1") == "http://host/openai/v1"

    def test_whitespace_stripped(self):
        assert _normalize_openai_base("  http://localhost:11434  ") == "http://localhost:11434/v1"


class _FakeHTTPError:
    def __init__(self, code):
        self.code = code


class _FakeReq:
    full_url = "http://localhost:11434/chat/completions"


class TestHttpErrorMessages:
    """Ollama reports "this model can't do tool calling" two different ways;
    the second is a Jinja traceback from its tool-parser generation that
    names nothing actionable. Both were reproduced against a real Ollama
    server (kitsune-qwable and Silvia respectively)."""

    def test_explicit_no_tools_support_is_explained(self):
        detail = '{"error":{"message":"Silvia:latest does not support tools"}}'
        msg = _http_error_message(_FakeHTTPError(400), _FakeReq(), detail)
        assert msg.startswith("This model can't do tool calling")
        assert "tools" in msg

    def test_jinja_parser_failure_is_explained(self):
        detail = ("Unable to generate parser for this template. Automatic "
                  "generate parser for this template failed: raise_exception")
        msg = _http_error_message(_FakeHTTPError(400), _FakeReq(), detail)
        assert msg.startswith("This model can't do tool calling")

    def test_404_names_the_url(self):
        msg = _http_error_message(_FakeHTTPError(404), _FakeReq(), "404 page not found")
        assert "http://localhost:11434/chat/completions" in msg
        assert "Base URL" in msg

    def test_401_points_at_the_api_key(self):
        msg = _http_error_message(_FakeHTTPError(401), _FakeReq(), "unauthorized")
        assert "API key" in msg

    def test_other_errors_pass_detail_through(self):
        msg = _http_error_message(_FakeHTTPError(500), _FakeReq(), "boom")
        assert "HTTP 500" in msg and "boom" in msg
