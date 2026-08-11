import re

from PySide6.QtWidgets import (
    QPlainTextEdit, QWidget, QTextEdit,
    QLineEdit, QPushButton, QLabel, QHBoxLayout, QVBoxLayout,
    QMenu, QCompleter,
)
from PySide6.QtGui import (
    QSyntaxHighlighter, QTextCharFormat, QColor, QFont,
    QPainter, QTextFormat, QPainterPath, QKeySequence, QTextCursor,
    QAction, QFontMetricsF, QTextDocument, QPixmap, QIcon, QPen,
)
from PySide6.QtCore import Qt, QRect, QSize, QRegularExpression, QPoint, QEvent, Signal, QStringListModel


def _compute_fold_regions(doc) -> dict[int, int]:
    """Scan for foldable regions.  Returns {open_block_number: close_block_number}.

    Two passes:
    1. Explicit delimiter matching — {…} (…) […]; region created only when
       opener and closer are on different lines.
    2. Indentation continuation — any non-empty line followed by at least one
       non-empty line that is strictly more indented.  Covers function bodies,
       ternary chains, nested list comprehensions, etc.  setdefault ensures
       delimiter regions from pass 1 take precedence.
    """
    regions: dict[int, int] = {}
    brace_stack: list[int] = []
    paren_stack: list[int] = []
    bracket_stack: list[int] = []

    # Pass 1: explicit delimiter matching
    block = doc.begin()
    while block.isValid():
        bn = block.blockNumber()
        text = block.text()
        ci = text.find("//")
        if ci >= 0:
            text = text[:ci]
        for ch in text:
            if ch == "{":
                brace_stack.append(bn)
            elif ch == "}" and brace_stack:
                start = brace_stack.pop()
                if start != bn:
                    regions[start] = bn
            elif ch == "(":
                paren_stack.append(bn)
            elif ch == ")" and paren_stack:
                start = paren_stack.pop()
                if start != bn:
                    regions[start] = bn
            elif ch == "[":
                bracket_stack.append(bn)
            elif ch == "]" and bracket_stack:
                start = bracket_stack.pop()
                if start != bn:
                    regions[start] = bn
        block = block.next()

    # Pass 2: indentation-based continuation folds
    block = doc.begin()
    while block.isValid():
        bn = block.blockNumber()
        raw = block.text()
        if raw.strip():
            base_indent = len(raw) - len(raw.lstrip())
            nxt = block.next()
            last_bn = None
            while nxt.isValid():
                ntext = nxt.text()
                if ntext.strip():
                    n_indent = len(ntext) - len(ntext.lstrip())
                    if n_indent <= base_indent:
                        break
                    last_bn = nxt.blockNumber()
                nxt = nxt.next()
            if last_bn is not None and last_bn > bn:
                regions.setdefault(bn, last_bn)
        block = block.next()

    return regions


def _draw_vline_avoiding_cursor(painter: QPainter, x: int, y_top: float, y_bottom: float,
                                 cursor_rect: QRect):
    """Draw a vertical guide line from y_top to y_bottom at column x, minus
    whatever vertical span cursor_rect covers at that x -- both
    _IndentGuides and _ColumnGuide are raised child widgets of the
    viewport, so their solid guide-line pixels paint *over* the text
    cursor (a child can't be told to render behind its own parent's
    content, only reordered among siblings), otherwise fully hiding the
    blinking caret whenever a guide happens to land on top of it."""
    y_top, y_bottom = round(y_top), round(y_bottom)
    # The cursor only carves a notch out of a segment it actually crosses.
    # Testing the column alone left the two branches below drawing from the
    # segment all the way to a caret elsewhere in the file -- a guide on one
    # line reaching down to the caret dozens of lines later.
    if not (cursor_rect.left() <= x <= cursor_rect.right()
            and cursor_rect.top() <= y_bottom and y_top <= cursor_rect.bottom()):
        painter.drawLine(x, y_top, x, y_bottom)
        return
    if y_top < cursor_rect.top():
        painter.drawLine(x, y_top, x, cursor_rect.top())
    if cursor_rect.bottom() < y_bottom:
        painter.drawLine(x, cursor_rect.bottom(), x, y_bottom)


class _IndentGuides(QWidget):
    """Transparent overlay that draws faint vertical lines at each indent level."""

    def __init__(self, editor: 'CodeEditor'):
        super().__init__(editor.viewport())
        self._editor = editor
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(editor.viewport().rect())
        self.raise_()
        editor.document().contentsChanged.connect(self.update)

    def update_geometry(self):
        self.setGeometry(self._editor.viewport().rect())
        self.raise_()
        self.update()

    def paintEvent(self, event):
        editor = self._editor
        indent_size = editor._indent_size
        if indent_size < 1:
            return

        doc_cursor = QTextCursor(editor.document())
        doc_cursor.movePosition(QTextCursor.MoveOperation.Start)
        x0 = editor.cursorRect(doc_cursor).x()
        char_w = QFontMetricsF(editor.font()).horizontalAdvance('0')

        block = editor.firstVisibleBlock()
        geom = editor.blockBoundingGeometry(block).translated(editor.contentOffset())
        top = geom.top()

        painter = QPainter(self)
        painter.setPen(QColor("#E0E0E0"))
        cursor_rect = editor.cursorRect()

        r_top = event.rect().top()
        r_bottom = event.rect().bottom()
        r_left = event.rect().left()
        r_right = event.rect().right()

        while block.isValid() and top <= r_bottom:
            height = editor.blockBoundingRect(block).height()
            bot = top + height

            if bot >= r_top and block.isVisible():
                text = block.text()
                n = len(text) - len(text.lstrip(' '))  # leading spaces
                # Only draw on non-empty indented lines; guides at each indent
                # column strictly inside the indentation (not at the first
                # non-whitespace column itself).
                if text.strip() and n >= indent_size:
                    col = indent_size
                    while col < n:
                        x = round(x0 + col * char_w)
                        if r_left <= x <= r_right + 1:
                            _draw_vline_avoiding_cursor(painter, x, top, bot - 1, cursor_rect)
                        col += indent_size

            block = block.next()
            if not block.isValid():
                break
            top = bot

        painter.end()


class _ColumnGuide(QWidget):
    """Transparent overlay on the viewport that draws a vertical column guide."""

    def __init__(self, editor: 'CodeEditor'):
        super().__init__(editor.viewport())
        self._editor = editor
        self._column: int = 80
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setGeometry(editor.viewport().rect())
        self.raise_()

    def update_geometry(self):
        self.setGeometry(self._editor.viewport().rect())
        self.raise_()
        self.update()

    def set_column(self, column: int):
        self._column = column
        self.update()

    def paintEvent(self, event):
        cursor = QTextCursor(self._editor.document())
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        x0 = self._editor.cursorRect(cursor).x()
        total_w = QFontMetricsF(self._editor.font()).horizontalAdvance('0' * self._column)
        x = round(x0 + total_w)
        if not (event.rect().left() <= x <= event.rect().right() + 1):
            return
        painter = QPainter(self)
        painter.setPen(QColor("#DDDDDD"))
        _draw_vline_avoiding_cursor(painter, x, event.rect().top(), event.rect().bottom(),
                                     self._editor.cursorRect())
        painter.end()


class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self._editor.line_number_area_paint_event(event)

    def mousePressEvent(self, event):
        x = event.position().x()
        y = int(event.position().y())
        fold_x = self.width() - 14
        ed = self._editor

        def _block_at_y(y):
            block = ed.firstVisibleBlock()
            top = round(ed.blockBoundingGeometry(block).translated(ed.contentOffset()).top())
            while block.isValid():
                if block.isVisible():
                    h = round(ed.blockBoundingRect(block).height())
                    if top <= y < top + h:
                        return block
                    top += h
                block = block.next()
            return None

        if x >= fold_x:
            block = _block_at_y(y)
            if block:
                ed.toggle_fold(block.blockNumber())
        elif x < 14:
            block = _block_at_y(y)
            if block:
                ed.toggle_breakpoint(block.blockNumber())


_OPENERS = "([{"
_CLOSERS = ")]}"


