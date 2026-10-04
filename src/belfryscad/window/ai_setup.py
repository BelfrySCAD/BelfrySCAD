"""Helping someone set up an AI service, for Preferences ▸ AI (#678).

The page used to say almost nothing: no word on where a key comes from,
that an API key is billed apart from a chat subscription, or whether the
settings work at all until the first chat failed. This module holds the
parts with no Qt in them -- the per-service steps, model listing for
Claude, plain-language errors, and a connection test that goes the way the
chat pane itself goes -- so they can be tested without a dialog.
"""
from __future__ import annotations

import json
import socket
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from belfryscad.window.ai_providers import ANTHROPIC_VERSION, Preset, _normalize_openai_base

_TIMEOUT = 30

#: The step-by-step guide, reachable from Preferences ▸ AI, Help, and the
#: chat pane's not-set-up message.
WIKI_URL = "https://github.com/BelfrySCAD/BelfrySCAD/wiki/AI-Chat"


def open_guide():
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    QDesktopServices.openUrl(QUrl(WIKI_URL))

#: What to do, per service, in the order a newcomer needs it. Rich text: the
#: Preferences label opens the links in a browser.
SETUP_STEPS = {
    "anthropic": (
        "<b>Two ways to connect — pick one:</b><ol>"
        "<li><b>With a Claude Pro or Max subscription:</b> install the "
        "<a href='https://docs.anthropic.com/en/docs/claude-code/setup'>Claude Code CLI</a>, "
        "run <code>claude</code> once in a terminal and sign in. No key needed here; "
        "leave Model empty to use the CLI's default.</li>"
        "<li><b>With an API key:</b> create one at "
        "<a href='https://console.anthropic.com/settings/keys'>console.anthropic.com</a> "
        "and paste it below. API use is billed pay-as-you-go, <i>separately</i> from any "
        "claude.ai subscription. Then press <b>Fetch</b> and pick a model.</li></ol>"
        "Press <b>Test</b> to check it works."),
    "openai": (
        "<ol><li>Create an API key at "
        "<a href='https://platform.openai.com/api-keys'>platform.openai.com</a> "
        "(billed pay-as-you-go, <i>separately</i> from a ChatGPT subscription).</li>"
        "<li>Paste it below, press <b>Fetch</b> and pick a model.</li>"
        "<li>Press <b>Test</b>.</li></ol>"),
    "google": (
        "<ol><li>Create a free API key at "
        "<a href='https://aistudio.google.com/apikey'>aistudio.google.com</a>.</li>"
        "<li>Paste it below, press <b>Fetch</b> and pick a model.</li>"
        "<li>Press <b>Test</b>.</li></ol>"),
    "moonshot": (
        "<ol><li>Create an API key at "
        "<a href='https://platform.moonshot.ai/console/api-keys'>platform.moonshot.ai</a>.</li>"
        "<li>Paste it below, press <b>Fetch</b> and pick a model.</li>"
        "<li>Press <b>Test</b>.</li></ol>"),
    "ollama": (
        "<ol><li>Install <a href='https://ollama.com/download'>Ollama</a> and start it.</li>"
        "<li>Get a model that can use tools, e.g. <code>ollama pull qwen3</code>.</li>"
        "<li>Press <b>Fetch</b>, pick the model, then <b>Test</b>. Free, and nothing "
        "leaves your computer, but a local model is much slower.</li></ol>"),
    "copilot": (
        "<ol><li>Install GitHub's <code>copilot</code> CLI: "
        "<code>npm install -g @github/copilot</code> (or <code>brew install copilot-cli</code>).</li>"
        "<li>Run <code>copilot login</code> once in a terminal. It uses your GitHub "
        "Copilot subscription; there is no key to enter.</li>"
        "<li>Press <b>Test</b>.</li></ol>"),
    "custom": (
        "For any other server that speaks the OpenAI protocol: set its Base URL (usually "
        "ending in <code>/v1</code>) and key, press <b>Fetch</b>, pick a model, then "
        "<b>Test</b>."),
}

#: A tool for the test request to offer. The chat pane always sends tools,
#: and a model without tool support fails the whole request -- so testing
#: without one would pass a model the chat then cannot use.
_PING_TOOL = {"type": "function", "function": {
    "name": "ping", "description": "Does nothing.",
    "parameters": {"type": "object", "properties": {}}}}


