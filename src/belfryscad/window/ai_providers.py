"""Streaming chat clients for the AI pane: one OpenAI-protocol, one
Anthropic. Both translate a shared ChatMessage list to/from their own wire
format and yield the same StreamEvent stream, so callers never branch on
which provider is active.

HTTP is stdlib urllib (matching library_manager.py -- this project has no
`requests` dependency); SSE is read line-by-line off the file-like response.

Each provider's SSE decoding is split into a pure `_parse_*_sse_lines`
generator taking an iterable of already-decoded lines, so the fiddly part
-- accumulating tool-call arguments that arrive as JSON fragments across
many chunks -- is unit-testable with canned text, no network involved.

The two wire formats are different enough that sharing the accumulator
itself would be an abstraction with no payoff; only the *output* is shared:
  OpenAI    delta.tool_calls[] keyed by array index; id/name arrive once,
            function.arguments streams as JSON string fragments; the call
            is complete at finish_reason == "tool_calls".
  Anthropic content_block_start carries a tool_use block's id/name up
            front; only input_json_delta/partial_json fragments accumulate,
            keyed by content-block index, parsed at content_block_stop.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 4096
_TIMEOUT = 120


@dataclass(frozen=True)
class Preset:
    """A known service, so the user picks a name instead of typing a URL.

    `protocol` selects the wire format ("openai" or "anthropic"); every
    non-Anthropic service here speaks the OpenAI protocol. `base_url` is a
    starting value the user can still edit -- "custom" exists precisely for
    anything not listed. `model_hint` is only placeholder text: model IDs
    change far too often to hard-code as defaults.
    """
    id: str
    label: str
    protocol: str
    base_url: str
    model_hint: str
    needs_key: bool = True
    # Distinct from needs_key. Anthropic does not *need* one (the CLI or
    # ANTHROPIC_API_KEY may cover it) but can still use a key; Ollama serves
    # locally with no auth at all, so the field is dead space there.
    accepts_key: bool = True


# Base URLs verified reachable (an auth/validation error rather than a 404).
PRESETS: list[Preset] = [
    Preset("anthropic", "Claude (Anthropic)", "anthropic",
           "https://api.anthropic.com/v1", "e.g. claude-opus-5", needs_key=False),
    Preset("openai", "ChatGPT (OpenAI)", "openai",
           "https://api.openai.com/v1", "e.g. gpt-4o"),
    Preset("google", "Gemma / Gemini (Google AI)", "openai",
           "https://generativelanguage.googleapis.com/v1beta/openai",
           "e.g. gemma-3-27b-it"),
    Preset("moonshot", "Kimi (Moonshot)", "openai",
           "https://api.moonshot.ai/v1", "e.g. kimi-k2-0711-preview"),
    Preset("ollama", "Ollama (local)", "openai",
           "http://localhost:11434/v1", "a model with the 'tools' capability",
           needs_key=False, accepts_key=False),
    # CLI-only: Copilot authenticates through the CLI's own GitHub login,
    # so there is no base URL or key for it. protocol is nominal -- the
    # chat pane routes on the id before it ever looks at the protocol.
    Preset("copilot", "GitHub Copilot (CLI)", "openai",
           "", "use 'auto' to let Copilot pick",
           needs_key=False, accepts_key=False),
    Preset("custom", "Custom (OpenAI-protocol)", "openai",
           "", "any model the server offers"),
]

PRESETS_BY_ID = {p.id: p for p in PRESETS}


def preset_for(preset_id: str) -> Preset:
    return PRESETS_BY_ID.get(preset_id, PRESETS_BY_ID["openai"])


# Per-preset settings keys. Kept here (not in preferences.py) so the chat
# pane and the Preferences dialog can't drift apart on where a value lives.
def model_key(preset_id: str) -> str:
    return f"ai/model/{preset_id}"


def base_url_key(preset_id: str) -> str:
    return f"ai/baseUrl/{preset_id}"


def list_model_capabilities(base_url: str) -> dict[str, set[str]]:
    """{model id: capabilities} from an Ollama server's native /api/tags.

    The OpenAI-protocol /models endpoint reports ids only, so this is the
    one place capability data is actually available -- and it matters:
    a model without "tools" fails the whole request, since the chat pane
    always sends tools. Returns {} for any server that isn't Ollama.
    """
    root = _normalize_openai_base(base_url)
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    try:
        with urlopen(Request(root + "/api/tags"), timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:      # noqa: BLE001 -- not Ollama, or not reachable
        return {}
    caps = {}
    for m in data.get("models", []):
        name = m.get("name") or m.get("model")
        if name:
            caps[name] = set(m.get("capabilities") or [])
    return caps


def list_models(base_url: str, api_key: str = "") -> list[str]:
    """Model IDs from an OpenAI-protocol server's /models endpoint, for the
    Preferences model dropdown. Raises on failure -- the caller reports it
    and leaves the field free-text, since not every server implements this
    (Anthropic doesn't use this protocol at all)."""
    url = _normalize_openai_base(base_url) + "/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(url, headers=headers)
    with urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
    return sorted(ids)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatMessage:
    role: str                                   # "user" | "assistant" | "tool"
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None             # set when role == "tool"
    tool_name: str | None = None                # OpenAI wants this on tool results
    # (base64, mime) images carried by a user message. Tool results can't
    # hold images in the OpenAI protocol, so a viewport grab is delivered
    # as a follow-up user message instead -- see ai_chat's worker.
    images: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class StreamEvent:
    kind: str                                   # "text_delta"|"tool_calls"|"done"|"error"
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    error: str | None = None


def _decoded_lines(resp) -> Iterator[str]:
    for raw in resp:
        yield raw.decode("utf-8", errors="replace").rstrip("\r\n")


def _finish(acc: dict) -> list[ToolCall]:
    """Turn an accumulator of {index: {id, name, json_parts}} into ToolCalls,
    ordered by index. Malformed/empty argument JSON degrades to {} rather
    than raising -- run_tool reports the resulting TypeError back to the
    model, which is more useful than killing the turn."""
    calls = []
    for _idx, slot in sorted(acc.items()):
        raw = "".join(slot["json_parts"]).strip()
        try:
            args = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            args = {}
        calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=args))
    return calls