def _scan_string(text, i):
    """From just past an opening quote, find where the string ends.

    Returns ``(index just past it, still open at end of line)``.

    Still open only when the line ends on a backslash, which escapes the
    newline and continues the string on the next line -- a real feature of
    the language, and the value carries no break where the source has one.
    A string simply left unclosed ends at the line end instead: the parser
    would take it further, but painting the rest of the file as one string
    over a stray quote is worse than stopping.
    """
    n = len(text)
    j = i
    while j < n:
        if text[j] == "\\":
            if j + 1 >= n:
                return n, True      # the backslash escapes the newline
            j += 2                  # \" and \\ do not end the string
            continue
        if text[j] == '"':
            return j + 1, False
        j += 1
    return n, False


def _scan_line(text, in_comment, in_string):
    """One left-to-right pass over a line for comments, strings and brackets.

    Returns ``(comment_spans, string_spans, brackets, in_comment_out,
    in_string_out)``, where spans are ``(start, length)`` and brackets are
    ``(pos, char)``. The two flags carry into the next line.

    Brackets inside a comment or a string are text, not nesting, and a
    regex pass over the line alone cannot know that -- a ``{`` in a
    ``/* ... */`` that opened three lines up, or in a string continued from
    the line above, must not shift the colours. Both the per-line colouring
    and the whole-document unmatched scan read brackets through here, so
    they cannot disagree about which ones count.

    Strings are reported as spans rather than matched by a regex for the
    same reason: ``"[^"]*"`` cannot cross a line, and stops early on an
    escaped quote.
    """
    spans, strings, brackets = [], [], []
    i, n = 0, len(text)
    if in_string:
        i, in_string = _scan_string(text, 0)
        strings.append((0, i))
    while i < n:
        if in_comment:
            end = text.find("*/", i)
            stop = n if end < 0 else end + 2
            spans.append((i, stop - i))
            in_comment = end < 0
            i = stop
            continue
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            spans.append((i, n - i))
            break
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            spans.append((i, 2))
            in_comment = True
            i += 2
            continue
        if ch == '"':
            end, in_string = _scan_string(text, i + 1)
            strings.append((i, end - i))
            i = end
            continue
        if ch in _OPENERS or ch in _CLOSERS:
            brackets.append((i, ch))
        i += 1
    return spans, strings, brackets, in_comment, in_string


class OpenSCADHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self._rules = []
        # Block number -> column of each opener that is never closed. Filled
        # by _rescan_unmatched(), read back per line in highlightBlock().
        self._unmatched = {}
        self._rescanning = False

        keyword_format = QTextCharFormat()
        keyword_format.setForeground(QColor("#569CD6"))
        keyword_format.setFontWeight(QFont.Weight.Bold)
        keywords = [
            "module", "function", "if", "else", "for", "let",
            "each", "true", "false", "undef", "include", "use",
        ]
        for kw in keywords:
            self._rules.append((
                QRegularExpression(rf"\b{kw}\b"),
                keyword_format,
            ))

        builtin_format = QTextCharFormat()
        builtin_format.setForeground(QColor("#4EC9B0"))
        builtins = [
            "cube", "sphere", "cylinder", "polyhedron",
            "translate", "rotate", "scale", "mirror", "multmatrix",
            "color", "hull", "minkowski", "resize", "offset",
            "union", "difference", "intersection",
            "echo", "assert", "children",
        ]
        for b in builtins:
            self._rules.append((
                QRegularExpression(rf"\b{b}\b"),
                builtin_format,
            ))

        number_format = QTextCharFormat()
        number_format.setForeground(QColor("#5A9E4A"))
        self._rules.append((
            QRegularExpression(r"\b\d+\.?\d*\b"),
            number_format,
        ))

        # Painted from _scan_line's spans, not a regex rule: a string can be
        # continued onto the next line with a backslash, which no per-line
        # pattern can follow, and `"[^"]*"` also stopped at the first
        # escaped quote inside one.
        self._string_format = QTextCharFormat()
        self._string_format.setForeground(QColor("#CE9178"))

        self._comment_format = QTextCharFormat()
        self._comment_format.setForeground(QColor("#6A9955"))
        self._rules.append((
            QRegularExpression(r"//[^\n]*"),
            self._comment_format,
        ))
        self._block_comment_start = QRegularExpression(r"/\*")
        self._block_comment_end = QRegularExpression(r"\*/")

        self._special_var_format = QTextCharFormat()
        self._special_var_format.setForeground(QColor("#C586C0"))
        self._rules.append((
            QRegularExpression(r"\$\w+"),
            self._special_var_format,
        ))

        # Bracket pairs, coloured by nesting depth so a matching pair shares
        # a colour.
        #
        # Each depth bounces to the far side of the spectrum rather than
        # walking along it, by stepping two places at a time round the five
        # hues sorted by angle.
        #
        # The hues also stay clear of the red an unmatched bracket is drawn
        # in (below), so a depth colour can never be mistaken for an error.
        # There is deliberately nothing in the magenta/pink band at all:
        # the rose and orchid that used to sit there measured 30 and 50
        # degrees off red, but hue angle is the wrong test -- a saturated
        # pink reads as hot and red-adjacent however far round the wheel
        # it technically is. Every colour but the goldenrod is now at
        # least 95 degrees away, and goldenrod is dark and yellow-side
        # enough that it has never been the one confused.
        #
        # Vacating a third of the wheel is what stops the ring being an
        # even 72 degrees apart, so adjacent depths are 108 to 163 degrees
        # apart rather than a uniform 144. The tightest pair anywhere in
        # the set is 53 degrees, between two colours that are never
        # adjacent in depth.
        #
        # Saturation and value are tuned per colour for readability. Hue is
        # not free: it carries both the spacing and the distance from red.
        self._bracket_formats = []
        for colour in ("#C4921C",   # dark goldenrod    42
                        "#59D798",   # spring green    150
                        "#C3ACFA",   # violet, pastel  258
                        "#8BCD5C",   # green            95
                        "#54A5DE"):  # blue            205
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(colour))
            self._bracket_formats.append(fmt)

        # An opener with no closer is an error, not a depth. Bold as well as
        # red: rose sits in the depth cycle, and colour alone would leave the
        # two telling apart by hue on a dark background.
        self._unmatched_format = QTextCharFormat()
        self._unmatched_format.setForeground(QColor("#FF2D2D"))
        self._unmatched_format.setFontWeight(QFont.Weight.Bold)

        document.contentsChanged.connect(self._rescan_unmatched)

    _OPENERS = _OPENERS
    _CLOSERS = _CLOSERS

    def _rescan_unmatched(self):
        """Find every opener the document never closes.

        Whether an opener is unmatched is not a property of its own line --
        the closer can be thousands of lines below -- so this walks the
        whole document, on every edit.

        Measured at 15ms on the largest BOSL2 file (skin.scad, 5400 lines),
        which is what buys running it outright rather than debounced: a
        delayed scan leaves the red on a stale line number for as long as
        it waits, so typing above an unclosed bracket would drag the mark
        down the screen a beat behind the cursor.

        ponytail: re-reads every line each time. Cache per-block bracket
        lists and re-scan only edited blocks if a file ever makes this
        felt -- 15ms is inside a frame, and real .scad files are far
        smaller than BOSL2's largest.
        """
        if self._rescanning:  # our own rehighlightBlock() calls come back here
            return
        doc = self.document()
        stack, in_comment, in_string = [], False, False
        block = doc.firstBlock()
        while block.isValid():
            _, _, brackets, in_comment, in_string = _scan_line(
                block.text(), in_comment, in_string)
            number = block.blockNumber()
            for pos, ch in brackets:
                if ch in _OPENERS:
                    stack.append((number, pos))
                elif stack:
                    # Kind is deliberately not checked: `( ]` is a mismatch,
                    # not an unclosed bracket, and calling it one would put
                    # the red on a bracket the user did close.
                    stack.pop()
            block = block.next()

        unmatched = {}
        for number, pos in stack:
            unmatched.setdefault(number, set()).add(pos)
        stale = [n for n in set(unmatched) | set(self._unmatched)
                 if unmatched.get(n) != self._unmatched.get(n)]
        if not stale:
            return
        self._unmatched = unmatched

        # Only the lines whose verdict actually changed, so an edit deep in
        # a large file does not repaint the whole document.
        self._rescanning = True
        try:
            for number in stale:
                block = doc.findBlockByNumber(number)
                if block.isValid():
                    self.rehighlightBlock(block)
        finally:
            self._rescanning = False

    def highlightBlock(self, text):
        for pattern, fmt in self._rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                match = it.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)

        # The block state carries three things across lines: depth in the
        # high bits, "inside a string continued from the line above" in bit
        # 1, "inside a block comment" in bit 0. -1 is Qt's "no previous
        # state" for the first block.
        prev = self.previousBlockState()
        in_comment = prev >= 0 and bool(prev & 1)
        in_string = prev >= 0 and bool(prev & 2)
        depth = (prev >> 2) if prev >= 0 else 0

        spans, strings, brackets, in_comment, in_string = _scan_line(
            text, in_comment, in_string)
        for start, length in spans:
            self.setFormat(start, length, self._comment_format)
        # After the regex rules, so a keyword inside a string does not keep
        # its keyword colour.
        for start, length in strings:
            self.setFormat(start, length, self._string_format)

        unmatched = self._unmatched.get(self.currentBlock().blockNumber(), ())
        for pos, ch in brackets:
            if ch in _OPENERS:
                # An unmatched opener still counts towards the depth, so the
                # brackets that DO pair below it keep the colours they had.
                self.setFormat(pos, 1, self._unmatched_format if pos in unmatched
                               else self._bracket_formats[depth % len(self._bracket_formats)])
                depth += 1
            else:
                # Clamped: a stray closer would otherwise drive the depth
                # negative and recolour everything after it.
                depth = max(0, depth - 1)
                self.setFormat(pos, 1, self._bracket_formats[depth % len(self._bracket_formats)])

        self.setCurrentBlockState(
            (depth << 2) | (2 if in_string else 0) | (1 if in_comment else 0))


