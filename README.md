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
- **Customizer** — edit a script's parameters through generated widgets, with saved parameter sets
- **Documentation generation** — `openscad_docsgen`-compatible docs and example images, in the GUI or headless
- **Coverage** — see which statements, branch arms and module bodies a script actually ran
- **AI chat** — provider-agnostic assistant that proposes source edits for review before applying
- **Command line** — headless render and export, animation frames, dependency lists, test runner
- **Export** — 3MF, AMF, OBJ, OFF, PLY, STL, VRML and X3D, plus SVG and PDF for 2D designs; colour is carried by the formats that support it

## Installation

Prebuilt installers are attached to every
[release](https://github.com/BelfrySCAD/BelfrySCAD/releases/latest) — a Windows
`.msi` and a Linux `.AppImage`. On macOS, run from source or build a `.dmg`
yourself (see Building Installers below).

### From source

Requires Python 3.12+, and macOS 13.3 or later on a Mac — the floor comes from
the `openscad_cpp_evaluator` wheel.

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
- **Parser & evaluator**: [openscad_cpp_evaluator](https://pypi.org/project/openscad-cpp-evaluator/) (C++; bundles the Bison-based `openscad_cpp_parser`)
- **CSG kernel**: [Manifold](https://github.com/elalish/manifold)
- **Renderer**: ModernGL
- **Language**: Python

## Documentation

Implementation notes live in [`docs/`](docs/): [editor](docs/editor.md),
[rendering](docs/rendering.md), [WYSIWYG interaction](docs/wysiwyg.md),
[debugger](docs/debugger.md) and [documentation generation](docs/docsgen.md).

## License

MIT