# --------------------------------------------------------------------------
# OpenAI protocol
# --------------------------------------------------------------------------

def _openai_messages(messages: list[ChatMessage]) -> list[dict]:
    out = []
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id,
                        "content": m.text})
        elif m.role == "assistant" and m.tool_calls:
            out.append({
                "role": "assistant",
                "content": m.text or None,
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name,
                                  "arguments": json.dumps(c.arguments)}}
                    for c in m.tool_calls
                ],
            })
        elif m.images:
            content = ([{"type": "text", "text": m.text}] if m.text else []) + [
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}}
                for b64, mime in m.images
            ]
            out.append({"role": m.role, "content": content})
        else:
            out.append({"role": m.role, "content": m.text})
    return out


def _parse_openai_sse_lines(lines: Iterable[str]) -> Iterator[StreamEvent]:
    acc: dict[int, dict] = {}
    for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            if delta.get("content"):
                yield StreamEvent(kind="text_delta", text=delta["content"])
            for tc in delta.get("tool_calls") or []:
                slot = acc.setdefault(tc.get("index", 0),
                                      {"id": "", "name": "", "json_parts": []})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["json_parts"].append(fn["arguments"])
            if choice.get("finish_reason") == "tool_calls" and acc:
                yield StreamEvent(kind="tool_calls", tool_calls=_finish(acc))
                acc = {}
    if acc:
        yield StreamEvent(kind="tool_calls", tool_calls=_finish(acc))
    yield StreamEvent(kind="done")


def _normalize_openai_base(base_url: str) -> str:
    """Add the conventional /v1 when the URL is a bare host.

    The OpenAI convention is that the base URL already includes the version
    segment, but a bare "http://localhost:11434" is the natural thing to
    type for a local server, and it fails with an unhelpful router-level
    "404 page not found" rather than anything naming the real problem. A
    URL that already carries a path (/v1, /openai/v1, ...) is left alone.
    """
    url = base_url.strip().rstrip("/")
    path = urlsplit(url).path
    return url if path else url + "/v1"


def stream_openai(base_url: str, api_key: str, model: str,
                  messages: list[ChatMessage], tools: list[dict],
                  system: str = "",
                  cancel: threading.Event | None = None) -> Iterator[StreamEvent]:
    wire = _openai_messages(messages)
    if system:
        wire = [{"role": "system", "content": system}] + wire
    body = {
        "model": model,
        "messages": wire,
        "stream": True,
    }
    if tools:
        body["tools"] = [
            {"type": "function",
             "function": {"name": t["name"], "description": t["description"],
                          "parameters": t["json_schema"]}}
            for t in tools
        ]
    headers = {"Content-Type": "application/json"}
    if api_key:
        # Omitted entirely when unset, rather than sent empty: local
        # OpenAI-protocol servers (Ollama, LM Studio, llama.cpp) need no
        # key, and some reject a malformed Bearer header outright.
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(
        _normalize_openai_base(base_url) + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    yield from _stream_request(req, _parse_openai_sse_lines, cancel)


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------

def _anthropic_messages(messages: list[ChatMessage]) -> list[dict]:
    """Anthropic wants tool results as a user-role content block, and an
    assistant's tool calls as tool_use blocks alongside any text."""
    out = []
    for m in messages:
        if m.role == "tool":
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": m.tool_call_id,
                 "content": m.text},
            ]})
        elif m.role == "assistant" and m.tool_calls:
            blocks = []
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            blocks += [{"type": "tool_use", "id": c.id, "name": c.name,
                        "input": c.arguments} for c in m.tool_calls]
            out.append({"role": "assistant", "content": blocks})
        elif m.images:
            blocks = [{"type": "image", "source": {
                "type": "base64", "media_type": mime, "data": b64}}
                for b64, mime in m.images]
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            out.append({"role": m.role, "content": blocks})
        else:
            out.append({"role": m.role, "content": m.text})
    return out


