"""Docs pane -- formatted preview and validation of a file's openscad_docsgen
comments, with Examples and Figures rendered as real images.

The work is the same code `belfryscad --docsgen` runs (belfryscad.docsgen),
so what shows here is what a docs build would publish, and an error listed
here is an error that build would report.

It all happens on one dedicated worker thread. That is not just to keep the
UI responsive: docsgen's parser, its error log and the offscreen renderer
are process-wide singletons, so two previews running at once would tread on
each other. Requests that arrive while a build is running are coalesced --
only the newest survives, since older ones describe text the user has
already moved past.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import (QColor, QDesktopServices, QFontInfo, QImage, QPalette, QTextBlockFormat, QTextCursor, QTextDocument,
                            QTextCharFormat, QTextFormat, QTextFrameFormat, QTextTable)
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton,
                                QSplitter, QTextBrowser, QTreeWidget,
                                QTreeWidgetItem, QVBoxLayout, QWidget)

_LEVEL_ORDER = {"error": 0, "warning": 1, "notice": 2}

# The numbers below are taken from GitHub's own published markdown CSS
# (github-markdown-css, the mirror of what renders a BOSL2 wiki page), so
# this pane reads like the page the docs will actually be published to.
# Colours are expressed as a blend between the palette's text and base
# rather than GitHub's literal hex, so they track a dark theme instead of
# freezing the light one.

#: `pre { padding: 16px }`. Qt block backgrounds already span the full text
#: width, so the whole inset is left/right margin -- there is no separate
#: outer indent.
_CODE_PADDING = 16
_CODE_VERTICAL_PADDING = 12

#: `h1/h2 { padding-bottom: .3em }`, plus the space above that GitHub gets
#: from `h1 { margin: .67em 0 }`. Qt gives headings no margins of their own.
_HEADING_TOP_MARGIN = {1: 22, 2: 16}
_HEADING_RULE_GAP = 6

#: `border-bottom: 1px solid var(--borderColor-muted)` -- #d1d9e0 at 70%
#: alpha over white, about #dde2e7. That is a hairline, far lighter than
#: Qt's default (which draws the rule in the full text colour).
_RULE_FADE = 0.86

#: `--bgColor-muted: #f6f8fa` -- the shared background of code blocks and
#: striped table rows. Nine points off white, where a flat mid-grey would
#: shout.
_TINT_MIX = 0.035

#: `table th, table td { border: 1px solid var(--borderColor-default) }`
#: -- #d1d9e0, a touch stronger than the heading rule.
_TABLE_BORDER_FADE = 0.78

#: `table th, table td { padding: 6px 13px }`. Qt has one padding value per
#: table rather than per axis, so this takes the vertical figure.
_TABLE_CELL_PADDING = 6

#: A render placeholder sits in its own tinted box. Stronger than the code
#: tint so the two do not read as the same kind of thing -- one is content,
#: the other is a control. Qt has no block border (only frames and table
#: cells have one), and wrapping each placeholder in a table would change
#: the document's block structure, which the scroll anchoring relies on
#: staying put -- so the box is drawn with background and padding alone.
_PLACEHOLDER_TINT_MIX = 0.10
_PLACEHOLDER_PADDING = 10

#: GitHub's heading scale, as multiples of the body size, with its
#: `font-weight: 600`. Qt sizes headings with a legacy FontSizeAdjustment
#: instead, which lands every level at the body size here -- so h1 and h2
#: came out barely larger than the prose they head.
_HEADING_SCALE = {1: 2.0, 2: 1.5, 3: 1.25, 4: 1.0, 5: 0.875, 6: 0.85}
_HEADING_WEIGHT = 600

#: GitHub's monospace stack, minus the two entries Qt cannot reach: its
#: `ui-monospace`/`SF Mono` resolve through the browser rather than the font
#: database, so on macOS this starts at Menlo -- GitHub's own next choice --
#: and still names Consolas and Liberation Mono for Windows and Linux.
_MONO_STACK = ["Menlo", "Consolas", "Liberation Mono", "Courier New", "monospace"]


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    """`a` moved `t` of the way toward `b`."""
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


def _apply_rule_color(view):
    """Make the heading rules grey instead of Qt's default hard black.

    Qt draws a BlockTrailingHorizontalRulerWidth with the palette's
    WindowText role, NOT Text -- established by setting each role in turn
    and seeing which one moved the rule. In a QTextBrowser the document's
    own text is drawn with Text, so overriding WindowText recolours every
    rule and leaves the prose alone. Confirmed: with WindowText at #8b949e
    the rule is #8b949e and the body text is still black.

    Derived from the palette rather than hardcoded, so it stays a soft rule
    in a dark theme too.
    """
    palette = view.palette()
    grey = _blend(palette.text().color(), palette.base().color(), _RULE_FADE)
    palette.setColor(QPalette.ColorRole.WindowText, grey)
    view.setPalette(palette)


def _stripe_tables(doc, palette):
    """Tint alternating body rows of every table.

    Done per cell because Qt's CSS subset has no :nth-child, and because a
    document built by setMarkdown ignores setDefaultStyleSheet entirely --
    that only applies when Qt parses HTML.

    Row 0 is the header and keeps its own look; striping starts from the
    second body row so the first row under the header stays clear.
    """
    tint = _code_tint(palette)

    def tables_in(frame):
        for child in frame.childFrames():
            if isinstance(child, QTextTable):
                yield child
            yield from tables_in(child)

    border = _blend(palette.text().color(), palette.base().color(), _TABLE_BORDER_FADE)
    for table in tables_in(doc.rootFrame()):
        # GitHub gives every cell a 1px border and a little breathing room;
        # Qt's default table is tighter and heavier than that.
        tfmt = table.format()
        tfmt.setBorder(1)
        tfmt.setBorderBrush(border)
        tfmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
        tfmt.setCellPadding(_TABLE_CELL_PADDING)
        tfmt.setCellSpacing(0)
        table.setFormat(tfmt)
        for row in range(2, table.rows(), 2):
            for col in range(table.columns()):
                cell = table.cellAt(row, col)
                fmt = cell.format()
                fmt.setBackground(tint)
                cell.setFormat(fmt)


def _code_tint(palette) -> QColor:
    """The code block's background: the pane's own base colour nudged a
    little toward its text colour.

    Derived from the palette rather than hardcoded, so it stays a subtle
    tint in a dark theme instead of a bright slab -- the tint tracks
    whichever way the contrast runs.
    """
    return _blend(palette.base().color(), palette.text().color(), _TINT_MIX)


#: URL scheme for a click-to-render placeholder. Not a real scheme -- the
#: pane intercepts it in anchorClicked and never navigates.
_RENDER_SCHEME = "bfsrender"

#: href of the status line's "render all" link.
_RENDER_ALL_HREF = "bfsrenderall"

#: Scheme for a remote image's stand-in link. The real URL rides in the
#: path. It gets its own scheme rather than being left as a plain http link
#: so the box can be applied by target: the docs are full of ordinary
#: prose links to wikipedia and the like, and boxing those would be wrong.
_REMOTE_SCHEME = "bfsremote"


#: What a click-to-render placeholder reads before it is clicked. Shared so
#: the label can be recovered by stripping it back off again.
_RENDER_PREFIX = "\u25b6 Render "

#: A rendering placeholder's trailing ellipsis grows by one dot a second.
#: Rendering an Example takes seconds and "render all" takes minutes, with
#: nothing else on screen moving, so without this the pane looks hung.
_ELLIPSIS_MS = 1000
_ELLIPSIS = ("", ".", "..", "...")


def placeholder_markdown(md: str, base_dir: str) -> tuple:
    """`md` with every image that is not on disk yet replaced by a
    click-to-render link, plus the list of those images' paths.

    Rendering every Example in a BOSL2 file up front costs minutes, and an
    image reference whose file does not exist renders as a broken-image
    icon -- so an unrendered document used to be a wall of broken links.
    A placeholder is both faster to show and honest about what it is.

    Images already rendered are left as images, so clicking one placeholder
    at a time accumulates a fully-rendered page.
    """
    import os
    import re

    pending = []

    def swap(m):
        alt, rel = m.group(1), m.group(2)
        label = alt.strip() or os.path.basename(rel)
        if rel.startswith(("http://", "https://")):
            # A remote image -- BOSL2's isosurface.scad embeds animated GIFs
            # straight from raw.githubusercontent.com. QTextBrowser does no
            # network fetching, so leaving these as images renders a broken
            # icon, and offering to "render" them would be a lie: there is
            # no Example behind them. An honest link that opens in a browser
            # is the most this pane can truthfully do.
            return f"[\U0001f517 {label} (remote image)]({_REMOTE_SCHEME}:{rel})"
        if os.path.exists(os.path.join(base_dir, rel)):
            return m.group(0)
        pending.append(rel)
        return f"[{_RENDER_PREFIX}{label}]({_RENDER_SCHEME}:{rel})"

    return re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", swap, md), pending


#: `ul, ol { padding-left: 2em }`. Qt's own default is a flat 40px per
#: level regardless of font size, which is half again as deep as GitHub at
#: this pane's 13pt and does not track a font-size change at all.
_LIST_INDENT_EM = 2


def _set_list_indent(doc):
    """Pull lists in to GitHub's depth.

    One document-level property rather than a walk over every list: Qt
    multiplies indentWidth by the list's nesting level itself, so setting
    it keeps nested lists proportional for free.
    """
    doc.setIndentWidth(_LIST_INDENT_EM * QFontInfo(doc.defaultFont()).pixelSize())


def _style_headings(doc):
    """Give level-1 and level-2 headings breathing room above and a rule
    below, the way a rendered wiki page presents them.

    The rule is Qt's own BlockTrailingHorizontalRulerWidth -- the very
    property its markdown reader sets on a `---` block -- applied to the
    heading block itself, so it is drawn by the same code path rather than
    faked with a border or an inserted empty block.

    Deeper headings (Module:/Function: entries, level 3+) are deliberately
    left alone: BOSL2 files carry dozens of them, and ruling every one turns
    the page into a ladder.
    """
    base_size = doc.defaultFont().pointSizeF()
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    block = doc.begin()
    while block.isValid():
        fmt_in = block.blockFormat()
        level = fmt_in.headingLevel()
        if level in _HEADING_SCALE:
            # Size and weight first: every level gets GitHub's scale, not
            # just the two that are ruled.
            cursor.setPosition(block.position())
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                 QTextCursor.MoveMode.KeepAnchor)
            char = QTextCharFormat()
            char.setFontPointSize(base_size * _HEADING_SCALE[level])
            char.setFontWeight(_HEADING_WEIGHT)
            cursor.mergeCharFormat(char)
        if level in _HEADING_TOP_MARGIN:
            fmt = QTextBlockFormat(fmt_in)
            fmt.setTopMargin(_HEADING_TOP_MARGIN[level])
            fmt.setBottomMargin(_HEADING_RULE_GAP)
            fmt.setProperty(QTextFormat.Property.BlockTrailingHorizontalRulerWidth, 1)
            cursor.setPosition(block.position())
            cursor.setBlockFormat(fmt)
        block = block.next()
    cursor.endEditBlock()


def _style_placeholders(doc, palette):
    """Box every render placeholder and remote-image stand-in.

    Found by the link target rather than the visible text, so the wording
    can change without silently losing the box -- and so ordinary prose
    links (the docs are full of them) are left alone.
    """
    tint = _blend(palette.base().color(), palette.text().color(),
                  _PLACEHOLDER_TINT_MIX)
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        is_placeholder = False
        while not it.atEnd():
            frag = it.fragment()
            href = frag.charFormat().anchorHref() if frag.isValid() else ""
            if href.startswith((_RENDER_SCHEME + ":", _REMOTE_SCHEME + ":")):
                is_placeholder = True
                break
            it += 1
        if is_placeholder:
            fmt = QTextBlockFormat(block.blockFormat())
            fmt.setBackground(tint)
            fmt.setLeftMargin(_PLACEHOLDER_PADDING)
            fmt.setRightMargin(_PLACEHOLDER_PADDING)
            fmt.setTopMargin(_PLACEHOLDER_PADDING)
            fmt.setBottomMargin(_PLACEHOLDER_PADDING)
            cursor.setPosition(block.position())
            cursor.setBlockFormat(fmt)
        block = block.next()
    cursor.endEditBlock()


def _style_code_blocks(doc, palette):
    """Give every code block a tinted, indented box.

    Qt's markdown reader renders a code block as plain monospace text with
    no background of its own, so this walks the finished document and
    applies the block format. Code blocks are found by Qt's own
    BlockCodeLanguage property -- set for indented and fenced blocks alike,
    and never for prose -- rather than by guessing from the font.

    Each line of a block is its own QTextBlock, so the tint is applied per
    line with zero spacing between them; they abut into one continuous box.
    The first and last line of a run carry the box's top and bottom
    padding.
    """
    tint = _code_tint(palette)
    runs, current = [], []
    block = doc.begin()
    while block.isValid():
        if block.blockFormat().hasProperty(QTextFormat.Property.BlockCodeLanguage):
            current.append(block)
        elif current:
            runs.append(current)
            current = []
        block = block.next()
    if current:
        runs.append(current)

    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    for run in runs:
        for i, blk in enumerate(run):
            fmt = QTextBlockFormat(blk.blockFormat())
            fmt.setBackground(tint)
            fmt.setLeftMargin(_CODE_PADDING)
            fmt.setRightMargin(_CODE_PADDING)
            # Padding lives on the outer lines only, so the run reads as one
            # box rather than a stack of separately-spaced strips.
            fmt.setTopMargin(_CODE_VERTICAL_PADDING if i == 0 else 0)
            fmt.setBottomMargin(_CODE_VERTICAL_PADDING if i == len(run) - 1 else 0)
            cursor.setPosition(blk.position())
            cursor.setBlockFormat(fmt)
            # Name the whole stack rather than one family, so this still
            # picks a real monospace face off macOS.
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                 QTextCursor.MoveMode.KeepAnchor)
            char = QTextCharFormat()
            char.setFontFamilies(_MONO_STACK)
            cursor.mergeCharFormat(char)
    cursor.endEditBlock()


def mark_rendering(doc, rels):
    """Turn every placeholder about to be rendered into a live label.

    `rels` is the list of images queued, or None for "render all". The
    block format is left alone, so the box stays; only the text and its
    link change, which also stops a second click queueing the same image
    again. Returns the targets write_rendering_text then drives.
    """
    targets = []
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            href = frag.charFormat().anchorHref() if frag.isValid() else ""
            if href.startswith(_RENDER_SCHEME + ":"):
                rel = href[len(_RENDER_SCHEME) + 1:]
                if rels is None or rel in rels:
                    label = block.text().strip()
                    if label.startswith(_RENDER_PREFIX):
                        label = label[len(_RENDER_PREFIX):]
                    fmt = QTextCharFormat(frag.charFormat())
                    fmt.setAnchor(False)
                    fmt.setAnchorHref("")
                    fmt.setFontUnderline(False)
                    fmt.clearForeground()
                    targets.append((block.blockNumber(), f"Rendering {label}", fmt))
                break
            it += 1
        block = block.next()
    # Collected first, then written by the caller: editing inside the walk
    # would be rewriting the very blocks it is iterating.
    return targets


def write_rendering_text(doc, targets, dots: int):
    """Rewrite each marked block with `dots` trailing dots."""
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    for number, text, fmt in targets:
        block = doc.findBlockByNumber(number)
        if not block.isValid():
            continue
        cursor.setPosition(block.position())
        cursor.setPosition(block.position() + block.length() - 1,
                           QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(text + _ELLIPSIS[dots % len(_ELLIPSIS)], fmt)
    cursor.endEditBlock()


#: How often the animation clock ticks. Not the frame rate -- each image
#: keeps its own delay and advances when that much has accumulated, so one
#: timer drives any mix of speeds.
_ANIM_TICK_MS = 40


class _ApngPlayer:
    """One animated image in the document.

    docsgen writes Spin/Anim examples as APNG, and QTextDocument animates
    nothing at all -- Qt's image reader hands it frame 0 and stops. So the
    document keeps referring to the same image URL and this swaps what that
    URL resolves to, one frame at a time.

    Frames are decoded on demand and then kept: a 36-frame 320x240 example
    is ~11MB decoded but only ~170KB compressed, so decoding everything up
    front would stall the first tick and charge full price for an animation
    the user may never scroll to.
    """

    def __init__(self, url, frames, delay_ms):
        self.url = url
        self._encoded = frames
        self._decoded = [None] * len(frames)
        self.delay = max(_ANIM_TICK_MS, delay_ms)
        self.elapsed = 0
        self.index = 0

    def advance(self, dt):
        """The next frame, or None if this image is not due yet."""
        self.elapsed += dt
        if self.elapsed < self.delay:
            return None
        self.elapsed = 0
        self.index = (self.index + 1) % len(self._encoded)
        if self._decoded[self.index] is None:
            self._decoded[self.index] = QImage.fromData(self._encoded[self.index])
        return self._decoded[self.index]


class _DocsWorker(QObject):
    """Lives on the pane's worker thread; owns every docsgen call."""
    ready = Signal(object)      # belfryscad.docsgen.preview.DocsPreview
    failed = Signal(str)

    request = Signal(str, str, object)   # source_text, src_file, image selection

    def __init__(self):
        super().__init__()
        self.request.connect(self._build, Qt.ConnectionType.QueuedConnection)

    def _build(self, source_text: str, src_file: str, images):
        """`images` is None for every image, [] for none, or a list of
        specific image paths."""
        from belfryscad.docsgen.preview import build_preview
        try:
            self.ready.emit(build_preview(source_text, src_file,
                                           gen_images=images != [], images=images))
        except Exception as e:      # a broken preview must never take the app down
            self.failed.emit(f"{type(e).__name__}: {e}")


