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

from PySide6.QtCore import QObject, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QPushButton,
                                QSplitter, QTextBrowser, QTreeWidget,
                                QTreeWidgetItem, QVBoxLayout, QWidget)

_LEVEL_ORDER = {"error": 0, "warning": 1, "notice": 2}


class _DocsWorker(QObject):
    """Lives on the pane's worker thread; owns every docsgen call."""
    ready = Signal(object)      # belfryscad.docsgen.preview.DocsPreview
    failed = Signal(str)

    request = Signal(str, str, bool)   # source_text, src_file, gen_images

    def __init__(self):
        super().__init__()
        self.request.connect(self._build, Qt.ConnectionType.QueuedConnection)

    def _build(self, source_text: str, src_file: str, gen_images: bool):
        from belfryscad.docsgen.preview import build_preview
        try:
            self.ready.emit(build_preview(source_text, src_file, gen_images=gen_images))
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
        self._images_box = QCheckBox("Render examples")
        self._images_box.setChecked(True)
        self._images_box.setToolTip(
            "Run each Example and Figure and show its image. Turn this off "
            "for a faster text-only check of the documentation.")
        self._images_box.toggled.connect(self.refresh_requested)

        top = QHBoxLayout()
        top.setContentsMargins(4, 4, 4, 0)
        top.addWidget(self._refresh_btn)
        top.addWidget(self._images_box)
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

    # -- driving -------------------------------------------------------

    def refresh(self, source_text: str, src_file: str):
        """Rebuild the preview for `source_text`. Safe to call on every
        keystroke: a request arriving mid-build replaces any other waiting
        request rather than queueing behind it."""
        if not src_file:
            self._status.setText("Save the file first — the preview needs its name and folder.")
            return
        self._pending = (source_text, src_file, self._images_box.isChecked())
        if not self._busy:
            self._start_pending()

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

        # Relative image links resolve against the directory the generated
        # .md file would have sat in.
        self._view.setSearchPaths([preview.base_dir])
        self._view.document().setBaseUrl(QUrl.fromLocalFile(preview.base_dir + "/"))
        if preview.markdown:
            # document().setMarkdown, not the widget's one-argument
            # setMarkdown: the GitHub dialect is what renders the argument
            # tables, and only the document-level call accepts it.
            self._view.document().setMarkdown(
                markdown_for_qt(preview.markdown),
                QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
        else:
            self._view.setMarkdown(
                "*No documentation blocks found.*\n\n"
                "A documented file starts with a `// LibFile:` or `// File:` "
                "block. See the openscad_docsgen WRITING_DOCS guide.")

        self._show_errors(preview.errors)
        self._start_pending()

    def _on_failed(self, message: str):
        self._busy = False
        self._view.setMarkdown(f"**The preview could not be built.**\n\n    {message}")
        self._show_errors([])
        self._status.setText("Preview failed.")
        self._start_pending()

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
