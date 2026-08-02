"""Static OpenSCAD source scanner backing -d/--deps (Makefile dependency
file generation) and -m/--make (build missing files via a Makefile before
use). Mirrors real OpenSCAD's handle_dep()/write_deps() (src/handle_dep.cc)
-- verified against that upstream source directly, since --help's one-line
descriptions don't spell out the exact semantics: use/include only ever
register a dependency once the target is *found* (real OpenSCAD's lexer
gates handle_dep() on find_valid_path() already having succeeded), while
import()/surface()/*_extrude(file=...) register their target unconditionally,
found or not -- that asymmetry is *why* -m exists at all: a missing import()
target is exactly the case a Makefile rule can generate on demand before
evaluation needs it, whereas a missing use/include is just a plain error.

This is a best-effort regex scan, not a real parse -- openscad_cpp_evaluator's
C++ parser doesn't expose a list of files it touched. It strips // and /* */
comments, then looks for `use <...>`/`include <...>` (recursed into
transitively) and import()/surface()/linear_extrude(file=...)/
rotate_extrude(file=...) calls with a literal string filename. A filename
built from an expression rather than a literal string can't be seen by a
static scan and won't be tracked -- an accepted limitation, not a bug to
fix here.
"""

import re
import shlex
import subprocess
import sys
from pathlib import Path

_USE_INCLUDE_RE = re.compile(r'\b(?:use|include)\s*<([^>]+)>')
_IMPORT_RE = re.compile(r'\b(?:import|surface)\s*\(\s*(?:file\s*=\s*)?"((?:[^"\\]|\\.)*)"')
_EXTRUDE_FILE_RE = re.compile(r'\b(?:linear_extrude|rotate_extrude)\s*\([^)]*?\bfile\s*=\s*"((?:[^"\\]|\\.)*)"')


def _strip_comments(text: str) -> str:
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    return re.sub(r'//[^\n]*', '', text)


def _resolve(target: str, base_dir: Path) -> Path | None:
    p = Path(target)
    candidate = p if p.is_absolute() else base_dir / p
    return candidate if candidate.is_file() else None


def scan_dependencies(main_path: str) -> list[str]:
    """Every file real OpenSCAD's handle_dep() would have registered while
    parsing/evaluating main_path: the main file itself, every use/include
    target found (recursed into transitively), and every import()/surface()/
    *_extrude(file=...) literal target (included whether or not it exists
    yet). Order: first-encountered; duplicates removed."""
    deps: list[str] = []
    seen: set[str] = set()

    def add(path_str: str):
        if path_str not in deps:
            deps.append(path_str)

    def visit(path: Path):
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            return
        seen.add(key)
        add(str(path))
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return
        stripped = _strip_comments(text)
        base_dir = path.parent

        for target in _USE_INCLUDE_RE.findall(stripped):
            resolved = _resolve(target, base_dir)
            if resolved is not None:
                visit(resolved)

        for target in _IMPORT_RE.findall(stripped) + _EXTRUDE_FILE_RE.findall(stripped):
            resolved = _resolve(target, base_dir)
            add(str(resolved) if resolved is not None else str(base_dir / target))

    visit(Path(main_path))
    return deps


def write_deps_file(deps_path: str, output_files: list[str], dependencies: list[str]) -> bool:
    """Real OpenSCAD's exact write_deps() format (src/handle_dep.cc):
    '<output1> <output2>: \\\n\t<dep1> \\\n\t<dep2> ...\n' -- a standard
    Makefile rule with backslash-continued, tab-indented dependency lines."""
    try:
        with open(deps_path, 'w', encoding='utf-8') as f:
            f.write(' '.join(output_files) + ':')
            for dep in dependencies:
                f.write(f' \\\n\t{dep}')
            f.write('\n')
    except OSError as e:
        print(f"belfryscad: can't open dependencies file {deps_path!r} for writing: {e}", file=sys.stderr)
        return False
    return True


def run_make_for_missing(main_path: str, make_cmd: str) -> None:
    """Before evaluation: if main_path doesn't exist, run `<make_cmd>
    <main_path>` (a shell command, matching real OpenSCAD's own
    system(make_cmd + " '" + path + "'") in handle_dep()) so a Makefile
    rule can produce it. Once main_path exists (whether it already did, or
    was just built), scan it for import()/surface()/*_extrude(file=...)
    targets and do the same for any of those that are still missing.
    Errors are reported but not fatal here -- if a file is still missing
    afterward, the subsequent parse/import fails on its own with a clear
    error, same as upstream."""
    def make(path_str: str):
        cmd = f"{make_cmd} {shlex.quote(path_str)}"
        result = subprocess.run(cmd, shell=True)
        if result.returncode != 0:
            print(f"belfryscad: {cmd!r}: exit status {result.returncode}", file=sys.stderr)

    if not Path(main_path).exists():
        make(main_path)

    if Path(main_path).is_file():
        for path_str in scan_dependencies(main_path):
            if path_str != main_path and not Path(path_str).exists():
                make(path_str)
