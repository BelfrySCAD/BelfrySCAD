"""Talk to GitHub Copilot through its `copilot` CLI.

Same idea as ai_cli.py's Claude bridge -- the CLI carries its own GitHub
credentials, so this needs no API key -- but the process model differs.

`claude -p` is a coprocess: one long-lived process fed a message per turn
over stdin, holding the conversation itself. `copilot -p` exits when the
turn completes, so this runs one process per turn and carries the
conversation forward with --session-id. The id comes from the CLI's own
`result` event on the first turn; passing our own up front would work too,
but taking the CLI's keeps the session file it writes and the id we resume
by from ever disagreeing.

Tools are restricted with --available-tools rather than by denying the
dangerous ones by name: it is an allowlist, so Copilot's own file and shell
tools are not merely forbidden but absent from what the model is offered.
The Claude side has to deny by name because its equivalent flag has no
"only these" form.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from typing import Iterator

from belfryscad.window.ai_providers import StreamEvent
from belfryscad.window.ai_tools import TOOLS

CLI_PATH_KEY = "ai/copilotCliPath"

_TOOL_NAMES = [f"belfryscad-{t['name']}" for t in TOOLS]


def _is_github_copilot(path: str) -> bool:
    """Tell GitHub's `copilot` from AWS's, which shares the name.

    AWS Copilot (ECS/App Runner deployment) installs a `copilot` binary too,
    and on a machine with both, PATH order decides which one is found --
    `gh copilot` itself gets this wrong. Running the wrong one would fail
    with a baffling ECS error, so the binary is asked what it is.

    --version is used rather than --help: it is cheap, it does not need
    auth, and neither tool needs a terminal for it.
    """
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "github copilot" in out.lower()


def find_copilot_cli() -> str | None:
    """GitHub's copilot CLI, or None.

    A path set in Preferences wins over PATH -- with the AWS collision this
    is not a corner case, it is the ordinary way to resolve it. Either
    candidate is verified to actually be GitHub's before being returned.
    """
    from PySide6.QtCore import QSettings
    configured = (QSettings("BelfrySCAD", "BelfrySCAD")
                  .value(CLI_PATH_KEY, "") or "").strip()
    if (configured and os.path.isfile(configured) and os.access(configured, os.X_OK)
            and _is_github_copilot(configured)):
        return configured
    found = shutil.which("copilot")
    if found and _is_github_copilot(found):
        return found
    return None


class CopilotCliSession:
    """One `copilot -p` invocation per turn, chained by session id.

    Not thread-safe: only the AI worker thread drives it, one turn at a
    time -- same contract as ClaudeCliSession.
    """

    def __init__(self, system_prompt: str, mcp_url: str, model: str = "",
                 cwd: str | None = None):
        self._system_prompt = system_prompt
        self._mcp_url = mcp_url
        self._model = model
        self._cwd = cwd
        self._session_id: str | None = None
        self._proc: subprocess.Popen | None = None

    def _command(self, text: str) -> list[str]:
        # Inline JSON, not a file: --additional-mcp-config augments
        # ~/.copilot/mcp-config.json for this session only, so the user's own
        # MCP setup is neither read into ours nor written over.
        mcp_cfg = json.dumps(
            {"mcpServers": {"belfryscad": {"type": "http", "url": self._mcp_url}}})
        cmd = [
            find_copilot_cli() or "copilot",
            "-p", f"{self._system_prompt}\n\n{text}" if not self._session_id else text,
            "--output-format", "json",
            "--additional-mcp-config", mcp_cfg,
            "--available-tools", *_TOOL_NAMES,
            # Required for non-interactive mode. Scoped by --available-tools
            # above, so "all" means all of ours, not all of Copilot's.
            "--allow-all-tools",
            "--no-color",
        ]
        if self._session_id:
            cmd += ["--session-id", self._session_id]
        if self._model:
            cmd += ["--model", self._model]
        return cmd

    def stop(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        # Best-effort, same as the Claude side: this runs on shutdown and on
        # cancel, where a half-dead process must not raise into the caller.
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:      # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def send_turn(self, text: str,
                  cancel: threading.Event | None = None) -> Iterator[StreamEvent]:
        """Run one turn and yield events until it ends."""
        try:
            self._proc = subprocess.Popen(
                self._command(text), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, cwd=self._cwd,
            )
        except OSError as e:
            yield StreamEvent(kind="error", error=f"copilot CLI failed to start: {e}")
            return

        proc = self._proc
        assert proc.stdout is not None
        saw_result = False
        for line in proc.stdout:
            if cancel is not None and cancel.is_set():
                self.stop()
                return
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue  # progress chatter is not always JSON
            for out in self._translate(ev):
                if out.kind in ("done", "error"):
                    saw_result = True
                yield out
                if saw_result:
                    self._proc = None
                    return

        err = ""
        if proc.stderr is not None:
            err = proc.stderr.read()[:500]
        self.stop()
        yield StreamEvent(kind="error",
                          error=f"copilot CLI exited unexpectedly. {err}".strip())

    def _translate(self, ev: dict) -> Iterator[StreamEvent]:
        """One JSONL event -> zero or more StreamEvents."""
        kind = ev.get("type")
        data = ev.get("data") or {}

        if kind == "assistant.message_delta":
            if data.get("deltaContent"):
                yield StreamEvent(kind="text_delta", text=data["deltaContent"])

        elif kind == "assistant.message":
            # Report-only, as on the Claude side: the CLI runs the tool
            # itself against our MCP server, so this drives the activity
            # line and never re-enters our own tool loop.
            for req in data.get("toolRequests") or []:
                name = req.get("name") or (req.get("function") or {}).get("name") or ""
                yield StreamEvent(kind="tool_running", text=_short_tool_name(name))

        elif kind == "result":
            # Carry the session forward. Taken from the CLI rather than
            # generated here so the id we resume by is the one it stored.
            if ev.get("sessionId"):
                self._session_id = ev["sessionId"]
            code = ev.get("exitCode", 0)
            if code:
                yield StreamEvent(kind="error",
                                  error=f"copilot CLI exited with status {code}")
            else:
                yield StreamEvent(kind="done")


def _short_tool_name(name: str) -> str:
    """"belfryscad-read_open_script" -> "read_open_script"."""
    if not name:
        return ""
    return name.split("belfryscad-", 1)[-1] if "belfryscad-" in name else name
