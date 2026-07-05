import re

from PySide6.QtWidgets import (
    QPlainTextEdit, QWidget, QTextEdit,
    QLineEdit, QPushButton, QLabel, QHBoxLayout, QVBoxLayout,
    QMenu, QCompleter,
)
from PySide6.QtGui import (
    QSyntaxHighlighter, QTextCharFormat, QColor, QFont,
    QPainter, QTextFormat, QPainterPath, QKeySequence, QTextCursor,
    QAction, QFontMetricsF,
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
                            painter.drawLine(x, round(top), x, round(bot) - 1)
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
        painter.drawLine(x, event.rect().top(), x, event.rect().bottom())
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


class OpenSCADHighlighter(QSyntaxHighlighter):
    def __init__(self, document):
        super().__init__(document)
        self._rules = []

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
            "cube", "sphere", "cylinder", "cone", "polyhedron",
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

        string_format = QTextCharFormat()
        string_format.setForeground(QColor("#CE9178"))
        self._rules.append((
            QRegularExpression(r'"[^"]*"'),
            string_format,
        ))

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

    def highlightBlock(self, text):
        for pattern, fmt in self._rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                match = it.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)

        # Multi-line /* ... */ comments
        self.setCurrentBlockState(0)
        start_idx = 0
        if self.previousBlockState() != 1:
            m = self._block_comment_start.match(text)
            start_idx = m.capturedStart() if m.hasMatch() else -1
        while start_idx >= 0:
            m_end = self._block_comment_end.match(text, start_idx + 2)
            if m_end.hasMatch():
                length = m_end.capturedEnd() - start_idx
            else:
                self.setCurrentBlockState(1)
                length = len(text) - start_idx
            self.setFormat(start_idx, length, self._comment_format)
            m = self._block_comment_start.match(text, start_idx + length)
            start_idx = m.capturedStart() if m.hasMatch() else -1


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

        self._find_input = QLineEdit()
        self._find_input.setPlaceholderText("Find")
        self._find_input.setMinimumWidth(160)
        find_row.addWidget(self._find_input)

        self._match_label = QLabel()
        self._match_label.setMinimumWidth(58)
        self._match_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        find_row.addWidget(self._match_label)

        self._btn_prev = QPushButton("◀")
        self._btn_next = QPushButton("▶")
        self._btn_case = QPushButton("Aa")
        self._btn_case.setCheckable(True)
        self._btn_case.setToolTip("Match Case")
        self._btn_regex = QPushButton(".*")
        self._btn_regex.setCheckable(True)
        self._btn_regex.setToolTip("Use Regular Expression")
        self._btn_close = QPushButton("✕")
        self._btn_close.setToolTip("Close (Escape)")

        for btn in (self._btn_prev, self._btn_next, self._btn_case,
                    self._btn_regex, self._btn_close):
            btn.setFixedSize(22, 22)
            btn.setFlat(True)
            find_row.addWidget(btn)

        outer.addLayout(find_row)

        # --- Replace row (hidden in find-only mode) ---
        self._replace_widget = QWidget()
        replace_row = QHBoxLayout(self._replace_widget)
        replace_row.setContentsMargins(0, 0, 0, 0)
        replace_row.setSpacing(2)

        self._replace_input = QLineEdit()
        self._replace_input.setPlaceholderText("Replace")
        self._replace_input.setMinimumWidth(160)
        replace_row.addWidget(self._replace_input)

        self._btn_replace = QPushButton("Replace")
        self._btn_replace_all = QPushButton("Replace All")
        replace_row.addWidget(self._btn_replace)
        replace_row.addWidget(self._btn_replace_all)
        replace_row.addStretch()

        outer.addWidget(self._replace_widget)
        self._replace_widget.hide()

        # Connections
        self._find_input.textChanged.connect(self._on_search_changed)
        self._find_input.returnPressed.connect(self._find_next)
        self._btn_case.toggled.connect(self._on_search_changed)
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

    def open_find(self):
        self._replace_widget.hide()
        self._show_and_focus()

    def open_replace(self):
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
        c = doc.find(pattern)
        while not c.isNull():
            self._matches.append(c)
            c = doc.find(pattern, c)

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
    print_to_console = Signal(str)             # emits formatted assignment string
    print_value_to_console = Signal(str, object)  # emits (name, value) for viewer-aware logging

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

    def _indent_lines(self):
        cursor = self.textCursor()
        spaces = " " * self._indent_size
        doc = self.document()
        cursor.beginEditBlock()
        if cursor.hasSelection():
            start_bn = doc.findBlock(cursor.selectionStart()).blockNumber()
            end_bn = doc.findBlock(cursor.selectionEnd()).blockNumber()
            end_cur = QTextCursor(doc)
            end_cur.setPosition(cursor.selectionEnd())
            if end_cur.atBlockStart() and end_bn > start_bn:
                end_bn -= 1
            for bn in range(start_bn, end_bn + 1):
                bc = QTextCursor(doc.findBlockByNumber(bn))
                bc.insertText(spaces)
        else:
            bc = QTextCursor(cursor.block())
            bc.insertText(spaces)
        cursor.endEditBlock()

    def _unindent_lines(self):
        cursor = self.textCursor()
        n = self._indent_size
        doc = self.document()
        cursor.beginEditBlock()
        if cursor.hasSelection():
            start_bn = doc.findBlock(cursor.selectionStart()).blockNumber()
            end_bn = doc.findBlock(cursor.selectionEnd()).blockNumber()
            end_cur = QTextCursor(doc)
            end_cur.setPosition(cursor.selectionEnd())
            if end_cur.atBlockStart() and end_bn > start_bn:
                end_bn -= 1
            for bn in range(start_bn, end_bn + 1):
                block = doc.findBlockByNumber(bn)
                text = block.text()
                n_sp = min(n, len(text) - len(text.lstrip()))
                if n_sp > 0:
                    bc = QTextCursor(block)
                    bc.movePosition(bc.MoveOperation.Right, bc.MoveMode.KeepAnchor, n_sp)
                    bc.removeSelectedText()
        else:
            text = cursor.block().text()
            n_sp = min(n, len(text) - len(text.lstrip()))
            if n_sp > 0:
                bc = QTextCursor(cursor.block())
                bc.movePosition(bc.MoveOperation.Right, bc.MoveMode.KeepAnchor, n_sp)
                bc.removeSelectedText()
        cursor.endEditBlock()

    # ------------------------------------------------------------------
    # Code completion
    # ------------------------------------------------------------------

    _BUILTIN_KEYWORDS = {
        "module", "function", "if", "else", "for", "let",
        "each", "true", "false", "undef", "include", "use",
    }
    _BUILTIN_MODULES = {
        "cube", "sphere", "cylinder", "cone", "polyhedron",
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
        x = max(self.line_number_area_width() + 2, self.width() - bar_w - 4)
        bar.setGeometry(x, 2, min(bar_w, self.width() - self.line_number_area_width() - 6), bar_h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), self.line_number_area_width(), cr.height())
        )
        self._reposition_find_bar()
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

        menu.exec(event.globalPos())
