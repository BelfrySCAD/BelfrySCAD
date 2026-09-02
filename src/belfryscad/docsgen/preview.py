"""Docs preview for one editor buffer.

Runs the same parser, the same validation and the same markdown generator a
full `--docsgen` build uses, but over unsaved editor text rather than a file
on disk, and writes its images to a cache directory instead of the project's
docs tree. Qt-free, so the GUI can call it from a worker thread.

Images land in ~/.cache/BelfrySCAD/docs-preview/<file>-<hash>/, which is
what makes repeat previews cheap: an example whose rendered bytes match the
cached copy is left alone, so only genuinely changed examples cost anything.
"""
from __future__ import annotations

import hashlib
import os
import re
import os.path
from dataclasses import dataclass, field
from pathlib import Path

from .unicode_math import render_math

CACHE_DIR = Path.home() / ".cache" / "BelfrySCAD" / "docs-preview"


@dataclass
class DocsPreview:
    """markdown, plus the directory its relative image links resolve
    against, plus everything the parser complained about."""
    markdown: str = ""
    base_dir: str = ""
    errors: list = field(default_factory=list)   # (file, line, msg, level)

    @property
    def has_errors(self) -> bool:
        from .errorlog import ErrorLog
        return any(level == ErrorLog.FAIL for _f, _l, _m, level in self.errors)


def find_rc(src_file: str) -> str | None:
    """The nearest .openscad_docsgen_rc at or above src_file's directory.

    Upstream only ever looks in the current directory, because it is always
    run from the project root. The GUI is not, so the file being edited has
    to lead back to its project's settings -- the ProjectName, TargetProfile
    and ColorScheme in there are what make a preview look like the real
    published page.
    """
    from .parser import DocsGenParser
    d = Path(src_file).resolve().parent
    for folder in [d, *d.parents]:
        candidate = folder / DocsGenParser.RCFILE
        if candidate.is_file():
            return str(candidate)
    return None


def _cache_dir(src_file: str) -> str:
    digest = hashlib.sha256(str(Path(src_file).resolve()).encode()).hexdigest()[:12]
    return str(CACHE_DIR / f"{Path(src_file).stem}-{digest}")


def invalidate_cache(src_file: str) -> int:
    """Throw away every rendered image for `src_file`; returns how many.

    The cache key is the file's path, deliberately not its contents, so an
    image survives editing the script around it -- that is what makes
    click-to-render accumulate. The cost is that an Example whose code HAS
    changed keeps showing the old picture, and nothing else ever clears it.
    Refresh is the way out.
    """
    import shutil
    directory = Path(_cache_dir(src_file))
    count = len(list(directory.rglob("*.png"))) if directory.is_dir() else 0
    shutil.rmtree(directory, ignore_errors=True)
    return count


def invalidate_image(src_file: str, rel: str) -> bool:
    """Drop one rendered image from `src_file`'s cache. True if it was there.

    `invalidate_cache` above is the Refresh button's hammer -- it throws away
    every image for the file, and on a BOSL2-sized file re-rendering them all
    is minutes of work. This is the single-image version behind the Docs
    pane's "Re-render This Image", for the case where one Example's picture
    is stale: its code changed, or the renderer itself did (a translucency
    fix that changed every transparent render is what prompted this -- the
    cache is keyed by source path and content, so nothing about a renderer
    change invalidates it).
    """
    root = Path(_cache_dir(src_file)).resolve()
    # `rel` comes out of the rendered document, so treat it as untrusted and
    # refuse anything that resolves outside the cache directory.
    target = (root / rel).resolve()
    if root not in target.parents:
        return False
    try:
        target.unlink()
    except OSError:
        return False
    return True


