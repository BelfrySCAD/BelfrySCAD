"""Talk to Claude through the `claude` CLI as a persistent coprocess,
instead of the HTTP API.

Why: the CLI carries its own authentication (a Claude subscription via
OAuth), so this path needs no API key at all. It's tried after
ANTHROPIC_API_KEY and before the manually-entered key -- see
ai_chat.resolve_anthropic_transport.

One long-lived process per conversation, fed user messages as JSON lines
on stdin (`--input-format stream-json`) and read back as JSON lines on
stdout. Conversation state therefore lives in the CLI: we send only the
new message each turn, never the accumulated history.

Tools come from our own in-process MCP server (ai_mcp), wired up with
--strict-mcp-config so the CLI's own Read/Write/Edit can't be used
instead -- those would miss unsaved editor buffers and would write
straight to disk, bypassing the review dialog. --system-prompt *replaces*
the CLI's default prompt, which also keeps any personal CLAUDE.md
persona out of the CAD assistant.

The inner `stream_event.event` payloads are ordinary Anthropic streaming
events, so text deltas are read exactly as ai_providers does.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from typing import Iterator

from belfryscad.window.ai_providers import StreamEvent
from belfryscad.window.ai_tools import TOOLS

_TOOL_NAMES = [f"mcp__belfryscad__{t['name']}" for t in TOOLS]
# The CLI's own file tools are denied: they can't see unsaved buffers, and
# Write/Edit would apply changes without the user's review.
_DENIED = ["Write", "Edit", "NotebookEdit", "Bash", "Read", "Glob", "Grep",
           "WebFetch", "WebSearch", "Task"]


def find_claude_cli() -> str | None:
    return shutil.which("claude")


class ClaudeCliSession:
    """A persistent `claude -p` coprocess. Not thread-safe: only the AI
    worker thread drives it, one turn at a time."""

    def __init__(self, system_prompt: str, mcp_url: str, model: str = "",
                 cwd: str | None = None):
        self._system_prompt = system_prompt
        self._mcp_url = mcp_url
        self._model = model
        self._cwd = cwd
        self._proc: subprocess.Popen | None = None

    def _command(self) -> list[str]:
        mcp_cfg = json.dumps(
            {"mcpServers": {"belfryscad": {"type": "http", "url": self._mcp_url}}})
        cmd = [
            find_claude_cli() or "claude", "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose", "--include-partial-messages", "--replay-user-messages",
            "--system-prompt", self._system_prompt,
            "--mcp-config", mcp_cfg,
            "--strict-mcp-config",
            "--allowedTools", *_TOOL_NAMES,
            "--disallowedTools", *_DENIED,
        ]
        if self._model:
            cmd += ["--model", self._model]
        return cmd

    def _ensure_started(self):
        if self._proc is not None and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, cwd=self._cwd,
        )

    def stop(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        # Teardown is entirely best-effort: this runs on app shutdown and on
        # cancel, where a half-dead process must never raise into the caller.
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:      # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def send_turn(self, text: str,
                  cancel: threading.Event | None = None) -> Iterator[StreamEvent]:
        """Send one user message and yield events until the turn ends."""
        try:
            self._ensure_started()
            proc = self._proc
            assert proc is not None and proc.stdin is not None
            msg = {"type": "user", "message": {
                "role": "user", "content": [{"type": "text", "text": text}]}}
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        except (OSError, BrokenPipeError) as e:
            self.stop()
            yield StreamEvent(kind="error", error=f"claude CLI failed to start: {e}")
            return

        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                # The CLI has no mid-turn cancel over stdin; drop the whole
                # process so a cancelled turn can't keep streaming into the
                # next one. The next turn restarts it (losing CLI-side
                # history, which is the honest trade for a hard stop).
                self.stop()
                return
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "stream_event":
                inner = ev.get("event") or {}
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield StreamEvent(kind="text_delta", text=delta["text"])
                elif inner.get("type") == "content_block_start":
                    block = inner.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        # Report-only: the CLI runs the tool itself (against
                        # our MCP server), so this never re-enters our own
                        # tool loop -- it just drives the activity line.
                        yield StreamEvent(
                            kind="tool_running",
                            text=_short_tool_name(block.get("name", "")))
            elif kind == "result":
                if ev.get("is_error"):
                    yield StreamEvent(
                        kind="error",
                        error=str(ev.get("result") or "claude CLI reported an error"))
                else:
                    yield StreamEvent(kind="done")
                return

        # stdout closed without a result: the process died.
        err = ""
        if proc.stderr is not None:
            err = proc.stderr.read()[:500]
        self.stop()
        yield StreamEvent(kind="error",
                          error=f"claude CLI exited unexpectedly. {err}".strip())


def _short_tool_name(name: str) -> str:
    """"mcp__belfryscad__read_open_script" -> "read_open_script"."""
    return name.rsplit("__", 1)[-1] if name else ""
