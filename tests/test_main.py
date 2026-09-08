"""Tests for belfryscad.main's -p/-P (Customizer parameter set) and
-d/-m (Makefile deps/missing-file) CLI resolution -- the rest of main.py's
argparse/dispatch isn't otherwise covered at this layer (see
tests/test_headless.py and tests/test_headless_render.py for the export
functions themselves, tests/test_scad_deps.py for scan_dependencies/
write_deps_file/run_make_for_missing in isolation)."""

import json
import os
import sys
import struct

import pytest

from belfryscad.main import main


def _stl_vertex_xs(path) -> list[float]:
    data = open(path, "rb").read()
    n = struct.unpack("<I", data[80:84])[0]
    xs = []
    off = 84
    for _ in range(n):
        off += 12
        for _ in range(3):
            x, _y, _z = struct.unpack("<fff", data[off:off + 12])
            xs.append(round(x, 3))
            off += 12
        off += 2
    return xs


class TestParamSet:
    def _write_model(self, tmp_path):
        src = tmp_path / "model.scad"
        src.write_text("width = 1;\ncube([width, 1, 1]);\n")
        return src

    def _write_presets(self, tmp_path, sets):
        path = tmp_path / "model.json"
        path.write_text(json.dumps({"fileFormatVersion": "1", "parameterSets": sets}))
        return path

    def test_applies_preset(self, tmp_path, monkeypatch):
        src = self._write_model(tmp_path)
        preset_file = self._write_presets(tmp_path, {"Big": {"width": "10"}})
        out = tmp_path / "out.stl"
        monkeypatch.setattr(
            "sys.argv",
            ["belfryscad", "-o", str(out), "-p", str(preset_file), "-P", "Big", str(src)],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        assert max(_stl_vertex_xs(out)) == 10.0

    def test_define_overrides_preset(self, tmp_path, monkeypatch):
        src = self._write_model(tmp_path)
        preset_file = self._write_presets(tmp_path, {"Big": {"width": "10"}})
        out = tmp_path / "out.stl"
        monkeypatch.setattr(
            "sys.argv",
            ["belfryscad", "-o", str(out), "-p", str(preset_file), "-P", "Big",
             "-D", "width=99", str(src)],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        assert max(_stl_vertex_xs(out)) == 99.0

    def test_p_without_capital_p_fails(self, tmp_path, monkeypatch, capsys):
        src = self._write_model(tmp_path)
        preset_file = self._write_presets(tmp_path, {"Big": {"width": "10"}})
        monkeypatch.setattr(
            "sys.argv",
            ["belfryscad", "-o", str(tmp_path / "out.stl"), "-p", str(preset_file), str(src)],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "-p and -P must be used together" in capsys.readouterr().err

    def test_capital_p_without_p_fails(self, tmp_path, monkeypatch, capsys):
        src = self._write_model(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            ["belfryscad", "-o", str(tmp_path / "out.stl"), "-P", "Big", str(src)],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        assert "-p and -P must be used together" in capsys.readouterr().err

    def test_unknown_preset_name_fails(self, tmp_path, monkeypatch, capsys):
        src = self._write_model(tmp_path)
        preset_file = self._write_presets(tmp_path, {"Big": {"width": "10"}})
        monkeypatch.setattr(
            "sys.argv",
            ["belfryscad", "-o", str(tmp_path / "out.stl"), "-p", str(preset_file),
             "-P", "Nonexistent", str(src)],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Nonexistent" in err and "Big" in err


class TestDepsAndMake:
    def test_deps_file_written(self, tmp_path, monkeypatch):
        (tmp_path / "lib.scad").write_text("module m() { cube(1); }\n")
        src = tmp_path / "main.scad"
        src.write_text("use <lib.scad>\nm();\n")
        out = tmp_path / "out.stl"
        deps = tmp_path / "out.deps"
        monkeypatch.setattr(
            "sys.argv",
            ["belfryscad", "-o", str(out), "-d", str(deps), str(src)],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        assert deps.read_text() == f"{out}: \\\n\t{src} \\\n\t{tmp_path / 'lib.scad'}\n"

    def test_make_cmd_generates_missing_input(self, tmp_path, monkeypatch):
        src = tmp_path / "generated.scad"
        # Python rather than /bin/sh: the shell form cannot run on Windows.
        gen = tmp_path / "gen.py"
        gen.write_text('import sys\n'
                       'open(sys.argv[1], "w").write("cube(1);\\n")\n')
        out = tmp_path / "out.stl"
        monkeypatch.setattr(
            "sys.argv",
            ["belfryscad", "-o", str(out), "-m",
             f'"{sys.executable}" "{gen}"', str(src)],
        )
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        assert src.exists()
        assert out.exists()


class TestEnsureStreams:
    """A windowed launch has sys.stdout/stderr = None (issue #364).

    print() tolerates that; a bare .flush() does not, and openscad_docsgen's
    vendored modules flush after every message -- errorlog.add_entry does it
    for every documentation error, so the Docs pane died with
    "'NoneType' object has no attribute 'flush'" precisely when it had
    something to report, losing the report with it.
    """

    def test_replaces_missing_streams(self, monkeypatch):
        from belfryscad.main import _ensure_streams

        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        _ensure_streams()
        assert sys.stdout is not None
        assert sys.stderr is not None
        sys.stdout.write("")      # usable, not just non-None
        sys.stdout.flush()
        sys.stderr.flush()

    def test_leaves_real_streams_alone(self, monkeypatch):
        """Every CLI mode depends on writing to the stdout it was given."""
        import io
        from belfryscad.main import _ensure_streams

        mine = io.StringIO()
        monkeypatch.setattr(sys, "stdout", mine)
        _ensure_streams()
        assert sys.stdout is mine

    def test_a_documentation_error_survives_missing_streams(self, monkeypatch):
        """The actual crash site: errorlog.add_entry flushes stderr."""
        from belfryscad.main import _ensure_streams
        from belfryscad.docsgen.errorlog import ErrorLog

        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)
        _ensure_streams()

        log = ErrorLog()
        log.add_entry("widget.scad", 8, 'Unrecognized block: "Bogus"', ErrorLog.FAIL)
        assert log.errlist == [("widget.scad", 8, 'Unrecognized block: "Bogus"', ErrorLog.FAIL)]
        assert log.has_errors
