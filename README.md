# BelfrySCAD

A hybrid procedural CAD application combining OpenSCAD-style script-based modeling with live WYSIWYG 3D interaction. Edit code or drag geometry — both views stay in sync.

## Features

- **Full OpenSCAD language** — variables, functions, modules, loops, conditionals, all built-in primitives and transforms
- **3D viewport** — GPU-accelerated rendering with camera controls (orbit, pan, zoom)
- **Bidirectional editing** — drag geometry in the viewport and the source code updates to match
- **Code editor** — syntax highlighting, code folding, find/replace, go to definition, indent guides
- **Debugger** — step through OpenSCAD code with breakpoints, call stack, and variable inspection
- **Animation** — preview animated models with playback controls
- **CSG operations** — union, difference, intersection powered by the Manifold kernel
- **Data viewers** — inspect lists, VNF meshes, paths, and grids
- **Export** — STL, OBJ, 3MF (with per-object color)

## Installation

Requires Python 3.11+.

```bash
git clone https://github.com/BelfrySCAD/BelfrySCAD.git
cd BelfrySCAD
uv sync
uv run belfryscad
```

Or with pip:

```bash
pip install -e .
belfryscad
```

## Building Installers

BelfrySCAD uses [Briefcase](https://briefcase.readthedocs.io/) for platform packaging.

```bash
# macOS (.dmg)
uv run briefcase create macOS app
uv run briefcase build macOS app
uv run briefcase package macOS app --adhoc-sign

# Windows (.msi)
uv run briefcase create windows app
uv run briefcase build windows app
uv run briefcase package windows app

# Linux (.AppImage)
uv run briefcase create linux appimage
uv run briefcase build linux appimage
uv run briefcase package linux appimage
```

## Running Tests

```bash
uv run pytest
```

## Technology Stack

- **UI**: PySide6 (Qt)
- **Parser**: [openscad_lalr_parser](https://pypi.org/project/openscad-lalr-parser/)
- **CSG kernel**: [Manifold](https://github.com/elalish/manifold)
- **Renderer**: ModernGL
- **Language**: Python

## License

MIT
