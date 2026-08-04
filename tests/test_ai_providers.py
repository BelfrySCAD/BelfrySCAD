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
    PRESETS, PROVIDERS, _http_error_message, _normalize_openai_base,
    base_url_key, model_key, preset_for,
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


class TestPresets:
    """Preset services, so the user picks a name instead of typing a URL.
    Base URLs were each confirmed to be a real endpoint (an auth or
    validation error rather than a 404) rather than written from memory."""

    def test_requested_services_present(self):
        ids = {p.id for p in PRESETS}
        assert {"openai", "google", "moonshot", "anthropic", "ollama", "custom"} <= ids

    def test_every_preset_has_a_protocol_we_implement(self):
        assert all(p.protocol in PROVIDERS for p in PRESETS)

    def test_only_anthropic_uses_the_anthropic_protocol(self):
        assert [p.id for p in PRESETS if p.protocol == "anthropic"] == ["anthropic"]

    def test_base_urls_are_absolute_except_custom(self):
        for p in PRESETS:
            if p.id == "custom":
                assert p.base_url == ""     # user supplies it
            else:
                assert p.base_url.startswith("http")

    def test_local_services_dont_require_a_key(self):
        assert preset_for("ollama").needs_key is False

    def test_unknown_id_falls_back(self):
        assert preset_for("nope").id == "openai"

    def test_settings_keys_are_namespaced_per_preset(self):
        # Distinct keys are what let several services be configured at once.
        assert model_key("openai") != model_key("moonshot")
        assert base_url_key("openai") != base_url_key("moonshot")
        assert model_key("openai").startswith("ai/")


class TestListModels:
    def test_parses_and_sorts_ids(self, monkeypatch):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"data":[{"id":"b"},{"id":"a"},{"no":"id"}]}'

        monkeypatch.setattr(ai_providers, "urlopen", lambda req, timeout=None: _Resp())
        assert ai_providers.list_models("http://x/v1") == ["a", "b"]

    def test_sends_key_when_present(self, monkeypatch):
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"data":[]}'

        def fake(req, timeout=None):
            captured["req"] = req
            return _Resp()

        monkeypatch.setattr(ai_providers, "urlopen", fake)
        ai_providers.list_models("http://x/v1", "sk-1")
        assert captured["req"].headers["Authorization"] == "Bearer sk-1"
        assert captured["req"].full_url == "http://x/v1/models"


class TestImageMessages:
    """A viewport grab can't ride inside a tool result in either chat
    protocol, so it's delivered as a following user message. Each protocol
    spells that differently."""

    def test_openai_uses_image_url_data_uri(self):
        msgs = [ChatMessage(role="user", text="look", images=[("QUJD", "image/png")])]
        content = _openai_messages(msgs)[0]["content"]
        assert content[0] == {"type": "text", "text": "look"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"] == "data:image/png;base64,QUJD"

    def test_anthropic_uses_a_base64_source_block(self):
        msgs = [ChatMessage(role="user", text="look", images=[("QUJD", "image/png")])]
        content = _anthropic_messages(msgs)[0]["content"]
        assert content[0] == {"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": "QUJD"}}
        assert content[1] == {"type": "text", "text": "look"}

    def test_image_only_message_omits_empty_text(self):
        msgs = [ChatMessage(role="user", text="", images=[("QUJD", "image/png")])]
        assert len(_openai_messages(msgs)[0]["content"]) == 1
        assert len(_anthropic_messages(msgs)[0]["content"]) == 1

    def test_messages_without_images_are_unchanged(self):
        msgs = [ChatMessage(role="user", text="hi")]
        assert _openai_messages(msgs) == [{"role": "user", "content": "hi"}]
        assert _anthropic_messages(msgs) == [{"role": "user", "content": "hi"}]


class TestModelCapabilities:
    """Ollama's native /api/tags is the only place capability data is
    available -- the OpenAI-protocol /models endpoint reports ids only.
    It matters because a model without "tools" fails the whole request,
    since the pane always sends tools."""

    def _fake_tags(self, monkeypatch, payload):
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return payload

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            return _Resp()

        monkeypatch.setattr(ai_providers, "urlopen", fake)
        return captured

    def test_parses_capabilities(self, monkeypatch):
        self._fake_tags(monkeypatch, b'''{"models":[
            {"name":"a","capabilities":["completion","tools","vision"]},
            {"name":"b","capabilities":["completion"]}]}''')
        caps = ai_providers.list_model_capabilities("http://localhost:11434/v1")
        assert caps["a"] == {"completion", "tools", "vision"}
        assert caps["b"] == {"completion"}

    def test_strips_v1_to_reach_the_native_api(self, monkeypatch):
        cap = self._fake_tags(monkeypatch, b'{"models":[]}')
        ai_providers.list_model_capabilities("http://localhost:11434/v1")
        assert cap["url"] == "http://localhost:11434/api/tags"

    def test_bare_host_also_works(self, monkeypatch):
        cap = self._fake_tags(monkeypatch, b'{"models":[]}')
        ai_providers.list_model_capabilities("http://localhost:11434")
        assert cap["url"] == "http://localhost:11434/api/tags"

    def test_non_ollama_server_yields_nothing(self, monkeypatch):
        def boom(req, timeout=None):
            raise OSError("404")
        monkeypatch.setattr(ai_providers, "urlopen", boom)
        assert ai_providers.list_model_capabilities("https://api.openai.com/v1") == {}

    def test_missing_capabilities_key_is_an_empty_set(self, monkeypatch):
        self._fake_tags(monkeypatch, b'{"models":[{"name":"a"}]}')
        assert ai_providers.list_model_capabilities("http://x/v1")["a"] == set()
