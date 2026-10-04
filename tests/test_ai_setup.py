"""Preferences ▸ AI help (#678): the steps per service, plain-language
errors, how Claude will be reached, and the connection test -- no network:
urlopen is replaced."""
import io
import json
from urllib.error import HTTPError, URLError

import pytest

from belfryscad.window import ai_setup
from belfryscad.window.ai_providers import PRESETS, preset_for
from belfryscad.window.ai_setup import SETUP_STEPS, claude_route, friendly_error, check_connection


def _http(code, body=""):
    return HTTPError("https://x/v1/chat/completions", code, "err", {}, io.BytesIO(body.encode()))


def test_every_service_has_steps_and_the_paid_ones_say_billing_is_separate():
    assert set(SETUP_STEPS) == {p.id for p in PRESETS}
    for pid in ("anthropic", "openai"):
        assert "separately" in SETUP_STEPS[pid] and "<a href=" in SETUP_STEPS[pid]
    assert "Pro or Max subscription" in SETUP_STEPS["anthropic"]   # the CLI route


def test_errors_say_what_to_do():
    assert "refused the API key" in friendly_error(_http(401), "ChatGPT (OpenAI)")
    assert "model name" in friendly_error(_http(404))
    assert "credit" in friendly_error(_http(429))
    assert "can't use tools" in friendly_error(_http(400, '{"error":"model does not support tools"}'))
    assert "Couldn't reach" in friendly_error(URLError("Connection refused"), "Ollama (local)")


def test_claude_route_follows_the_chats_order():
    assert claude_route("env-key", "/bin/claude", "stored")[0] == "http-env"
    assert claude_route("", "/bin/claude", "stored")[0] == "cli"
    assert claude_route("", None, "stored")[0] == "http"
    route, sentence = claude_route("", None, "")
    assert route == "none" and "Not set up yet" in sentence


def test_missing_settings_are_named_before_anything_is_sent(monkeypatch):
    monkeypatch.setattr(ai_setup, "urlopen", lambda *a, **k: pytest.fail("nothing should be sent"))
    openai = preset_for("openai")
    assert "API key" in check_connection(openai, "", "gpt", "")[1]
    assert "Choose a model" in check_connection(openai, "", "", "key")[1]
    assert "No way to reach Claude" in check_connection(preset_for("anthropic"), "", "m", "")[1]
    assert check_connection(preset_for("copilot"), "", "", "", copilot_cli=None)[0] is False


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_working_service_passes_and_the_request_offers_a_tool(monkeypatch):
    sent = {}

    def fake(req, timeout=None):
        sent["url"], sent["body"] = req.full_url, json.loads(req.data)
        return _Response(b'{"choices": []}')
    monkeypatch.setattr(ai_setup, "urlopen", fake)
    ok, message = check_connection(preset_for("openai"), "", "gpt-x", "key")
    assert ok and "gpt-x" in message
    assert sent["url"].endswith("/chat/completions") and sent["body"]["tools"]


def test_a_refused_key_fails_with_the_plain_message(monkeypatch):
    def fake(req, timeout=None):
        raise _http(401)
    monkeypatch.setattr(ai_setup, "urlopen", fake)
    ok, message = check_connection(preset_for("anthropic"), "", "claude-x", "bad")
    assert not ok and "refused the API key" in message
