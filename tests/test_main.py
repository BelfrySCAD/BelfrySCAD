"""Tests for belfryscad.main's -p/-P (Customizer parameter set) CLI
resolution -- the rest of main.py's argparse/dispatch isn't otherwise
covered at this layer (see tests/test_headless.py and
tests/test_headless_render.py for the export functions themselves)."""

import json
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