class FindBar(QWidget):
    """Floating find/replace overlay, parented to CodeEditor."""

    def __init__(self, editor: 'CodeEditor'):
        super().__init__(editor)
        self._editor = editor
        self._matches: list[QTextCursor] = []
        self._current: int = -1
        self._setup_ui()
        self.hide()

    def _setup_ui(self):
        self.setAutoFillBackground(True)
        pal = self.palette()
        from PySide6.QtGui import QPalette
        pal.setColor(QPalette.ColorRole.Window, QColor("#F3F3F3"))
        self.setPalette(pal)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 4, 6, 4)
        outer.setSpacing(3)

        # --- Find row ---
        find_row = QHBoxLayout()
        find_row.setSpacing(2)

        # Disclosure triangle: collapsed shows Find alone, expanded reveals
        # the Replace row. Sits left of the field, where the thing it
        # expands begins.
        self._btn_disclose = QPushButton("▶")
        self._btn_disclose.setCheckable(True)
        self._btn_disclose.setFlat(True)
        self._btn_disclose.setFixedSize(self._DISCLOSE_W, 22)
        self._btn_disclose.setToolTip("Show Replace")
        # A checkable button paints a pressed-looking background when
        # checked. The arrow already turns from > to v, which says the same
        # thing without the dark slab. The other toggles keep theirs -- for
        # them the fill IS the state readout, since their labels never
        # change.
        self._btn_disclose.setStyleSheet(
            "QPushButton { border: none; background: transparent; }")
        find_row.addWidget(self._btn_disclose)

        self._find_input = QLineEdit()
        self._find_input.setPlaceholderText("Find")
        # 48, not 160: the toggles are fixed-size now, so the fields are the
        # only widgets that can give. A 160 floor made the row wider than a
        # narrow editor could hold, and the layout ran the buttons over the
        # field instead. It still gets every pixel that is spare, since a
        # QLineEdit expands by default.
        self._find_input.setMinimumWidth(48)
        find_row.addWidget(self._find_input)

        self._match_label = QLabel()
        # No reserved width. At 58 it held that gap open between the field
        # and the buttons even with nothing to say, which was most of the
        # time. The field expands, so it absorbs the label growing and
        # shrinking -- the buttons stay put either way.
        self._match_label.setMinimumWidth(0)
        self._match_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        find_row.addWidget(self._match_label)

        self._btn_prev = QPushButton("◀")
        self._btn_next = QPushButton("▶")
        self._btn_case = QPushButton("Aa")
        self._btn_case.setCheckable(True)
        self._btn_case.setToolTip("Match Case")
        self._btn_word = QPushButton()
        self._btn_word.setCheckable(True)
        self._btn_word.setToolTip("Match Whole Word")
        self._refresh_word_icon()
        self._btn_regex = QPushButton(".*")
        self._btn_regex.setCheckable(True)
        self._btn_regex.setToolTip("Use Regular Expression")
        self._btn_close = QPushButton("✕")
        self._btn_close.setToolTip("Close (Escape)")

        # Width from the label, not a flat 22: "Aa" and ".*" are two glyphs
        # and were being clipped at that size, while the single-glyph arrows
        # fit fine. Height stays fixed so the row keeps one baseline.
        fm = self.fontMetrics()
        # Toggles first, then the arrows, then close: the arrows act on the
        # search rather than configuring it, so they belong at the end of
        # the group next to the field's results.
        for btn in (self._btn_case, self._btn_word, self._btn_regex,
                    self._btn_prev, self._btn_next, self._btn_close):
            btn.setFixedSize(max(22, fm.horizontalAdvance(btn.text()) + 12), 22)
            btn.setFlat(True)
            find_row.addWidget(btn)

        outer.addLayout(find_row)

        # --- Replace row (hidden in find-only mode) ---
        self._replace_widget = QWidget()
        replace_row = QHBoxLayout(self._replace_widget)
        replace_row.setContentsMargins(0, 0, 0, 0)
        replace_row.setSpacing(2)
        # Indent past where the disclosure triangle sits in the row above,
        # so the two fields share a left edge instead of stepping.
        replace_row.addSpacing(self._DISCLOSE_W + replace_row.spacing())

        self._replace_input = QLineEdit()
        self._replace_input.setPlaceholderText("Replace")
        self._replace_input.setMinimumWidth(48)
        replace_row.addWidget(self._replace_input)

        # Icons, not text: "Replace" and "Replace All" are wide enough that
        # a narrow editor squeezed them until the labels were cut mid-word
        # ("eplace Al"). A fixed-size icon button cannot be squeezed, so the
        # row stops depending on how much width is left over.
        self._btn_replace = QPushButton()
        self._btn_replace.setToolTip("Replace")
        self._btn_replace_all = QPushButton()
        self._btn_replace_all.setToolTip("Replace All")
        for btn in (self._btn_replace, self._btn_replace_all):
            btn.setFlat(True)
            btn.setFixedSize(24, 22)
        self._refresh_replace_icons()
        replace_row.addWidget(self._btn_replace)
        replace_row.addWidget(self._btn_replace_all)
        replace_row.addStretch()

        outer.addWidget(self._replace_widget)
        self._replace_widget.hide()

        # Connections
        self._find_input.textChanged.connect(self._on_search_changed)
        self._find_input.returnPressed.connect(self._find_next)
        self._btn_case.toggled.connect(self._on_search_changed)
        self._btn_disclose.toggled.connect(self._on_disclose_toggled)
        self._btn_word.toggled.connect(self._on_search_changed)
        self._btn_regex.toggled.connect(self._on_search_changed)
        self._btn_prev.clicked.connect(self._find_prev)
        self._btn_next.clicked.connect(self._find_next)
        self._btn_close.clicked.connect(self.close_bar)
        self._btn_replace.clicked.connect(self._replace_one)
        self._btn_replace_all.clicked.connect(self._replace_all)

        self._editor.document().contentsChanged.connect(self._on_doc_changed)

    # ------------------------------------------------------------------
    # Show / hide
    # ------------------------------------------------------------------

    # 16x16 viewBox. {c} is substituted with the palette's button-text
    # colour -- an SVG has no way to inherit it, and a fixed hex would
    # vanish against a dark background.
    _SVG_REPLACE = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
        '<path d="M2 3.5 H8.5 a3 3 0 0 1 3 3 V8.2" fill="none" stroke="{c}"'
        ' stroke-width="1.3" stroke-linecap="round"/>'
        '<path d="M9.6 6.9 L11.5 8.9 L13.4 6.9" fill="none" stroke="{c}"'
        ' stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>'
        '<rect x="8.3" y="9.6" width="6.4" height="4.2" rx="1"'
        ' fill="none" stroke="{c}" stroke-width="1.3"/>'
        '</svg>'
    )
    # Same arrow, two stacked targets -- "all of them" rather than one.
    _SVG_REPLACE_ALL = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
        '<path d="M1.6 2.6 H8 a3 3 0 0 1 3 3 V6.6" fill="none" stroke="{c}"'
        ' stroke-width="1.2" stroke-linecap="round"/>'
        '<path d="M9.3 5.5 L11 7.2 L12.7 5.5" fill="none" stroke="{c}"'
        ' stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>'
        '<rect x="5.4" y="7.9" width="6.0" height="3.1" rx="0.8"'
        ' fill="none" stroke="{c}" stroke-width="1.2"/>'
        '<rect x="8.4" y="11.0" width="6.0" height="3.1" rx="0.8"'
        ' fill="none" stroke="{c}" stroke-width="1.2"/>'
        '</svg>'
    )

    def _svg_icon(self, svg: str, w: int, h: int) -> QIcon:
        from PySide6.QtSvg import QSvgRenderer
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap(int(w * dpr), int(h * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        QSvgRenderer(svg.format(c=self.palette().buttonText().color().name()).encode()).render(painter)
        painter.end()
        return QIcon(pm)

    def _refresh_replace_icons(self):
        for btn, svg in ((self._btn_replace, self._SVG_REPLACE),
                          (self._btn_replace_all, self._SVG_REPLACE_ALL)):
            btn.setIcon(self._svg_icon(svg, 16, 16))
            btn.setIconSize(QSize(16, 16))

    # Width of the disclosure triangle. The replace row indents by this
    # plus one spacing so its field lines up with the find field.
    _DISCLOSE_W = 18

    def _refresh_word_icon(self):
        """Paint the Match Whole Word icon: 'ab' with a rule beneath it.

        Drawn rather than set as underlined text -- macOS's native button
        style renders the label through the system and drops the font's
        underline attribute, so a QFont with setUnderline(True) reads as
        plain 'ab'. Checking font().underline() only confirms the property
        was stored, not that anything appears.
        """
        w, h = 18, 14
        dpr = self.devicePixelRatioF() or 1.0
        pm = QPixmap(int(w * dpr), int(h * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)

        font = QFont(self.font())
        font.setPointSizeF(max(8.0, self.font().pointSizeF() - 1))
        color = self.palette().buttonText().color()

        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setFont(font)
        painter.setPen(color)
        fm = painter.fontMetrics()
        text = "ab"
        tw = fm.horizontalAdvance(text)
        x = (w - tw) / 2
        baseline = h - 4                      # leave room for the rule below
        painter.drawText(QPoint(int(x), int(baseline)), text)
        painter.setPen(QPen(color, 1.2))
        painter.drawLine(int(x), h - 2, int(x + tw), h - 2)
        painter.end()

        self._btn_word.setIcon(QIcon(pm))
        self._btn_word.setIconSize(QSize(w, h))

    def changeEvent(self, event):
        # Repaint the icon when the palette flips (macOS light/dark), or it
        # keeps the old text colour and can end up invisible.
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "_btn_word"):
            self._refresh_word_icon()
            self._refresh_replace_icons()

    def _on_disclose_toggled(self, expanded: bool):
        self._btn_disclose.setText("▼" if expanded else "▶")
        self._btn_disclose.setToolTip("Hide Replace" if expanded else "Show Replace")
        self._replace_widget.setVisible(expanded)
        # The bar changes height, so it has to be re-measured and re-placed
        # or it overlaps the text or leaves a gap.
        self.adjustSize()
        self._editor._reposition_find_bar()

    def open_find(self):
        self._btn_disclose.setChecked(False)
        self._replace_widget.hide()
        self._show_and_focus()

    def open_replace(self):
        # setChecked drives _on_disclose_toggled, which does the showing --
        # one path, so the arrow can never disagree with what is visible.
        self._btn_disclose.setChecked(True)
        self._replace_widget.show()
        self._show_and_focus()

    def _show_and_focus(self):
        self.adjustSize()
        self.show()
        self._editor._reposition_find_bar()
        self._find_input.setFocus()
        self._find_input.selectAll()
        self._on_search_changed()

    def close_bar(self):
        self.hide()
        self._matches = []
        self._current = -1
        self._editor._find_selections = []
        self._editor._refresh_extra_selections()
        self._editor.setFocus()

    # ------------------------------------------------------------------
    # Key handling
    # ------------------------------------------------------------------

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close_bar()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self._find_prev()
            else:
                self._find_next()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Search logic
    # ------------------------------------------------------------------

    def _make_pattern(self) -> QRegularExpression | None:
        text = self._find_input.text()
        if not text:
            return None
        if not self._btn_regex.isChecked():
            text = QRegularExpression.escape(text)
        if self._btn_word.isChecked():
            # Non-capturing group so the \b anchors bind to the whole
            # pattern rather than to whatever the user's regex starts and
            # ends with -- \bfoo|bar\b would anchor only foo's left and
            # bar's right.
            text = rf"\b(?:{text})\b"
        opts = QRegularExpression.PatternOption(0)
        if not self._btn_case.isChecked():
            opts |= QRegularExpression.PatternOption.CaseInsensitiveOption
        pat = QRegularExpression(text, opts)
        return pat if pat.isValid() else None

    def _on_search_changed(self):
        if self.isHidden():
            return
        pattern = self._make_pattern()
        self._matches = []
        if pattern is None:
            self._match_label.setText("")
            self._find_input.setStyleSheet("")
            self._editor._find_selections = []
            self._editor._refresh_extra_selections()
            return

        doc = self._editor.document()
        # FindCaseSensitively must be passed here, not left to the pattern:
        # QTextDocument.find() forces CaseInsensitiveOption on (or off) the
        # regex to match its own flags, so _make_pattern's option was being
        # overwritten and Match Case never did anything.
        flags = QTextDocument.FindFlag.FindCaseSensitively if self._btn_case.isChecked() \
            else QTextDocument.FindFlag(0)
        c = doc.find(pattern, 0, flags)
        while not c.isNull():
            self._matches.append(c)
            c = doc.find(pattern, c, flags)

        if not self._matches:
            self._match_label.setText("No results")
            self._find_input.setStyleSheet("background: #FFCCCC;")
            self._current = -1
            self._editor._find_selections = []
            self._editor._refresh_extra_selections()
            return

        self._find_input.setStyleSheet("")
        pos = self._editor.textCursor().position()
        self._current = 0
        for i, m in enumerate(self._matches):
            if m.selectionStart() >= pos:
                self._current = i
                break

        self._update_highlights()
        self._scroll_to_current()

    def _on_doc_changed(self):
        if self.isVisible():
            self._on_search_changed()

    def _update_highlights(self):
        sels = []
        for i, m in enumerate(self._matches):
            sel = QTextEdit.ExtraSelection()
            if i == self._current:
                sel.format.setBackground(QColor("#FF9900"))
                sel.format.setForeground(QColor("#FFFFFF"))
            else:
                sel.format.setBackground(QColor("#FFE080"))
            sel.cursor = m
            sels.append(sel)
        self._editor._find_selections = sels
        self._editor._refresh_extra_selections()
        n = len(self._matches)
        self._match_label.setText(f"{self._current + 1} of {n}" if n else "")

    def _scroll_to_current(self):
        if 0 <= self._current < len(self._matches):
            self._editor.setTextCursor(self._matches[self._current])
            self._editor.ensureCursorVisible()

    def _find_next(self):
        if not self._matches:
            return
        self._current = (self._current + 1) % len(self._matches)
        self._update_highlights()
        self._scroll_to_current()

    def _find_prev(self):
        if not self._matches:
            return
        self._current = (self._current - 1) % len(self._matches)
        self._update_highlights()
        self._scroll_to_current()

    # ------------------------------------------------------------------
    # Replace logic
    # ------------------------------------------------------------------

    def _replace_one(self):
        if not self._matches or self._current < 0:
            return
        self._matches[self._current].insertText(self._replace_input.text())
        # _on_doc_changed will re-run the search

    def _replace_all(self):
        if not self._matches:
            return
        replacement = self._replace_input.text()
        undo_cursor = QTextCursor(self._editor.document())
        undo_cursor.beginEditBlock()
        for m in reversed(self._matches):
            m.insertText(replacement)
        undo_cursor.endEditBlock()
        # _on_doc_changed will re-run the search


class CodeEditor(QPlainTextEdit):
    breakpoints_changed = Signal(object)       # emits set[int] of 0-indexed block numbers
    go_to_definition_requested = Signal(str)   # emits the identifier word
    edit_parameter_requested = Signal(str)     # emits a Customizer parameter's name
    print_to_console = Signal(str)             # emits formatted assignment string
    print_value_to_console = Signal(str, object)  # emits (name, value) for viewer-aware logging
    source_edited_externally = Signal()        # emits after an "Edit as..." Save writes back to source
    use_library_requested = Signal()           # emits to open the Use Library picker

    def __init__(self, parent=None):
        super().__init__(parent)
        self._line_number_area = LineNumberArea(self)
        self._highlighter = OpenSCADHighlighter(self.document())

        self.setUndoRedoEnabled(False)

        self._indent_size: int = 4

        font = QFont("Menlo", 13)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)
        self.setTabStopDistance(
            self.fontMetrics().horizontalAdvance(" ") * self._indent_size
        )

        self._error_selections: list = []
        self._selection_extra: list = []
        self._exec_selection: list = []
        self._find_selections: list = []
        self._bracket_selections: list = []
        self._find_bar = FindBar(self)
        self._indent_guides = _IndentGuides(self)
        self._column_guide = _ColumnGuide(self)

        self._completer = QCompleter(self)
        self._completer.setWidget(self)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseSensitive)
        self._completer_model = QStringListModel(self)
        self._completer.setModel(self._completer_model)
        self._completer.activated.connect(self._insert_completion)
        self._user_names: list[str] = []
        self._user_callables: set[str] = set()
        self._user_variables: set[str] = set()
        self._update_completer_words()

        self._debug_locals: dict | None = None
        self._breakpoints: set[int] = set()  # 0-indexed block numbers

        self._fold_regions: dict[int, int] = {}
        self._folded: set[int] = set()
        self._fold_dirty: bool = True
        self._fold_busy: bool = False

        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self.horizontalScrollBar().valueChanged.connect(self._reposition_scroll_overlays)
        self.verticalScrollBar().valueChanged.connect(self._reposition_scroll_overlays)
        self.document().contentsChanged.connect(self._on_doc_changed)
        self.cursorPositionChanged.connect(self._update_bracket_match)
        self._update_line_number_area_width()

    _BP_W = 14  # breakpoint column width (left gutter)

    def line_number_area_width(self):
        digits = max(1, len(str(self.blockCount())))
        return 6 + self._BP_W + self.fontMetrics().horizontalAdvance("9") * digits + 14

    def _update_line_number_area_width(self):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_number_area(self, rect, dy):
        if dy:
            self._line_number_area.scroll(0, dy)
        else:
            self._line_number_area.update(
                0, rect.y(), self._line_number_area.width(), rect.height()
            )
        if rect.contains(self.viewport().rect()):
            self._update_line_number_area_width()

    def line_number_area_paint_event(self, event):
        if self._fold_dirty and not self._fold_busy:
            self._fold_busy = True
            self._recompute_fold_regions()
            self._fold_dirty = False
            self._fold_busy = False

        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), QColor("#CCCCCC"))

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())

        lh = self.fontMetrics().height()
        bp_w = self._BP_W
        num_w = self._line_number_area.width() - 16 - bp_w

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                # Breakpoint dot
                if block_number in self._breakpoints:
                    r = min(bp_w, lh) // 2 - 2
                    if r > 0:
                        cx = bp_w // 2
                        cy = top + lh // 2
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(QColor("#E06C75"))
                        painter.drawEllipse(cx - r, cy - r, r * 2, r * 2)

                # Line number
                painter.setPen(QColor("#000000"))
                painter.drawText(
                    bp_w, top, num_w, lh,
                    Qt.AlignmentFlag.AlignRight,
                    str(block_number + 1),
                )

                if block_number in self._fold_regions:
                    cx = bp_w + num_w + 7
                    cy = int(top) + lh // 2
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor("#606060"))
                    if block_number in self._folded:
                        pts = [QPoint(cx - 3, cy - 4),
                               QPoint(cx + 3, cy),
                               QPoint(cx - 3, cy + 4)]
                    else:
                        pts = [QPoint(cx - 4, cy - 2),
                               QPoint(cx + 4, cy - 2),
                               QPoint(cx,     cy + 3)]
                    painter.drawPolygon(pts)

            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            block_number += 1

    # ------------------------------------------------------------------
    # Code folding
    # ------------------------------------------------------------------

    def _on_doc_changed(self):
        if not self._fold_busy:
            self._fold_dirty = True
        self._line_number_area.update()

    def _recompute_fold_regions(self):
        new_regions = _compute_fold_regions(self.document())
        stale = self._folded - set(new_regions.keys())
        for bn in stale:
            self._set_range_visible(bn, self._fold_regions.get(bn), True)
            self._folded.discard(bn)
        self._fold_regions = new_regions

    def _set_range_visible(self, start_bn: int, end_bn: int | None, visible: bool):
        if end_bn is None:
            return
        block = self.document().findBlockByNumber(start_bn + 1)
        while block.isValid() and block.blockNumber() <= end_bn:
            block.setVisible(visible)
            block = block.next()
        # beginEditBlock/endEditBlock forces Qt to recalculate the document layout,
        # which is required for block visibility changes to take effect visually.
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        cursor.endEditBlock()
        self._update_line_number_area_width()
        self._line_number_area.update()
        self.viewport().update()

    def toggle_fold(self, block_number: int):
        if self._fold_dirty:
            self._recompute_fold_regions()
            self._fold_dirty = False
        if block_number not in self._fold_regions:
            return
        end_bn = self._fold_regions[block_number]
        self._fold_busy = True
        if block_number in self._folded:
            self._folded.discard(block_number)
            self._set_range_visible(block_number, end_bn, True)
        else:
            self._folded.add(block_number)
            self._set_range_visible(block_number, end_bn, False)
        self._fold_busy = False
        self._update_line_number_area_width()
        self._line_number_area.update()

    def set_indent_size(self, size: int):
        self._indent_size = size
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * size)
        self._indent_guides.update()

    def set_error_location(self, line, col):
        fmt = QTextCharFormat()
        fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
        fmt.setUnderlineColor(QColor("#F44747"))
        block = self.document().findBlockByLineNumber(line - 1)
        if not block.isValid():
            return
        cursor_start = block.position() + max(0, col - 1)
        cursor_end = block.position() + block.length() - 1
        sel = QTextEdit.ExtraSelection()
        sel.format = fmt
        sel.cursor = self.textCursor()
        sel.cursor.setPosition(cursor_start)
        sel.cursor.setPosition(cursor_end, sel.cursor.MoveMode.KeepAnchor)
        self._error_selections = [sel]
        self._refresh_extra_selections()

    def clear_errors(self):
        self._error_selections = []
        self._refresh_extra_selections()

    def set_selection(self, start_offset: int, end_offset: int):
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#ADD6FF"))
        sel = QTextEdit.ExtraSelection()
        sel.format = fmt
        sel.cursor = self.textCursor()
        sel.cursor.setPosition(start_offset)
        sel.cursor.setPosition(end_offset, sel.cursor.MoveMode.KeepAnchor)
        self._selection_extra = [sel]
        self._refresh_extra_selections()
        # Scroll to the selected node
        c = self.textCursor()
        c.setPosition(start_offset)
        self.setTextCursor(c)
        self.ensureCursorVisible()

    def event(self, event):
        if event.type() == QEvent.Type.ShortcutOverride:
            if (event.matches(QKeySequence.StandardKey.Undo)
                    or event.matches(QKeySequence.StandardKey.Redo)):
                event.ignore()
                return True
        return super().event(event)

    def keyPressEvent(self, event):
        if self._completer.popup().isVisible():
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
                idx = self._completer.popup().currentIndex()
                if idx.isValid():
                    self._insert_completion(idx.data())
                self._completer.popup().hide()
                return
            if event.key() == Qt.Key.Key_Escape:
                self._completer.popup().hide()
                return
        if (event.matches(QKeySequence.StandardKey.Undo)
                or event.matches(QKeySequence.StandardKey.Redo)):
            event.ignore()
            return
        # Before the Key_Down handling below, which appends a line at the
        # end of the document -- Option-Down on the last line must not do
        # that. Alt has to be tested by bit rather than by equality:
        # macOS reports arrow keys with KeypadModifier set as well.
        mods = event.modifiers()
        if (mods & Qt.KeyboardModifier.AltModifier
                and not mods & (Qt.KeyboardModifier.ShiftModifier
                                | Qt.KeyboardModifier.ControlModifier
                                | Qt.KeyboardModifier.MetaModifier)
                and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down)):
            self._move_lines(-1 if event.key() == Qt.Key.Key_Up else 1)
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            cursor = self.textCursor()
            block_text = cursor.block().text()
            indent = len(block_text) - len(block_text.lstrip())
            stripped = block_text.rstrip()
            first_word = stripped.lstrip().split()[0] if stripped.strip() else ""
            if stripped.endswith(("{", "[", "(")) or first_word in ("function", "module"):
                indent += self._indent_size
            super().keyPressEvent(event)
            self.insertPlainText(" " * indent)
            return
        if event.key() == Qt.Key.Key_Backspace:
            cursor = self.textCursor()
            if not cursor.hasSelection():
                block_text = cursor.block().text()
                pos_in_block = cursor.positionInBlock()
                before_cursor = block_text[:pos_in_block]
                n = self._indent_size
                if before_cursor and not before_cursor.strip() and len(before_cursor) >= n:
                    for _ in range(n):
                        cursor.deletePreviousChar()
                    return
        if event.key() == Qt.Key.Key_Down:
            cursor = self.textCursor()
            if cursor.block() == self.document().lastBlock():
                cursor.movePosition(cursor.MoveOperation.EndOfBlock)
                cursor.insertText("\n")
                self.setTextCursor(cursor)
                return
        if event.key() == Qt.Key.Key_Tab and not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._indent_lines()
            return
        if event.key() == Qt.Key.Key_Backtab:
            self._unindent_lines()
            return
        if event.text() in ('}', ']', ')'):
            cursor = self.textCursor()
            block_text = cursor.block().text()
            n = self._indent_size
            # Only unindent if the line is pure whitespace so far
            if block_text and not block_text.strip() and len(block_text) >= n:
                cursor.movePosition(cursor.MoveOperation.StartOfBlock)
                cursor.movePosition(cursor.MoveOperation.Right,
                                    cursor.MoveMode.KeepAnchor, n)
                cursor.removeSelectedText()
        super().keyPressEvent(event)
        text = event.text()
        if text and (text.isalnum() or text == '_'):
            self._update_completer_popup()
        elif self._completer.popup().isVisible():
            self._completer.popup().hide()

    def replace_span(self, start: int, end: int, new_text: str):
        """Replace document text in [start, end) with new_text. Native Qt
        undo is disabled on this widget (see __init__); this just fires
        document().contentsChanged like any other edit, which MainWindow's
        _on_editor_changed already captures into the app's own undo stack
        (_TextEditCmd) — no extra undo wiring needed here."""
        cursor = QTextCursor(self.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(new_text)

    def _reformat_selection(self, start: int, end: int, selected_text: str):
        """Reformat Selection context-menu action: replace [start, end) with
        format_scad's pretty-printed output, re-based to the selection's own
        current indentation (so reformatting a nested block doesn't yank it
        out to column 0)."""
        from belfryscad.window.scad_format import format_scad
        block = self.document().findBlock(start)
        prefix = block.text()[:start - block.position()]
        base_indent = prefix if prefix.strip() == "" else ""
        formatted = format_scad(selected_text, self._indent_size)
        # split() (not splitlines()) keeps a trailing '' entry when
        # formatted ends in '\n' (the normal case), so the reformatted
        # block's own trailing newline is preserved and doesn't glue its
        # last line to whatever follows the selection.
        lines = formatted.split("\n")
        # The first line continues from whatever indentation the document
        # already has immediately before `start` (untouched -- it's outside
        # the replaced span), so only later lines need base_indent added.
        new_lines = lines[:1] + [base_indent + ln if ln else ln for ln in lines[1:]]
        new_text = "\n".join(new_lines)
        # format_scad always ends non-empty output in '\n'; match the
        # original selection's own trailing-newline-or-not (tolerating
        # trailing indentation *after* that newline, e.g. a selection
        # ending "...;\n  " right before an unselected "}") so we don't
        # introduce a blank line by doubling up with whatever follows.
        had_trailing_newline = bool(re.search(r'\n[ \t]*$', selected_text))
        if not had_trailing_newline and new_text.endswith("\n"):
            new_text = new_text[:-1]
        self.replace_span(start, end, new_text)
        self.source_edited_externally.emit()

    def _selected_block_range(self, cursor):
        """(first, last) block numbers the cursor's selection covers.

        With no selection that is the cursor's own line, twice, so callers
        that treat "the current line" and "the selected lines" alike need
        no branch.

        A selection ending exactly at a block start does not include that
        block: dragging down to the next line's column 0 highlights no text
        on it, and acting on it would touch a line the user cannot see
        selected.
        """
        doc = self.document()
        first = doc.findBlock(cursor.selectionStart()).blockNumber()
        last = doc.findBlock(cursor.selectionEnd()).blockNumber()
        if last > first:
            end_cur = QTextCursor(doc)
            end_cur.setPosition(cursor.selectionEnd())
            if end_cur.atBlockStart():
                last -= 1
        return first, last

    def _indent_lines(self):
        cursor = self.textCursor()
        spaces = " " * self._indent_size
        doc = self.document()
        first, last = self._selected_block_range(cursor)
        cursor.beginEditBlock()
        for bn in range(first, last + 1):
            QTextCursor(doc.findBlockByNumber(bn)).insertText(spaces)
        cursor.endEditBlock()

    def _unindent_lines(self):
        cursor = self.textCursor()
        n = self._indent_size
        doc = self.document()
        first, last = self._selected_block_range(cursor)
        cursor.beginEditBlock()
        for bn in range(first, last + 1):
            block = doc.findBlockByNumber(bn)
            text = block.text()
            n_sp = min(n, len(text) - len(text.lstrip()))
            if n_sp > 0:
                bc = QTextCursor(block)
                bc.movePosition(bc.MoveOperation.Right, bc.MoveMode.KeepAnchor, n_sp)
                bc.removeSelectedText()
        cursor.endEditBlock()

    def _move_lines(self, delta):
        """Move the current line, or every line the selection touches, one
        line up (delta -1) or down (delta +1). Option-Up/Down, as VS Code.

        Done as a single replace_span over the affected lines rather than a
        delete plus an insert, so it lands on the app's undo stack as one
        step -- see replace_span. The lines move verbatim; nothing is
        re-indented, so a move is always exactly reversible by the opposite
        move.
        """
        doc = self.document()
        cursor = self.textCursor()
        first, last = self._selected_block_range(cursor)
        other = first - 1 if delta < 0 else last + 1
        if not 0 <= other < doc.blockCount():
            return  # already against the top or bottom of the document

        moving = "\n".join(doc.findBlockByNumber(bn).text()
                           for bn in range(first, last + 1))
        displaced = doc.findBlockByNumber(other).text()

        span_start = doc.findBlockByNumber(min(first, other)).position()
        end_block = doc.findBlockByNumber(max(last, other))
        # length() counts the block separator; stop before it so the newline
        # after the span survives and the block count cannot change.
        span_end = end_block.position() + end_block.length() - 1

        # Where the cursor sits within its line, to put it back on the same
        # text afterwards rather than at the same absolute offset.
        def offset_of(pos):
            block = doc.findBlock(pos)
            return block.blockNumber(), pos - block.position()

        anchor_bn, anchor_off = offset_of(cursor.anchor())
        pos_bn, pos_off = offset_of(cursor.position())

        self.replace_span(span_start, span_end,
                          moving + "\n" + displaced if delta < 0
                          else displaced + "\n" + moving)

        # Every line the selection touched shifted by exactly delta -- and
        # so did a selection end resting on the block just past the group,
        # which is why this applies to the raw block numbers rather than to
        # the clamped range.
        def restored(bn, off):
            block = doc.findBlockByNumber(min(max(bn + delta, 0), doc.blockCount() - 1))
            return block.position() + min(off, block.length() - 1)

        moved = QTextCursor(doc)
        moved.setPosition(restored(anchor_bn, anchor_off))
        moved.setPosition(restored(pos_bn, pos_off), QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(moved)

    # ------------------------------------------------------------------
    # Code completion
    # ------------------------------------------------------------------

    _BUILTIN_KEYWORDS = {
        "module", "function", "if", "else", "for", "let",
        "each", "true", "false", "undef", "include", "use",
    }
    _BUILTIN_MODULES = {
        "cube", "sphere", "cylinder", "polyhedron",
        "translate", "rotate", "scale", "mirror", "multmatrix",
        "color", "hull", "minkowski", "resize", "offset",
        "union", "difference", "intersection",
        "echo", "assert", "children", "render",
        "circle", "square", "polygon", "text",
        "linear_extrude", "rotate_extrude", "roof", "surface",
        "projection", "import",
    }
    _BUILTIN_FUNCTIONS = {
        "abs", "sign", "ceil", "floor", "round", "sqrt", "ln", "log", "exp",
        "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
        "max", "min", "pow", "norm", "cross", "rands",
        "concat", "len", "str", "chr", "ord",
        "is_undef", "is_bool", "is_num", "is_string", "is_list", "is_function",
        "is_object", "search", "lookup",
        "version", "version_num", "parent_module",
        "object", "textmetrics", "fontmetrics",
    }
    _BUILTIN_CONSTANTS = {"PI"}
    _BUILTIN_DOLLAR_VARS = {
        "$fn", "$fa", "$fs", "$t", "$children", "$parent_modules",
        "$vpt", "$vpr", "$vpd",
    }
    _BUILTIN_WORDS = sorted(
        _BUILTIN_KEYWORDS | _BUILTIN_MODULES | _BUILTIN_FUNCTIONS
        | _BUILTIN_CONSTANTS | _BUILTIN_DOLLAR_VARS
    )
    _BUILTIN_CALLABLES = _BUILTIN_MODULES | _BUILTIN_FUNCTIONS

    def _update_completer_words(self):
        words = sorted(set(self._BUILTIN_WORDS + self._user_names))
        self._completer_model.setStringList(words)

    def update_user_names(self, scope):
        """Extract user-defined names from a root scope and refresh the completer."""
        if scope is None:
            self._user_names = []
            self._user_callables = set()
            self._user_variables = set()
            self._update_completer_words()
            return
        by_attr = {}
        for attr in ('variables', 'functions', 'modules'):
            table = getattr(scope, attr, None)
            by_attr[attr] = set(table.keys()) if isinstance(table, dict) else set()
        names = by_attr['variables'] | by_attr['functions'] | by_attr['modules']
        self._user_names = [n for n in names if n not in self._BUILTIN_WORDS]
        self._user_callables = by_attr['functions'] | by_attr['modules']
        self._user_variables = by_attr['variables']
        self._update_completer_words()

    def _is_callable_completion(self, name: str) -> bool:
        """True if `name` is only known as a function/module — i.e. calling
        it with a trailing '(' isn't ambiguous with a same-named variable."""
        is_callable = name in self._BUILTIN_CALLABLES or name in self._user_callables
        is_variable = name in self._user_variables
        return is_callable and not is_variable

    def _text_under_cursor(self) -> str:
        cursor = self.textCursor()
        block_text = cursor.block().text()
        pos = cursor.positionInBlock()
        start = pos
        while start > 0 and (block_text[start - 1].isalnum() or block_text[start - 1] == '_'):
            start -= 1
        if start > 0 and block_text[start - 1] == '$':
            start -= 1
        return block_text[start:pos]

    def _insert_completion(self, completion: str):
        prefix = self._text_under_cursor()
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.Left, cursor.MoveMode.KeepAnchor, len(prefix))
        if self._is_callable_completion(completion):
            completion += "("
        cursor.insertText(completion)
        self.setTextCursor(cursor)

    def _update_completer_popup(self):
        prefix = self._text_under_cursor()
        if len(prefix) < 2:
            self._completer.popup().hide()
            return
        if prefix != self._completer.completionPrefix():
            self._completer.setCompletionPrefix(prefix)
            self._completer.popup().setCurrentIndex(
                self._completer.completionModel().index(0, 0))
        if self._completer.completionCount() == 0:
            self._completer.popup().hide()
            return
        if (self._completer.completionCount() == 1
                and self._completer.currentCompletion() == prefix):
            self._completer.popup().hide()
            return
        cr = self.cursorRect()
        cr.setWidth(self._completer.popup().sizeHintForColumn(0)
                     + self._completer.popup().verticalScrollBar().sizeHint().width())
        self._completer.complete(cr)

    def toggle_breakpoint(self, block_number: int):
        if block_number in self._breakpoints:
            self._breakpoints.discard(block_number)
        else:
            self._breakpoints.add(block_number)
        self._line_number_area.update()
        self.breakpoints_changed.emit(self._breakpoints)

    def scroll_to_line(self, line: int, margin: int = 5):
        """Scroll so that *line* (1-indexed) is visible with *margin* lines of context."""
        block = self.document().findBlockByLineNumber(line - 1)
        if not block.isValid():
            return
        cursor = self.textCursor()
        cursor.setPosition(block.position())
        self.setTextCursor(cursor)
        first_vis = self.firstVisibleBlock().blockNumber()
        visible = self.viewport().height() // self.fontMetrics().lineSpacing()
        last_vis = first_vis + visible - 1
        target = line - 1  # 0-indexed block number
        if target < first_vis + margin or target > last_vis - margin:
            scroll_to = max(0, target - margin)
            sb = self.verticalScrollBar()
            sb.setValue(scroll_to)

    def set_execution_line(self, line: int):
        """Highlight the currently executing line (1-indexed)."""
        fmt = QTextCharFormat()
        fmt.setBackground(QColor("#FFFF88"))
        fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
        block = self.document().findBlockByLineNumber(line - 1)
        if not block.isValid():
            return
        sel = QTextEdit.ExtraSelection()
        sel.format = fmt
        sel.cursor = self.textCursor()
        sel.cursor.setPosition(block.position())
        sel.cursor.clearSelection()
        self._exec_selection = [sel]
        self._refresh_extra_selections()
        self.scroll_to_line(line)

    def clear_execution_line(self):
        self._exec_selection = []
        self._refresh_extra_selections()

    def clear_selection(self):
        self._selection_extra = []
        self._refresh_extra_selections()

    _OPEN  = "([{"
    _CLOSE = ")]}"
    _MATCH = {"(": ")", "[": "]", "{": "}", ")": "(", "]": "[", "}": "{"}

    def _update_bracket_match(self):
        self._bracket_selections = []
        cur = self.textCursor()
        doc = self.document()
        text = doc.toPlainText()
        pos = cur.position()

        # Cursor on either side of any bracket character; prefer the character before
        bracket_pos = None
        bracket_ch = None
        _all = self._OPEN + self._CLOSE
        if pos > 0 and text[pos - 1] in _all:
            bracket_pos, bracket_ch = pos - 1, text[pos - 1]
        elif pos < len(text) and text[pos] in _all:
            bracket_pos, bracket_ch = pos, text[pos]

        if bracket_pos is None:
            self._refresh_extra_selections()
            return

        # Find matching bracket by counting depth
        ch = bracket_ch
        match_ch = self._MATCH[ch]
        forward = ch in self._OPEN
        depth = 0
        match_pos = None
        i = bracket_pos
        step = 1 if forward else -1
        while 0 <= i < len(text):
            c = text[i]
            if c == ch:
                depth += 1
            elif c == match_ch:
                depth -= 1
                if depth == 0:
                    match_pos = i
                    break
            i += step

        fmt_ok = QTextCharFormat()
        fmt_ok.setBackground(QColor("#adceb7"))  # matched — green tint
        fmt_ok.setForeground(QColor("#102010"))
        fmt_err = QTextCharFormat()
        fmt_err.setBackground(QColor("#7a2020"))  # unmatched — red tint
        fmt_err.setForeground(QColor("#ffffff"))
        fmt = fmt_ok if match_pos is not None else fmt_err

        def make_sel(char_pos):
            sel = QTextEdit.ExtraSelection()
            sel.format = fmt
            c = QTextCursor(doc)
            c.setPosition(char_pos)
            c.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
            sel.cursor = c
            return sel

        self._bracket_selections = [make_sel(bracket_pos)]
        if match_pos is not None:
            self._bracket_selections.append(make_sel(match_pos))
        self._refresh_extra_selections()

    def _refresh_extra_selections(self):
        self.setExtraSelections(
            self._error_selections + self._selection_extra
            + self._find_selections + self._exec_selection + self._bracket_selections
        )

    def _reposition_find_bar(self):
        bar = self._find_bar
        if bar.isHidden():
            return
        bar_w = bar.sizeHint().width()
        bar_h = bar.sizeHint().height()
        avail = self.width() - self.line_number_area_width() - 6
        # Never below minimumSizeHint: squeezing past it does not shrink the
        # children, it overlaps them. Better to run off the right edge of a
        # very narrow editor than to draw the buttons on top of the field.
        width = max(bar.minimumSizeHint().width(), min(bar_w, avail))
        x = max(self.line_number_area_width() + 2, self.width() - width - 4)
        bar.setGeometry(x, 2, width, bar_h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height())
        )
        self._reposition_find_bar()
        self._reposition_scroll_overlays()

    def _reposition_scroll_overlays(self):
        """QPlainTextEdit's internal scroll optimization can shift the
        viewport's child widgets by the scroll delta as a side effect
        (Qt's QWidget::scroll() repositions children to keep them visually
        anchored) -- since _column_guide/_indent_guides each recompute
        their drawn line's x position fresh every paintEvent from
        cursorRect() (already scroll-adjusted), a widget whose own origin
        also drifted from that same scroll would double-apply the offset,
        drawing further from the true column each time you scroll. Forcing
        geometry back to (0, 0)-relative-to-viewport on every scroll (not
        just on resize) keeps both overlays anchored to the viewport
        itself rather than accumulating drift with it."""
        self._indent_guides.update_geometry()
        self._column_guide.update_geometry()

    def show_find(self, replace: bool = False):
        cursor = self.textCursor()
        if cursor.hasSelection():
            sel = cursor.selectedText()
            if ' ' not in sel:  # skip multi-line selections
                self._find_bar._find_input.setText(sel)
        if replace:
            self._find_bar.open_replace()
        else:
            self._find_bar.open_find()

    def set_debug_locals(self, locals_dict: dict | None):
        self._debug_locals = locals_dict

    def contextMenuEvent(self, event):
        cursor = self.cursorForPosition(event.pos())
        cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        word = cursor.selectedText()

        # Qt's WordUnderCursor excludes '$', so right-clicking '$fn' yields 'fn'.
        # Extend to include a leading '$' when one immediately precedes the word.
        if word and cursor.selectionStart() > 0:
            if self.document().characterAt(cursor.selectionStart() - 1) == '$':
                word = '$' + word

        menu = self.createStandardContextMenu()

        # Near the top, and not conditional on what is under the cursor:
        # everything below this point comes and goes with the word clicked,
        # so an item added at the end would sit in a different place each
        # time. Read-only tabs (library files) get nothing to insert with.
        if not self.isReadOnly():
            menu.addSeparator()
            use_act = QAction("Use Library…", self)
            use_act.triggered.connect(
                lambda checked=False: self.use_library_requested.emit())
            menu.addAction(use_act)

        sel_cursor = self.textCursor()
        if not self.isReadOnly() and sel_cursor.hasSelection():
            selected_text = sel_cursor.selectedText().replace(' ', '\n')
            from belfryscad.window.scad_format import can_format
            if can_format(selected_text):
                menu.addSeparator()
                act = QAction("Reformat Selection", self)
                act.triggered.connect(
                    lambda checked=False, s=sel_cursor.selectionStart(),
                    e=sel_cursor.selectionEnd(), t=selected_text:
                        self._reformat_selection(s, e, t)
                )
                menu.addAction(act)

        is_identifier = bool(word and re.match(r'^\$?[A-Za-z_][A-Za-z0-9_]*$', word))

        if is_identifier and self._debug_locals is not None and word in self._debug_locals:
            value = self._debug_locals[word]
            from belfryscad.window.debugger import _pretty_assignment, _fmt
            preview = _fmt(value)
            if len(preview) > 30:
                preview = preview[:30] + "…"
            name_act = QAction(f"Variable: {word}", self)
            name_act.setEnabled(False)
            preview_act = QAction(f"Value: {preview}", self)
            preview_act.setEnabled(False)
            first = menu.actions()[0] if menu.actions() else None
            menu.insertAction(first, preview_act)
            menu.insertAction(preview_act, name_act)
            menu.insertSeparator(first)
            menu.addSeparator()
            menu.addAction(
                f"Print '{word}' to Console",
                lambda v=value, n=word: self.print_value_to_console.emit(n, v)
            )
            from belfryscad.window.data_viewers import build_viewer_menu
            view_sub = QMenu(f"View '{word}' as...", self)
            build_viewer_menu(view_sub, word, value, self)
            if not view_sub.isEmpty():
                menu.addMenu(view_sub)

        if is_identifier:
            menu.addSeparator()
            act = QAction(f"Go to Definition of '{word}'", self)
            act.triggered.connect(
                lambda checked=False, w=word: self.go_to_definition_requested.emit(w)
            )
            menu.addAction(act)

            # Works from any use of the name, not just its declaration line
            # -- scan_parameters is keyed by name, same as Go to Definition
            # above, so right-clicking a reference is just as good as
            # right-clicking the assignment itself.
            from belfryscad.window.customizer import scan_parameters
            if not self.isReadOnly() and word in {p.name for p in scan_parameters(self.toPlainText())}:
                edit_act = QAction(f"Edit Parameter '{word}'…", self)
                edit_act.triggered.connect(
                    lambda checked=False, w=word: self.edit_parameter_requested.emit(w)
                )
                menu.addAction(edit_act)

        # Lexical "View as..."/"Edit as..." for a plain numeric literal under
        # the cursor — independent of debug session state, so these work even
        # when not debugging (unlike the debug-locals "View 'word' as..."
        # block above, which is keyed by identifier name).
        from belfryscad.window.data_viewers import (
            find_viewable_literals, find_editable_literals,
            build_lexical_view_menu, build_editor_menu,
        )
        text = self.toPlainText()
        offset = self.cursorForPosition(event.pos()).position()

        view_literals = find_viewable_literals(text, offset)
        edit_literals = find_editable_literals(text, offset) if not self.isReadOnly() else {}

        if view_literals or edit_literals:
            last = menu.actions()[-1] if menu.actions() else None
            if last is not None and not last.isSeparator():
                menu.addSeparator()

        if view_literals:
            view_sub = QMenu("View as...", self)
            build_lexical_view_menu(view_sub, text, view_literals, self)
            if not view_sub.isEmpty():
                menu.addMenu(view_sub)

        if edit_literals:
            def on_commit(new_text, start, end):
                self.replace_span(start, end, new_text)
                self.source_edited_externally.emit()

            edit_sub = QMenu("Edit as...", self)
            build_editor_menu(edit_sub, text, edit_literals, on_commit, self)
            if not edit_sub.isEmpty():
                menu.addMenu(edit_sub)

        menu.exec(event.globalPos())