class DocsPane(QWidget):
    """Rendered docs on top, the parser's complaints underneath."""

    goto_line = Signal(int)         # a clicked error's line in the source
    refresh_requested = Signal()    # the Refresh button; MainWindow supplies the text

    def __init__(self, parent=None):
        super().__init__(parent)

        self._view = QTextBrowser()
        self._view.setOpenExternalLinks(False)
        self._view.setOpenLinks(False)

        self._errors = QTreeWidget()
        self._errors.setHeaderLabels(["Line", "Level", "Message"])
        self._errors.setRootIsDecorated(False)
        self._errors.setColumnWidth(0, 60)
        self._errors.setColumnWidth(1, 70)
        self._errors.itemActivated.connect(self._on_error_activated)
        self._errors.itemClicked.connect(self._on_error_activated)

        self._status = QLabel("No preview yet.")
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh_requested)
        # "Render all" is a link in the status line rather than a button:
        # it belongs with the sentence that says how many images are
        # outstanding, and it only applies when some are.
        self._status.setTextFormat(Qt.TextFormat.RichText)
        self._status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        self._status.linkActivated.connect(self._on_status_link)

        top = QHBoxLayout()
        top.setContentsMargins(4, 4, 4, 0)
        top.addWidget(self._refresh_btn)
        top.addWidget(self._status, 1)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self._view)
        split.addWidget(self._errors)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(split, 1)

        self._thread = QThread(self)
        self._worker = _DocsWorker()
        self._worker.moveToThread(self._thread)
        self._worker.ready.connect(self._on_ready)
        self._worker.failed.connect(self._on_failed)
        self._thread.start()

        self._busy = False
        self._pending = None
        #: Last (text, path) built, so a placeholder click can rebuild the
        #: same content with one more image rendered.
        self._last_source = None
        #: Image paths shown as placeholders in the current document.
        self._pending_images = []
        #: Where to put the view back after a rebuild -- see _capture_scroll.
        self._scroll_anchor = None

        #: Placeholders currently reading "Rendering ...", as
        #: (block number, text without dots, char format).
        self._rendering = []
        self._dots = 0
        self._dots_timer = QTimer(self)
        self._dots_timer.setInterval(_ELLIPSIS_MS)
        self._dots_timer.timeout.connect(self._on_dots_tick)

        #: Animated images in the current document -- see _ApngPlayer.
        self._players = []
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(_ANIM_TICK_MS)
        self._anim_timer.timeout.connect(self._on_anim_tick)

        self._view.anchorClicked.connect(self._on_anchor_clicked)

    # -- driving -------------------------------------------------------

    def _on_anchor_clicked(self, url):
        """A placeholder click renders just that one image; every other link
        is left alone (the docs are full of real cross-references)."""
        text = url.toString()
        if url.scheme() == _REMOTE_SCHEME:
            # The real URL rides in the path; hand it to the browser, since
            # this pane cannot fetch over the network.
            QDesktopServices.openUrl(QUrl(text[len(_REMOTE_SCHEME) + 1:]))
            return
        if url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)
            return
        if url.scheme() != _RENDER_SCHEME:
            return
        rel = url.path() or url.toString()[len(_RENDER_SCHEME) + 1:]
        if self._last_source:
            text, path = self._last_source
            self._queue(text, path, [rel])

    def _on_status_link(self, href: str):
        if href == _RENDER_ALL_HREF:
            self._render_all()

    def _render_all(self):
        if self._last_source:
            text, path = self._last_source
            self._queue(text, path, None)   # None == render every image

    def _capture_scroll(self):
        """(block number, its offset above the viewport top) for the first
        block currently on screen, or None.

        Anchoring on a BLOCK rather than the raw scrollbar value is what
        makes rendering an image hold position: swapping a one-line
        placeholder for a 240px image changes every pixel offset below it,
        but not the block numbering -- `![alt](x)` and `[Render x](...)` are
        both a single paragraph, so block N stays block N.
        """
        layout = self._view.document().documentLayout()
        top = self._view.verticalScrollBar().value()
        block = self._view.document().begin()
        while block.isValid():
            rect = layout.blockBoundingRect(block)
            if rect.bottom() > top:
                return block.blockNumber(), rect.top() - top
            block = block.next()
        return None

    def _restore_scroll(self, anchor):
        """Put the anchored block back where it was on screen."""
        if anchor is None:
            return
        number, offset = anchor
        block = self._view.document().findBlockByNumber(number)
        if not block.isValid():
            return
        rect = self._view.document().documentLayout().blockBoundingRect(block)
        self._view.verticalScrollBar().setValue(int(round(rect.top() - offset)))

    def _queue(self, source_text: str, src_file: str, images):
        # Rendering an image rebuilds the whole document, which would
        # otherwise snap the reader back to the top. A plain refresh (new
        # file, new tab) genuinely should start at the top, so only an
        # image render captures an anchor.
        self._scroll_anchor = self._capture_scroll() if images != [] else None
        # `images == []` is a plain refresh, which renders nothing and
        # replaces the document wholesale, so there is nothing to mark.
        if images != []:
            self._mark_rendering(images)
        self._pending = (source_text, src_file, images)
        if not self._busy:
            self._start_pending()

    def refresh(self, source_text: str, src_file: str):
        """Rebuild the preview for `source_text`. Safe to call on every
        keystroke: a request arriving mid-build replaces any other waiting
        request rather than queueing behind it."""
        if not src_file:
            self._status.setText("Save the file first — the preview needs its name and folder.")
            return
        # A plain refresh renders nothing: the document appears at once with
        # a placeholder per example, and images are rendered on demand.
        self._last_source = (source_text, src_file)
        self._queue(source_text, src_file, [])

    def _start_pending(self):
        args, self._pending = self._pending, None
        if args is None:
            return
        self._busy = True
        self._status.setText("Building preview…")
        self._worker.request.emit(*args)

    def _on_ready(self, preview):
        from belfryscad.docsgen.preview import markdown_for_qt
        self._busy = False
        self._clear_rendering()

        # Relative image links resolve against the directory the generated
        # .md file would have sat in.
        self._view.setSearchPaths([preview.base_dir])
        self._view.document().setBaseUrl(QUrl.fromLocalFile(preview.base_dir + "/"))
        if preview.markdown:
            md, self._pending_images = placeholder_markdown(
                markdown_for_qt(preview.markdown), preview.base_dir)
            # document().setMarkdown, not the widget's one-argument
            # setMarkdown: the GitHub dialect is what renders the argument
            # tables, and only the document-level call accepts it.
            self._view.document().setMarkdown(
                md, QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
            _apply_rule_color(self._view)
            _set_list_indent(self._view.document())
            _style_headings(self._view.document())
            _style_code_blocks(self._view.document(), self._view.palette())
            _stripe_tables(self._view.document(), self._view.palette())
            _style_placeholders(self._view.document(), self._view.palette())
        else:
            self._pending_images = []
            self._view.setMarkdown(
                "*No documentation blocks found.*\n\n"
                "A documented file starts with a `// LibFile:` or `// File:` "
                "block. See the openscad_docsgen WRITING_DOCS guide.")

        self._show_errors(preview.errors)
        # After the document has been laid out with its final formats, so
        # the anchored block's geometry is the one actually on screen.
        self._restore_scroll(self._scroll_anchor)
        self._scroll_anchor = None
        self._scan_animations()
        self._start_pending()

    def _on_failed(self, message: str):
        self._busy = False
        self._clear_rendering()
        self._anim_timer.stop()
        self._players = []
        self._view.setMarkdown(f"**The preview could not be built.**\n\n    {message}")
        self._show_errors([])
        self._status.setText("Preview failed.")
        self._start_pending()

    # -- "Rendering ..." feedback ---------------------------------------

    def _mark_rendering(self, rels):
        self._rendering = mark_rendering(self._view.document(), rels)
        self._dots = 0
        write_rendering_text(self._view.document(), self._rendering, self._dots)
        if self._rendering:
            self._dots_timer.start()

    def _on_dots_tick(self):
        self._dots = (self._dots + 1) % len(_ELLIPSIS)
        write_rendering_text(self._view.document(), self._rendering, self._dots)

    def _clear_rendering(self):
        self._dots_timer.stop()
        self._rendering = []

    # -- animation -----------------------------------------------------

    def _scan_animations(self):
        """Find the document's animated images and start the clock.

        Images are found in the finished document rather than in the
        markdown, so a still PNG and an APNG can look identical in the
        source -- only the file itself says which it is.
        """
        from belfryscad.png_writer import read_apng_frames

        self._players = []
        doc = self._view.document()
        seen = set()
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fmt = it.fragment().charFormat()
                if fmt.isImageFormat():
                    name = fmt.toImageFormat().name()
                    if name and name not in seen:
                        seen.add(name)
                        url = doc.baseUrl().resolved(QUrl(name))
                        if url.isLocalFile():
                            frames, delay = read_apng_frames(url.toLocalFile())
                            if frames:
                                self._players.append(_ApngPlayer(url, frames, delay))
                it += 1
            block = block.next()

        if self._players:
            self._anim_timer.start()
        else:
            self._anim_timer.stop()

    def _on_anim_tick(self):
        doc = self._view.document()
        changed = False
        for player in self._players:
            frame = player.advance(_ANIM_TICK_MS)
            if frame is not None:
                # addResource keys on the resolved URL, which is what
                # QTextDocument.resource looks the image up by when it
                # paints. Replacing it is enough; the document itself is
                # untouched, so no relayout and no lost scroll position.
                doc.addResource(QTextDocument.ResourceType.ImageResource,
                                player.url, frame)
                changed = True
        if changed:
            self._view.viewport().update()

    # -- errors --------------------------------------------------------

    def _show_errors(self, errors):
        self._errors.clear()
        rows = sorted(errors, key=lambda e: (_LEVEL_ORDER.get(e[3], 9), e[1]))
        for _file, line, msg, level in rows:
            # A multi-line entry (a failed example dumps its whole script)
            # gets its first line as the label and the rest as a tooltip.
            head, _, rest = str(msg).partition("\n")
            item = QTreeWidgetItem([str(line), str(level).capitalize(), head])
            item.setData(0, Qt.ItemDataRole.UserRole, int(line))
            if rest:
                item.setToolTip(2, str(msg))
            self._errors.addTopLevelItem(item)

        counts = {}
        for _f, _l, _m, level in errors:
            counts[level] = counts.get(level, 0) + 1
        if counts:
            self._status.setText(", ".join(
                f"{n} {name}{'s' if n != 1 else ''}"
                for name, n in sorted(counts.items(), key=lambda kv: _LEVEL_ORDER.get(kv[0], 9))))
        else:
            self._status.setText("Documentation is valid.")
        if self._pending_images:
            n = len(self._pending_images)
            plural = "s" if n != 1 else ""
            self._status.setText(
                f"{self._status.text()} &nbsp; {n} image{plural} not rendered — click one, or "
                f'<a href="{_RENDER_ALL_HREF}">render all {n}</a>.')

    def _on_error_activated(self, item: QTreeWidgetItem, _column: int = 0):
        line = item.data(0, Qt.ItemDataRole.UserRole)
        if line:
            self.goto_line.emit(int(line))

    # -- teardown ------------------------------------------------------

    def shutdown(self):
        """Stop the worker thread. Called from MainWindow.closeEvent -- a
        running QThread at interpreter exit aborts the process."""
        self._thread.quit()
        self._thread.wait(5000)