def build_preview(source_text: str, src_file: str, gen_images: bool = True,
                   images=None, progress=None) -> DocsPreview:
    """Parse `source_text` as if it were `src_file`, and return its rendered
    docs plus every error and warning found along the way.

    `images` selects which Examples to render: None renders every one,
    an empty collection renders none, and a collection of image paths
    renders just those. Images already on disk from an earlier build are
    reused either way, so rendering one at a time accumulates.
    """
    from . import default_options
    from .errorlog import errorlog
    from .imagemanager import image_manager
    from .logmanager import log_manager
    from .parser import DocsGenParser, DocsGenException
    from .runner import runner

    docs_dir = _cache_dir(src_file)
    opts = default_options(docs_dir=docs_dir, quiet=True, gen_imgs=gen_images)

    # The rc file is read by DocsGenParser.__init__, so the path has to be in
    # place before construction -- hence a per-call subclass rather than an
    # instance attribute. A subclass also keeps this off the shared class,
    # which matters because the CLI uses the same parser in the same process.
    rc = find_rc(src_file)
    parser_cls = (type("PreviewParser", (DocsGenParser,), {"RCFILE": rc})
                  if rc else DocsGenParser)

    result = DocsPreview()
    errorlog.errlist.clear()
    errorlog.badfiles.clear()
    errorlog.has_errors = False
    image_manager.purge_requests()
    log_manager.purge_requests()

    # Examples resolve `include <...>` relative to the file being edited,
    # which the bare basename below cannot express.
    runner.src_dir_override = str(Path(src_file).resolve().parent)
    try:
        parser = parser_cls(opts)
        # The rc's own DocsDirectory would send images into the project's
        # real docs tree. A preview must never write there, and the rc is
        # re-read on every file parsed, so this has to be locked, not just
        # assigned.
        parser.opts.lock_docs_dir(docs_dir)

        # A bare basename, deliberately: blocks.py builds image paths from
        # this, and an absolute path would place them beside the source file
        # instead of inside the cache directory.
        name = os.path.basename(src_file)
        parser.parse_lines(source_text.splitlines(), line_num=0, src_file=name)

        if not parser.file_blocks:
            result.errors = list(errorlog.errlist)
            return result

        # Only inside a real docsgen project, and only after our own file is
        # parsed, so file_blocks[0] stays the one being previewed.
        if rc:
            _parse_siblings(parser, src_file, name)

        target = parser.opts.target
        fblock = parser.file_blocks[0]
        lines = target.postprocess(fblock.get_file_lines(parser, target))
        if gen_images and images != []:
            image_manager.process_requests(test_only=False, only=images, progress=progress)
        else:
            image_manager.purge_requests()

        result.markdown = "\n".join(lines)
        # Relative image links in the markdown are resolved against the
        # directory the generated .md file would have lived in.
        result.base_dir = os.path.dirname(os.path.join(docs_dir, name)) or docs_dir
    except DocsGenException as e:
        from .errorlog import ErrorLog
        errorlog.add_entry(os.path.basename(src_file), 0, str(e), ErrorLog.FAIL)
    finally:
        runner.src_dir_override = None
        result.errors = list(errorlog.errlist)
        errorlog.errlist.clear()
        errorlog.badfiles.clear()
        errorlog.has_errors = False

    return result


def _parse_siblings(parser, src_file: str, skip_name: str):
    """Parse the other .scad files in the same folder, purely to populate the
    name table.

    Without this, every cross-file `See Also:` and `[link]()` in the file
    reports "Invalid Link" -- five false errors on a single BOSL2 file, which
    would make the pane's error list useless. A full docsgen run resolves
    them because it parses the whole library at once.

    Cheap enough to do on every preview: all 58 BOSL2 files parse in about
    0.2s, since no images or scripts are involved. Errors and output from
    the neighbours are discarded -- they are not what the user is editing.
    """
    import contextlib
    import io
    from .errorlog import errorlog
    from .logmanager import log_manager

    folder = Path(src_file).resolve().parent
    keep = list(errorlog.errlist)
    log_manager.enabled = False
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            for path in sorted(folder.glob("*.scad")):
                if path.name == skip_name:
                    continue
                try:
                    parser.parse_file(str(path))
                except Exception:      # noqa: BLE001 -- a broken neighbour is not this file's problem
                    continue
    finally:
        log_manager.enabled = True
        errorlog.errlist[:] = keep
        errorlog.badfiles.clear()
        errorlog.has_errors = False