def _parse_anthropic_sse_lines(lines: Iterable[str]) -> Iterator[StreamEvent]:
    acc: dict[int, dict] = {}
    for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        try:
            ev = json.loads(payload)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") == "tool_use":
                acc[ev.get("index", 0)] = {"id": block.get("id", ""),
                                           "name": block.get("name", ""),
                                           "json_parts": []}
        elif etype == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                yield StreamEvent(kind="text_delta", text=delta["text"])
            elif delta.get("type") == "input_json_delta":
                slot = acc.get(ev.get("index", 0))
                if slot is not None and delta.get("partial_json"):
                    slot["json_parts"].append(delta["partial_json"])
        elif etype == "message_delta":
            if ((ev.get("delta") or {}).get("stop_reason") == "tool_use") and acc:
                yield StreamEvent(kind="tool_calls", tool_calls=_finish(acc))
                acc = {}
        elif etype == "error":
            msg = (ev.get("error") or {}).get("message", "unknown error")
            yield StreamEvent(kind="error", error=msg)
            return
    if acc:
        yield StreamEvent(kind="tool_calls", tool_calls=_finish(acc))
    yield StreamEvent(kind="done")


def stream_anthropic(base_url: str, api_key: str, model: str,
                     messages: list[ChatMessage], tools: list[dict],
                     system: str = "",
                     cancel: threading.Event | None = None) -> Iterator[StreamEvent]:
    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": _anthropic_messages(messages),
        "stream": True,
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [
            {"name": t["name"], "description": t["description"],
             "input_schema": t["json_schema"]}
            for t in tools
        ]
    req = Request(
        (base_url or "https://api.anthropic.com/v1").rstrip("/") + "/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "x-api-key": api_key,
                 "anthropic-version": ANTHROPIC_VERSION},
    )
    yield from _stream_request(req, _parse_anthropic_sse_lines, cancel)


# --------------------------------------------------------------------------

def _http_error_message(e, req, detail: str) -> str:
    """Lead with a plain-language cause where the server's own wording is
    unhelpful, then the raw detail. Both of the substrings matched here are
    how Ollama reports "this model can't do tool calling" -- the second is a
    Jinja traceback from its tool-parser generation, which names nothing a
    user could act on."""
    if ("does not support tools" in detail
            or "generate parser for this template" in detail):
        return ("This model can't do tool calling, which the chat pane needs "
                "in order to read your scripts and propose edits. Choose a "
                "model that advertises the \"tools\" capability — for Ollama, "
                "`ollama show <model>` lists it.\n\n"
                f"Server said: HTTP {e.code}: {detail[:300]}")
    msg = f"HTTP {e.code}: {detail[:500]}"
    if e.code == 404:
        msg += (f"\n(Requested {req.full_url} — check the Base URL in "
                f"Preferences → AI.)")
    if e.code in (401, 403):
        msg += "\n(Check the API key in Preferences → AI.)"
    return msg


def _stream_request(req, parser, cancel: threading.Event | None) -> Iterator[StreamEvent]:
    """Shared transport: run `req`, feed its decoded lines through `parser`,
    and turn network/HTTP failures into an error StreamEvent rather than an
    exception (the worker thread has nowhere useful to raise to)."""
    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:
            for event in parser(_cancellable(_decoded_lines(resp), cancel)):
                yield event
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        yield StreamEvent(kind="error", error=_http_error_message(e, req, detail))
    except URLError as e:
        yield StreamEvent(kind="error", error=f"Connection failed: {e.reason}")
    except Exception as e:  # noqa: BLE001 -- surfaced in the chat pane
        yield StreamEvent(kind="error", error=str(e))


def _cancellable(lines: Iterable[str], cancel: threading.Event | None) -> Iterator[str]:
    for line in lines:
        if cancel is not None and cancel.is_set():
            return
        yield line


PROVIDERS = {"openai": stream_openai, "anthropic": stream_anthropic}
