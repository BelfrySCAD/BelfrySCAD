#!/usr/bin/env python3
"""A configured claude CLI path must be honoured, and validated.

The point of the setting is the case where PATH has no `claude` at all, so
the interesting checks are the ones with PATH empty. A stored path that no
longer resolves must fall back rather than launch something broken.

Qt widgets crash pytest in this project, so this runs standalone.
"""
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PySide6.QtGui import QSurfaceFormat

fmt = QSurfaceFormat()
fmt.setVersion(3, 3)
fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
QSurfaceFormat.setDefaultFormat(fmt)

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from belfryscad.window.ai_cli import CLI_PATH_KEY, find_claude_cli  # noqa: E402

failures = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  -- {detail}" if not cond and detail else ""))
    if not cond:
        failures.append(label)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    s = QSettings("BelfrySCAD", "BelfrySCAD")
    saved_pref = s.value(CLI_PATH_KEY, "")
    saved_path = os.environ.get("PATH", "")

    with tempfile.TemporaryDirectory() as td:
        real = Path(td) / "claude"
        real.write_text("#!/bin/sh\nexit 0\n")
        real.chmod(real.stat().st_mode | stat.S_IXUSR)

        on_path_dir = Path(td) / "bin"
        on_path_dir.mkdir()
        on_path = on_path_dir / "claude"
        on_path.write_text("#!/bin/sh\nexit 0\n")
        on_path.chmod(on_path.stat().st_mode | stat.S_IXUSR)

        not_exec = Path(td) / "claude_not_exec"
        not_exec.write_text("#!/bin/sh\nexit 0\n")
        not_exec.chmod(0o644)

        missing = Path(td) / "gone" / "claude"

        try:
            # --- PATH has nothing: the setting is the only way in --------
            os.environ["PATH"] = ""
            s.setValue(CLI_PATH_KEY, "")
            s.sync()
            check("with no PATH and no setting, nothing is found",
                  find_claude_cli() is None, str(find_claude_cli()))

            s.setValue(CLI_PATH_KEY, str(real))
            s.sync()
            check("a configured path is used when PATH has nothing",
                  find_claude_cli() == str(real), str(find_claude_cli()))

            # --- a stale setting must not win over a working PATH --------
            os.environ["PATH"] = str(on_path_dir)
            s.setValue(CLI_PATH_KEY, str(missing))
            s.sync()
            check("a path that does not exist falls back to PATH",
                  find_claude_cli() == str(on_path), str(find_claude_cli()))

            s.setValue(CLI_PATH_KEY, str(not_exec))
            s.sync()
            check("a non-executable path falls back to PATH",
                  find_claude_cli() == str(on_path), str(find_claude_cli()))

            # --- a valid setting outranks PATH ---------------------------
            s.setValue(CLI_PATH_KEY, str(real))
            s.sync()
            check("a valid configured path wins over PATH",
                  find_claude_cli() == str(real), str(find_claude_cli()))

            s.setValue(CLI_PATH_KEY, "   ")
            s.sync()
            check("a blank setting is ignored, not treated as a path",
                  find_claude_cli() == str(on_path), str(find_claude_cli()))

            # --- the CLI transport becomes reachable because of it -------
            os.environ["PATH"] = ""
            os.environ.pop("ANTHROPIC_API_KEY", None)
            s.setValue(CLI_PATH_KEY, str(real))
            s.sync()
            from belfryscad.window.ai_chat import resolve_anthropic_transport
            transport, key = resolve_anthropic_transport()
            check("a configured path makes the CLI transport available",
                  transport == "cli" and key == "", f"{transport!r}")

            # --- Preferences reports which one will be used --------------
            from belfryscad.window.preferences import PreferencesDialog
            dlg = PreferencesDialog()
            check("Preferences shows the configured path in its status line",
                  str(real) in dlg._ai_cli_status.text(), dlg._ai_cli_status.text())

            dlg._ai_cli_path.setText(str(missing))
            dlg._update_ai_cli_status()
            check("Preferences warns when the configured path is missing",
                  "⚠" in dlg._ai_cli_status.text(), dlg._ai_cli_status.text())

            dlg._ai_cli_path.setText(str(not_exec))
            dlg._update_ai_cli_status()
            check("Preferences warns when the file is not executable",
                  "not executable" in dlg._ai_cli_status.text(), dlg._ai_cli_status.text())

            dlg._ai_cli_path.setText("")
            dlg._update_ai_cli_status()
            check("Preferences says so when there is no CLI anywhere",
                  "No claude CLI on PATH" in dlg._ai_cli_status.text(),
                  dlg._ai_cli_status.text())

            # --- the row only appears for Claude -------------------------
            def show_preset(pid):
                i = dlg._ai_preset.findData(pid)
                dlg._ai_preset.setCurrentIndex(i)
                app.processEvents()
                # isHidden, not isVisibleTo(dlg): the AI page is not the
                # current tab in a headless run, so QTabWidget has hidden the
                # whole page and isVisibleTo would report False for every
                # preset -- unable to tell the two cases apart. isHidden
                # reflects the hide() setRowVisible actually performs.
                return not dlg._ai_cli_row.isHidden(), not dlg._ai_cli_status.isHidden()

            row_v, status_v = show_preset("anthropic")
            check("the Claude CLI row shows for Claude", row_v and status_v,
                  f"row={row_v} status={status_v}")
            for other in ("openai", "ollama", "google"):
                row_v, status_v = show_preset(other)
                check(f"the Claude CLI row is hidden for {other}",
                      not row_v and not status_v, f"row={row_v} status={status_v}")
            show_preset("anthropic")

            # --- the API key field follows accepts_key -------------------
            from belfryscad.window.ai_providers import PRESETS_BY_ID
            for pid in ("anthropic", "openai", "ollama", "google", "custom"):
                i = dlg._ai_preset.findData(pid)
                dlg._ai_preset.setCurrentIndex(i)
                app.processEvents()
                want = PRESETS_BY_ID[pid].accepts_key
                shown = not dlg._ai_key.isHidden()
                check(f"API key field {'shows' if want else 'is hidden'} for {pid}",
                      shown == want, f"shown={shown} accepts_key={want}")
            check("Ollama is the preset with no key field",
                  not PRESETS_BY_ID["ollama"].accepts_key)
            check("Claude keeps its key field even though it needs no key",
                  PRESETS_BY_ID["anthropic"].accepts_key
                  and not PRESETS_BY_ID["anthropic"].needs_key)
            dlg._ai_preset.setCurrentIndex(dlg._ai_preset.findData("anthropic"))
            app.processEvents()

            # Editing the field must persist, or Choose… would be the only
            # way to set it.
            dlg._ai_cli_path.setText(str(real))
            dlg._save_ai_cli_path()
            check("typing a path into the field saves it",
                  QSettings("BelfrySCAD", "BelfrySCAD").value(CLI_PATH_KEY, "") == str(real))
            dlg.close()
        finally:
            os.environ["PATH"] = saved_path
            s.setValue(CLI_PATH_KEY, saved_pref)
            s.sync()

    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