# The docsgen targets emit a small, closed set of raw HTML inside their
# markdown -- verified by scanning target_wiki.py, target_githubwiki.py and
# blocks.py for tags, not by guessing. Qt's markdown reader drops raw HTML
# silently, which would lose every example image, so each of these is turned
# back into the markdown equivalent Qt does understand.
def _image_link(m):
    """`![alt](src)` with the alt text's backslash escapes removed.

    docsgen escapes underscores for GitHub, so an alt reads
    `ball\\_bearing() Example 1`. Qt's markdown parser splits an image
    apart at each escape and emits one copy per fragment -- three side-by-side
    copies of every BOSL2 example image, which is how this was found. The
    escapes are only needed by GitHub's renderer, so they come out here.
    """
    return "![{}]({})".format(m.group(1).replace("\\", ""), m.group(2))


_HTML_FIXUPS = (
    # <img align="left" alt="cuboid() Example 1" src="images/x.png" width=...>
    (r'<img\b[^>]*?\balt="([^"]*)"[^>]*?\bsrc="([^"]*)"[^>]*>', _image_link),
    (r'<a\b[^>]*?\bhref="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)'),
    (r'<code>(.*?)</code>', r'`\1`'),
    # <abbr>/<sup> carry mouseover help text that has nowhere to go in a
    # QTextBrowser; keep the visible label, drop the wrapper.
    (r'</?(?:abbr|sup)\b[^>]*>', ''),
    (r'<br\b[^>]*/?>', ''),
)


#: A pipe-table's separator row: dashes, colons, pipes and spaces, nothing
#: else. What tells a table apart from an ordinary line that has a `|` in it.
_TABLE_SEPARATOR = re.compile(r"^[-:| ]*\|[-:| ]*$")


def _collapse_spaces_outside_code(line: str) -> str:
    """Runs of spaces down to one, leaving code spans exactly as they are."""
    return "".join(
        part if part.startswith("`") else re.sub(r" {2,}", " ", part)
        for part in re.split(r"(`+[^`]*`+)", line)
    )


def collapse_table_spaces(markdown: str) -> str:
    """Squeeze runs of spaces inside tables to one, outside `code spans`.

    docsgen writes two spaces after a sentence and pads every cell out to a
    column, which is what makes the raw markdown readable. HTML collapses
    all of that; QTextDocument does not -- it is not an HTML layout and
    keeps every space it is given -- so the pane showed "the cube.  Default"
    and GitHub showed "the cube. Default".

    Only tables, because that is where the padding is, and only outside
    backticks, where spacing is the author's and has to survive.
    """
    lines = markdown.split("\n")
    out = list(lines)
    i, fenced = 0, False
    while i < len(lines):
        if lines[i].lstrip().startswith("```"):
            fenced = not fenced
        elif (not fenced and "|" in lines[i]
              and i + 1 < len(lines) and _TABLE_SEPARATOR.match(lines[i + 1])):
            while i < len(lines) and lines[i].strip():
                out[i] = _collapse_spaces_outside_code(lines[i])
                i += 1
            continue
        i += 1
    return "\n".join(out)


def markdown_for_qt(markdown: str) -> str:
    """The same markdown, rewritten so QTextDocument.setMarkdown renders it
    fully. Only display markup changes -- no content is added or removed."""
    import re
    out = markdown
    for pattern, replacement in _HTML_FIXUPS:
        out = re.sub(pattern, replacement, out, flags=re.S)
    # Pandoc-style fence attributes confuse Qt's info-string handling.
    out = out.replace("``` {.C linenos=True}", "```")
    out = render_math(out)
    return collapse_table_spaces(out.replace("&nbsp;", " "))
