"""Tests for belfryscad.window.ai_providers -- the OpenAI-protocol and
Anthropic streaming clients.

Only the pure SSE parsers and wire-format translators are tested here (fed
canned lines); the network transport itself isn't. The fiddly part these
cover is tool-call argument accumulation: both providers stream a tool
call's JSON arguments as fragments across many chunks, keyed by index, and
reassembling them wrong is the most likely place for a silent bug.
"""
import json

from belfryscad.window.ai_providers import (
    ChatMessage, ToolCall,
    _anthropic_messages, _openai_messages,
    _parse_anthropic_sse_lines, _parse_openai_sse_lines,
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
