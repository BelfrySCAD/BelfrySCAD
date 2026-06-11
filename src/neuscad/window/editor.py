import re

from PySide6.QtWidgets import (
    QPlainTextEdit, QWidget, QTextEdit,
    QLineEdit, QPushButton, QLabel, QHBoxLayout, QVBoxLayout,
    QMenu,
)
from PySide6.QtGui import (
    QSyntaxHighlighter, QTextCharFormat, QColor, QFont,
    QPainter, QTextFormat, QPainterPath, QKeySequence, QTextCursor,
    QAction, QFontMetricsF,
)
from PySide6.QtCore import Qt, QRect, QSize, QRegularExpression, QPoint, QEvent, Signal


def _compute_fold_regions(doc) -> dict[int, int]:
    """Scan the document for matching multi-line {…} pairs.
    Returns {open_block_number: close_block_number}."""
    regions: dict[int, int] = {}
    stack: list[int] = []
    block = doc.begin()
    while block.isValid():
        bn = block.blockNumber()
        text = block.text()
        ci = text.find("//")          # strip line comments (simple heuristic)
        if ci >= 0:
            text = text[:ci]
        for ch in text:
            if ch == "{":
                stack.append(bn)
            elif ch == "}" and stack:
                start = stack.pop()
                if start != bn:       # skip single-line {…}
                    regions[start] = bn
        block = block.next()
    return regions


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

        comment_format = QTextCharFormat()
        comment_format.setForeground(QColor("#6A9955"))
        self._rules.append((
            QRegularExpression(r"//[^\n]*"),
            comment_format,
        ))

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
        self._find_bar = FindBar(self)
        self._column_guide = _ColumnGuide(self)

        self._breakpoints: set[int] = set()  # 0-indexed block numbers

        self._fold_regions: dict[int, int] = {}
        self._folded: set[int] = set()
        self._fold_dirty: bool = True
        self._fold_busy: bool = False

        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self.document().contentsChanged.connect(self._on_doc_changed)
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
                    mid_y = top + lh / 2
                    tx = float(bp_w + num_w + 2)
                    tw, th = 8.0, 5.0
                    path = QPainterPath()
                    if block_number in self._folded:
                        path.moveTo(tx,        mid_y - th)
                        path.lineTo(tx + tw,   mid_y)
                        path.lineTo(tx,        mid_y + th)
                    else:
                        path.moveTo(tx,        mid_y - th + 2)
                        path.lineTo(tx + tw,   mid_y - th + 2)
                        path.lineTo(tx + tw/2, mid_y + th - 1)
                    path.closeSubpath()
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor("#555555"))
                    painter.drawPath(path)

            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            block_number += 1

    # ------------------------------------------------------------------
    # Code folding
    # ------------------------------------------------------------------

    def _on_doc_changed(self):
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
        self.document().markContentsDirty(0, self.document().characterCount())
        self.viewport().update()

    def toggle_fold(self, block_number: int):
        if self._fold_dirty:
            self._recompute_fold_regions()
            self._fold_dirty = False
        if block_number not in self._fold_regions:
            return
        end_bn = self._fold_regions[block_number]
        if block_number in self._folded:
            self._folded.discard(block_number)
            self._set_range_visible(block_number, end_bn, True)
        else:
            self._folded.add(block_number)
            self._set_range_visible(block_number, end_bn, False)
        self._update_line_number_area_width()
        self._line_number_area.update()

    def set_indent_size(self, size: int):
        self._indent_size = size
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * size)

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
        if (event.matches(QKeySequence.StandardKey.Undo)
                or event.matches(QKeySequence.StandardKey.Redo)):
            event.ignore()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            cursor = self.textCursor()
            block_text = cursor.block().text()
            indent = len(block_text) - len(block_text.lstrip())
            stripped = block_text.rstrip()
            if stripped.endswith(("{", "[", "(")):
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

    def toggle_breakpoint(self, block_number: int):
        if block_number in self._breakpoints:
            self._breakpoints.discard(block_number)
        else:
            self._breakpoints.add(block_number)
        self._line_number_area.update()
        self.breakpoints_changed.emit(self._breakpoints)

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
        self.setTextCursor(sel.cursor)
        self.ensureCursorVisible()

    def clear_execution_line(self):
        self._exec_selection = []
        self._refresh_extra_selections()

    def clear_selection(self):
        self._selection_extra = []
        self._refresh_extra_selections()

    def _refresh_extra_selections(self):
        self.setExtraSelections(
            self._error_selections + self._selection_extra
            + self._find_selections + self._exec_selection
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

    def contextMenuEvent(self, event):
        cursor = self.cursorForPosition(event.pos())
        cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        word = cursor.selectedText()

        menu = self.createStandardContextMenu()

        if word and re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', word):
            menu.addSeparator()
            act = QAction(f"Go to Definition of '{word}'", self)
            act.triggered.connect(
                lambda checked=False, w=word: self.go_to_definition_requested.emit(w)
            )
            menu.addAction(act)

        menu.exec(event.globalPos())
