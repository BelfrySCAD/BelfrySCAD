"""Edit > Insert Template and Manage Templates.

The file format, loading and expansion are Qt-free and tested directly.
The editor and the manager window are widgets, so they are driven in a
subprocess, as the other widget tests here are.
"""
import json
import subprocess
import sys

from belfryscad import scad_templates
from belfryscad.scad_templates import BUILTIN_DIR, MARKER, Template, expand, load, read, save


def _write(folder, name, data):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


def test_every_builtin_loads_with_at_most_one_marker():
    templates, builtins, errors = load(BUILTIN_DIR, BUILTIN_DIR / "no-such-dir")
    assert not errors
    # OpenSCAD's own seven keys, so an OpenSCAD user's override of one
    # replaces ours too.
    assert set(builtins) == {"CC0", "difference", "for", "function", "module", "rotate", "translate"}
    assert all(t.builtin and t.content.count(MARKER) <= 1 for t in templates.values())


def test_a_user_template_replaces_the_builtin_of_the_same_key(tmp_path):
    b, u = tmp_path / "b", tmp_path / "u"
    _write(b, "for.json", {"key": "for", "content": "builtin"})
    _write(u, "whatever.json", {"key": "for", "content": "mine"})
    templates, builtins, _ = load(b, u)
    assert templates["for"].content == "mine" and not templates["for"].builtin
    assert builtins["for"].content == "builtin"


def test_a_bad_file_is_reported_and_skipped_not_fatal(tmp_path):
    _write(tmp_path / "u", "broken.json", "{not json")
    _write(tmp_path / "u", "nokey.json", {"content": "x"})
    _write(tmp_path / "u", "ok.json", {"key": "ok", "content": "x"})
    templates, _, errors = load(tmp_path / "b", tmp_path / "u")
    assert list(templates) == ["ok"]
    assert len(errors) == 2 and all("Error reading template file" in e for e in errors)


def test_openscads_offset_form_becomes_a_marker(tmp_path):
    _write(tmp_path, "t.json", {"key": "t", "content": "abcdef", "offset": 2})
    assert read(tmp_path / "t.json").content == "ab" + MARKER + "cdef"


def test_expand_places_the_cursor_and_indents():
    text, at = expand("for (i = [^~^ : ]) {\n\t\n}", "  ", "    ")
    assert text == "for (i = [ : ]) {\n      \n    }"
    assert at == len("for (i = [")
    assert expand("rotate([])", "    ") == ("rotate([])", len("rotate([])"))   # no marker: the end


def test_save_writes_openscads_format_and_never_a_builtins_file(tmp_path):
    b = tmp_path / "b"
    _write(b, "for.json", {"key": "for", "content": "builtin"})
    t = read(b / "for.json", builtin=True)
    t.content = "mine"
    path = save(t, tmp_path / "u")
    assert path.parent == tmp_path / "u" and not t.builtin
    assert json.loads((b / "for.json").read_text())["content"] == "builtin"
    assert json.loads(path.read_text()) == {"key": "for", "content": "mine"}
    # Saved again, it goes back to the same file rather than a new one.
    t.content = "again"
    assert save(t, tmp_path / "u") == path


def test_user_dir_is_openscads(monkeypatch, tmp_path):
    monkeypatch.setattr(scad_templates.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert scad_templates.user_dir() == tmp_path / "OpenSCAD" / "templates"


def test_insert_and_manage_in_the_real_widgets(tmp_path):
    driver = tmp_path / "_templates.py"
    driver.write_text(f'''
import json, tempfile
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication([])
from belfryscad.settings import use_scratch_settings
use_scratch_settings(tempfile.mkdtemp(prefix="belfryscad-templates-"), seed=False)
from belfryscad import scad_templates
user = Path({str(tmp_path / "user")!r})
scad_templates.user_dir = lambda: user
out = {{}}

from belfryscad.window.main_window import MainWindow
w = MainWindow(); w.skip_unsaved_prompts = True
edit_menu = next(m for m in w.menuBar().findChildren(type(w.menuBar().addMenu("x")))
                 if m.title().replace("&", "") == "Edit")
out["menu"] = [a.text() for a in edit_menu.actions() if "Template" in a.text()]

# Insertion: indented to the line, tabs as the editor's indent, caret at ^~^.
ed = w._current_editor()
ed.setPlainText("  x\\n")
c = ed.textCursor(); c.setPosition(3); ed.setTextCursor(c)
ed.insert_template("for (i = [^~^ : ]) {{\\n\\t\\n}}")
out["text"] = ed.toPlainText()
out["caret"] = ed.textCursor().position()
w._current_tab().undo_stack.undo()
out["one_undo"] = ed.toPlainText()

# The manager: edit a built-in (makes a user copy), then revert it.
m = w._manage_templates()
keys = [m.list.item(i).data(0x0100) for i in range(m.list.count())]
m.list.setCurrentRow(keys.index("for"))
m.content.setPlainText("for (^~^) mine")
out["saved"] = m.save()
out["user_files"] = sorted(p.name for p in user.glob("*.json"))
out["loaded"] = scad_templates.load()[0]["for"].content
out["delete_label"] = m.delete_btn.text()
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
m._delete()
out["after_revert"] = scad_templates.load()[0]["for"].builtin

# A new one, named, saved, and then listed by Insert Template.
m._new()
m.name.setText("hello"); m.content.setPlainText("echo(^~^);")
out["new_saved"] = m.save()
out["new_listed"] = "hello" in scad_templates.load()[0]
m.close()
w.close()
print(json.dumps(out))
''', encoding="utf-8")
    res = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True,
                         env={"QT_QPA_PLATFORM": "offscreen", "PATH": "/usr/bin:/bin",
                              "HOME": str(tmp_path),
                              # Path.home() on Windows reads USERPROFILE, not HOME
                              "USERPROFILE": str(tmp_path)})
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["menu"] == ["Insert Template…", "Manage Templates…"]
    assert out["text"] == "  xfor (i = [ : ]) {\n      \n  }\n"
    assert out["caret"] == len("  xfor (i = [")
    assert out["one_undo"] == "  x\n"
    assert out["saved"] and out["user_files"] == ["for.json"]
    assert out["loaded"] == "for (^~^) mine"
    assert out["delete_label"] == "Revert to Built-in"
    assert out["after_revert"] is True
    assert out["new_saved"] and out["new_listed"]