def friendly_error(e: Exception, service: str = "this service") -> str:
    """What went wrong, in words a newcomer can act on."""
    if isinstance(e, HTTPError):
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:      # noqa: BLE001
            detail = ""
        if e.code in (401, 403):
            return (f"{service} refused the API key ({e.code}). Check it's entered in full, "
                    "and that the account behind it has API access and credit.")
        if e.code == 404:
            return ("Not found (404): check the model name (Fetch lists the ones available) "
                    "and the Base URL.")
        if e.code == 429:
            return (f"{service} says too many requests or no credit left (429). Check the "
                    "account's billing, or wait a minute and try again.")
        if "does not support tools" in detail or "tool" in detail.lower() and e.code == 400:
            return ("This model can't use tools, which the chat needs to read and edit your "
                    "scripts. Choose another model.")
        return f"{service} answered HTTP {e.code}: {detail[:300]}"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return f"No answer from {service} within {_TIMEOUT} seconds."
    if isinstance(e, URLError):
        return f"Couldn't reach {service}: {e.reason}. Check the Base URL, and that it's running."
    return str(e)


def list_anthropic_models(base_url: str, api_key: str) -> list[str]:
    """Model ids from Anthropic's /models endpoint (needs an API key)."""
    url = (base_url or "https://api.anthropic.com/v1").rstrip("/") + "/models?limit=100"
    req = Request(url, headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION})
    with urlopen(req, timeout=_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return [m["id"] for m in data.get("data", []) if m.get("id")]


def claude_route(env_key: str, cli: str | None, stored_key: str) -> tuple[str, str]:
    """(route, sentence) for how the chat will reach Claude -- the order
    ai_chat.resolve_anthropic_transport uses."""
    if env_key:
        return "http-env", "Will connect with the ANTHROPIC_API_KEY set in your environment."
    if cli:
        return "cli", f"Will connect through the Claude CLI ({cli}), using its sign-in."
    if stored_key:
        return "http", "Will connect with the API key below."
    return "none", "Not set up yet: sign in to the Claude CLI, or enter an API key below."


def check_connection(preset: Preset, base_url: str, model: str, api_key: str,
                    claude_cli: str | None = None, copilot_cli: str | None = None) -> tuple[bool, str]:
    """Send the smallest real request the chat would, and say how it went.

    Claude goes the chat's own way: the environment or entered key over
    HTTP, else the CLI. Copilot is CLI-only and checked for presence."""
    name = preset.label
    try:
        if preset.id == "copilot":
            if not copilot_cli:
                return False, "No copilot CLI found. Install it (see the steps above)."
            return True, (f"Found the copilot CLI ({copilot_cli}). If you haven't yet, run "
                          "`copilot login` in a terminal once.")
        if preset.protocol == "anthropic":
            if not api_key and claude_cli:
                cmd = [claude_cli, "-p", "Reply with just: OK"] + (["--model", model] if model else [])
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if r.returncode != 0:
                    why = (r.stderr or r.stdout).strip()[-300:]
                    return False, ("The Claude CLI failed. Run `claude` in a terminal to check it's "
                                   f"signed in.\n{why}")
                return True, f"✓ Connected through the Claude CLI. It replied: {r.stdout.strip()[:60]}"
            if not api_key:
                return False, "No way to reach Claude yet: sign in to the Claude CLI, or enter an API key."
            if not model:
                return False, "Choose a model first: press Fetch, or type one."
            body = {"model": model, "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Reply with just: OK"}],
                    "tools": [{"name": "ping", "description": "Does nothing.",
                               "input_schema": {"type": "object", "properties": {}}}]}
            req = Request((base_url or preset.base_url).rstrip("/") + "/messages",
                          data=json.dumps(body).encode("utf-8"), method="POST",
                          headers={"content-type": "application/json", "x-api-key": api_key,
                                   "anthropic-version": ANTHROPIC_VERSION})
        else:
            if not (base_url or preset.base_url):
                return False, "Set the Base URL first."
            if preset.needs_key and not api_key:
                return False, "Enter the API key first (the steps above say where to get one)."
            if not model:
                return False, "Choose a model first: press Fetch, or type one."
            body = {"model": model, "max_tokens": 16, "tools": [_PING_TOOL],
                    "messages": [{"role": "user", "content": "Reply with just: OK"}]}
            headers = {"content-type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            req = Request(_normalize_openai_base(base_url or preset.base_url) + "/chat/completions",
                          data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
        with urlopen(req, timeout=_TIMEOUT) as resp:
            json.loads(resp.read().decode("utf-8"))
        return True, f"✓ Connected: {model} answered, and can use tools."
    except subprocess.TimeoutExpired:
        return False, "The Claude CLI didn't answer within two minutes."
    except Exception as e:     # noqa: BLE001 -- reported in the dialog
        return False, friendly_error(e, name)
