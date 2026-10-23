"""Every bundled SVG is well-formed XML. Qt refuses a malformed one and the
button is left without an icon, silently: `--` inside a comment did that to
the Preview toolbar icon."""
from pathlib import Path
from xml.dom.minidom import parse

import pytest

RESOURCES = Path(__file__).resolve().parent.parent / "src" / "belfryscad" / "resources"


@pytest.mark.parametrize("svg", sorted(RESOURCES.rglob("*.svg")), ids=lambda p: p.name)
def test_svg_parses(svg):
    parse(str(svg))
