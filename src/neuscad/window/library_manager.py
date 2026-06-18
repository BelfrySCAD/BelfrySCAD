"""Library Manager window — browse, install, upgrade, and remove OpenSCAD libraries."""

import json
import os
import platform
import shutil
import ssl
import tempfile
import threading
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def _make_ssl_context() -> ssl.SSLContext:
    # Try certifi CA bundle first (most reliable on macOS)
    try:
        import certifi
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=certifi.where())
        return ctx
    except Exception:
        pass
    # Try common system CA paths
    for cafile in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(cafile):
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.load_verify_locations(cafile=cafile)
                return ctx
            except Exception:
                pass
    return ssl.create_default_context()


_SSL_CTX = _make_ssl_context()

from PySide6.QtCore import QObject, QSettings, QThread, Qt, Signal, Slot
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

_LIBRARIES_JSON = Path(__file__).parent.parent / "resources" / "libraries.json"
_THUMB_CACHE_DIR = Path.home() / ".cache" / "NeuSCAD" / "thumbnails"


def _library_dir() -> Path:
    system = platform.system()
    if system == "Linux":
        return Path.home() / ".local" / "share" / "OpenSCAD" / "libraries"
    return Path.home() / "Documents" / "OpenSCAD" / "libraries"


def _load_catalog() -> list[dict]:
    with open(_LIBRARIES_JSON, encoding="utf-8") as f:
        return json.load(f)


def _parse_repo(download_url: str) -> tuple[str, str, str]:
    parsed = urlparse(download_url.rstrip("/"))
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"Cannot parse owner/repo from {download_url}")
    return parsed.hostname, parts[0], parts[1]


# ---------------------------------------------------------------------------
# Installed-version tracking via QSettings
# ---------------------------------------------------------------------------

class _InstalledVersions:
    _PREFIX = "libraries/"

    def get(self, name: str) -> str | None:
        s = QSettings("NeuSCAD", "NeuSCAD")
        v = s.value(f"{self._PREFIX}{name}/version")
        return str(v) if v is not None else None

    def set(self, name: str, version: str) -> None:
        s = QSettings("NeuSCAD", "NeuSCAD")
        s.setValue(f"{self._PREFIX}{name}/version", version)

    def remove(self, name: str) -> None:
        s = QSettings("NeuSCAD", "NeuSCAD")
        s.remove(f"{self._PREFIX}{name}")

    def all_installed(self) -> dict[str, str]:
        s = QSettings("NeuSCAD", "NeuSCAD")
        s.beginGroup("libraries")
        result = {}
        for name in s.childGroups():
            v = s.value(f"{name}/version")
            if v is not None:
                result[name] = str(v)
        s.endGroup()
        return result


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class _DownloadWorker(QObject):
    progress = Signal(str, int)
    finished = Signal(str)
    errored = Signal(str)
    done = Signal()

    def __init__(self, lib_entry: dict, target_dir: Path, cancel: threading.Event):
        super().__init__()
        self._lib = lib_entry
        self._target = target_dir
        self._cancel = cancel

    @Slot()
    def run(self):
        try:
            self._do_download()
        except Exception as e:
            self.errored.emit(str(e))
        finally:
            self.done.emit()

    def _do_download(self):
        name = self._lib["name"]
        install_as = self._lib.get("install_as", name)
        version = self._lib.get("version", "")
        branch = self._lib.get("default_branch", "main")

        zip_url = self._build_zip_url(version, branch)
        self.progress.emit(f"Downloading {name}…", 0)

        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        try:
            req = Request(zip_url, headers={"User-Agent": "NeuSCAD-Library-Manager"})
            with urlopen(req, timeout=60, context=_SSL_CTX) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                while True:
                    if self._cancel.is_set():
                        return
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        pct = min(int(downloaded * 70 / total), 70)
                        self.progress.emit(f"Downloading {name}…", pct)
            tmp.close()

            if self._cancel.is_set():
                return

            self.progress.emit(f"Extracting {name}…", 75)
            extract_dir = Path(tempfile.mkdtemp(prefix="neuscad_lib_"))
            with zipfile.ZipFile(tmp.name) as zf:
                zf.extractall(extract_dir)

            root_dirs = [d for d in extract_dir.iterdir() if d.is_dir()]
            source = root_dirs[0] if len(root_dirs) == 1 else extract_dir

            dest = self._target / install_as
            backup = None
            if dest.exists():
                backup = dest.with_suffix(".bak")
                if backup.exists():
                    shutil.rmtree(backup)
                dest.rename(backup)

            self._target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))

            if backup and backup.exists():
                shutil.rmtree(backup)
            shutil.rmtree(extract_dir, ignore_errors=True)

            self.progress.emit(f"Installed {name}", 100)
            self.finished.emit(version)

        except Exception:
            if 'backup' in dir() and backup and backup.exists():
                dest = self._target / install_as
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                backup.rename(dest)
            raise
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def _build_zip_url(self, version: str, branch: str) -> str:
        host, owner, repo = _parse_repo(self._lib["download_url"])
        base = f"https://{host}/{owner}/{repo}"
        if not version:
            return f"{base}/archive/refs/heads/{branch}.zip"
        looks_like_tag = "." in version or version[0] in "vVβ" or not all(c in "0123456789abcdef" for c in version)
        if looks_like_tag:
            return f"{base}/archive/refs/tags/{version}.zip"
        return f"{base}/archive/{version}.zip"


class _UpdateCheckWorker(QObject):
    checked = Signal(str, str)
    status = Signal(str)
    done = Signal()

    def __init__(self, catalog: list[dict], cancel: threading.Event):
        super().__init__()
        self._catalog = catalog
        self._cancel = cancel

    @Slot()
    def run(self):
        try:
            headers_gh = {"Accept": "application/vnd.github.v3+json", "User-Agent": "NeuSCAD"}
            headers_cb = {"Accept": "application/json", "User-Agent": "NeuSCAD"}
            for lib in self._catalog:
                if self._cancel.is_set():
                    break
                name = lib["name"]
                self.status.emit(f"Checking {name}…")
                try:
                    host, owner, repo = _parse_repo(lib["download_url"])
                except ValueError:
                    continue

                version = None
                try:
                    if host == "github.com":
                        version = self._check_github(owner, repo, lib.get("default_branch", "main"), headers_gh)
                    elif host == "codeberg.org":
                        version = self._check_codeberg(owner, repo, lib.get("default_branch", "main"), headers_cb)
                except HTTPError as e:
                    if e.code in (403, 429):
                        self.status.emit("Rate limited — stopping update check")
                        break
                    continue
                except (URLError, OSError):
                    continue

                if version:
                    self.checked.emit(name, version)
        finally:
            self.done.emit()

    def _check_github(self, owner, repo, branch, headers):
        try:
            release = self._api_get(f"https://api.github.com/repos/{owner}/{repo}/releases/latest", headers)
            if release and "tag_name" in release:
                return release["tag_name"]
        except HTTPError:
            pass
        commits = self._api_get(f"https://api.github.com/repos/{owner}/{repo}/commits?per_page=1&sha={branch}", headers)
        if commits:
            return commits[0]["sha"][:7]
        return None

    def _check_codeberg(self, owner, repo, branch, headers):
        try:
            releases = self._api_get(f"https://codeberg.org/api/v1/repos/{owner}/{repo}/releases?limit=1", headers)
            if releases and "tag_name" in releases[0]:
                return releases[0]["tag_name"]
        except (HTTPError, IndexError, KeyError):
            pass
        info = self._api_get(f"https://codeberg.org/api/v1/repos/{owner}/{repo}/branches/{branch}", headers)
        if info and "commit" in info:
            return info["commit"]["id"][:7]
        return None

    @staticmethod
    def _api_get(url, headers):
        req = Request(url, headers=headers)
        with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            return json.loads(resp.read())


class _ThumbnailLoader(QObject):
    loaded = Signal(str, bytes)
    done = Signal()

    def __init__(self, requests: list[tuple[str, str]], cancel: threading.Event):
        super().__init__()
        self._requests = requests
        self._cancel = cancel

    @Slot()
    def run(self):
        try:
            for name, url in self._requests:
                if self._cancel.is_set():
                    break
                cached = _THUMB_CACHE_DIR / f"{name}.dat"
                if cached.exists():
                    self.loaded.emit(name, cached.read_bytes())
                    continue
                try:
                    req = Request(url, headers={"User-Agent": "NeuSCAD"})
                    with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                        data = resp.read()
                    _THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    cached.write_bytes(data)
                    self.loaded.emit(name, data)
                except (HTTPError, URLError, OSError):
                    continue
        finally:
            self.done.emit()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class LibraryManagerWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Library Manager")
        self.setMinimumSize(780, 520)
        self.resize(880, 580)

        self._catalog = _load_catalog()
        self._versions = _InstalledVersions()
        self._remote_versions: dict[str, str] = {}
        self._download_thread: QThread | None = None
        self._download_worker: _DownloadWorker | None = None
        self._download_cancel = threading.Event()
        self._download_lib_name: str = ""
        self._update_thread: QThread | None = None
        self._update_worker: _UpdateCheckWorker | None = None
        self._update_cancel = threading.Event()
        self._thumb_thread: QThread | None = None
        self._thumb_worker: _ThumbnailLoader | None = None
        self._thumb_cancel = threading.Event()

        self._build_ui()
        self._populate_list()
        self._start_thumbnail_load()

    # -- layout ---

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # search bar
        search_bar = QHBoxLayout()
        search_bar.setContentsMargins(8, 8, 8, 4)
        search_lbl = QLabel("Search:")
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Filter libraries…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._filter_list)
        search_bar.addWidget(search_lbl)
        search_bar.addWidget(self._search_edit)
        outer.addLayout(search_bar)

        # splitter: list | detail
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)

        self._list_widget = QListWidget()
        self._list_widget.setMinimumWidth(220)
        self._list_widget.setMaximumWidth(320)
        self._list_widget.currentRowChanged.connect(self._on_selection_changed)
        splitter.addWidget(self._list_widget)

        # right detail pane inside scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._detail_widget = QWidget()
        self._detail_layout = QVBoxLayout(self._detail_widget)
        self._detail_layout.setContentsMargins(16, 12, 16, 12)
        self._detail_layout.setSpacing(8)

        # header row: name + action buttons
        header = QHBoxLayout()
        self._detail_name = QLabel()
        font = QFont()
        font.setPointSize(16)
        font.setBold(True)
        self._detail_name.setFont(font)
        header.addWidget(self._detail_name, 1)

        self._btn_install = QPushButton("Install")
        self._btn_install.clicked.connect(self._on_install)
        self._btn_upgrade = QPushButton("Upgrade")
        self._btn_upgrade.clicked.connect(self._on_upgrade)
        self._btn_uninstall = QPushButton("Uninstall")
        self._btn_uninstall.clicked.connect(self._on_uninstall)
        self._btn_browse = QPushButton("Open in Browser")
        self._btn_browse.clicked.connect(self._on_browse)
        for btn in (self._btn_install, self._btn_upgrade, self._btn_uninstall, self._btn_browse):
            header.addWidget(btn)
        self._detail_layout.addLayout(header)

        # metadata line
        self._detail_meta = QLabel()
        self._detail_meta.setStyleSheet("color: gray;")
        self._detail_layout.addWidget(self._detail_meta)

        # description
        self._detail_desc = QLabel()
        self._detail_desc.setWordWrap(True)
        self._detail_layout.addWidget(self._detail_desc)

        # thumbnail
        self._detail_thumb = QLabel()
        self._detail_thumb.setFixedHeight(200)
        self._detail_thumb.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._detail_layout.addWidget(self._detail_thumb)

        # links
        self._detail_links = QLabel()
        self._detail_links.setWordWrap(True)
        self._detail_links.setOpenExternalLinks(True)
        self._detail_layout.addWidget(self._detail_links)

        # version info
        self._detail_version = QLabel()
        self._detail_layout.addWidget(self._detail_version)

        self._detail_layout.addStretch()
        scroll.setWidget(self._detail_widget)
        splitter.addWidget(scroll)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        outer.addWidget(splitter, 1)

        # bottom status bar
        bottom = QHBoxLayout()
        bottom.setContentsMargins(8, 4, 8, 6)
        self._status_label = QLabel("Ready")
        self._status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bottom.addWidget(self._status_label)

        self._btn_check_updates = QPushButton("Check for Updates")
        self._btn_check_updates.clicked.connect(self._on_check_updates)
        bottom.addWidget(self._btn_check_updates)

        self._progress_bar = QProgressBar()
        self._progress_bar.setFixedWidth(160)
        self._progress_bar.hide()
        bottom.addWidget(self._progress_bar)
        outer.addLayout(bottom)

        # start with empty detail
        self._clear_detail()

    # -- list management ---

    def _populate_list(self):
        lib_dir = _library_dir()
        installed_versions = self._versions.all_installed()

        self._list_widget.clear()
        for lib in self._catalog:
            name = lib["name"]
            install_as = lib.get("install_as", name)

            is_installed = (lib_dir / install_as).is_dir()
            inst_ver = installed_versions.get(name)
            catalog_ver = lib.get("version", "")
            remote_ver = self._remote_versions.get(name)

            latest = remote_ver or catalog_ver
            update_available = is_installed and inst_ver and latest and inst_ver != latest

            suffix = ""
            if update_available:
                suffix = "  ●"
            elif is_installed:
                suffix = "  ✓"

            item = QListWidgetItem(f"{name}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, lib)
            self._list_widget.addItem(item)

    def _filter_list(self, text: str):
        text_lower = text.lower()
        for i in range(self._list_widget.count()):
            item = self._list_widget.item(i)
            lib = item.data(Qt.ItemDataRole.UserRole)
            match = text_lower in lib["name"].lower() or text_lower in lib.get("description", "").lower()
            item.setHidden(not match)

    # -- detail pane ---

    def _clear_detail(self):
        self._detail_name.setText("")
        self._detail_meta.setText("")
        self._detail_desc.setText("")
        self._detail_thumb.clear()
        self._detail_thumb.hide()
        self._detail_links.setText("")
        self._detail_version.setText("")
        for btn in (self._btn_install, self._btn_upgrade, self._btn_uninstall, self._btn_browse):
            btn.hide()

    def _on_selection_changed(self, row: int):
        if row < 0:
            self._clear_detail()
            return
        item = self._list_widget.item(row)
        lib = item.data(Qt.ItemDataRole.UserRole)
        self._show_detail(lib)

    def _show_detail(self, lib: dict):
        name = lib["name"]
        install_as = lib.get("install_as", name)
        lib_dir = _library_dir()

        self._detail_name.setText(name)
        self._detail_meta.setText(f"{lib.get('type', '')}  ·  {lib.get('license', '')}")
        self._detail_desc.setText(lib.get("description", ""))

        # thumbnail
        thumb_path = _THUMB_CACHE_DIR / f"{name}.dat"
        if thumb_path.exists():
            pm = QPixmap()
            pm.loadFromData(thumb_path.read_bytes())
            if not pm.isNull():
                pm = pm.scaledToWidth(min(pm.width(), 400), Qt.TransformationMode.SmoothTransformation)
                self._detail_thumb.setPixmap(pm)
                self._detail_thumb.setFixedHeight(pm.height())
                self._detail_thumb.show()
            else:
                self._detail_thumb.hide()
        else:
            self._detail_thumb.hide()

        # links
        links_parts = []
        doc_url = lib.get("documentation_url")
        if doc_url:
            links_parts.append(f'<a href="{doc_url}">Documentation</a>')
        tutorials = lib.get("tutorials", [])
        if tutorials:
            tuts = ", ".join(f'<a href="{t["url"]}">{t["name"]}</a>' for t in tutorials)
            links_parts.append(f"Tutorials: {tuts}")
        self._detail_links.setText("<br>".join(links_parts))

        # version info
        is_installed = (lib_dir / install_as).is_dir()
        inst_ver = self._versions.get(name)
        catalog_ver = lib.get("version", "")
        remote_ver = self._remote_versions.get(name)
        latest = remote_ver or catalog_ver

        ver_parts = []
        if latest:
            label = "Latest" if remote_ver else "Catalog version"
            ver_parts.append(f"{label}: {latest}")
        if is_installed:
            ver_parts.append(f"Installed: {inst_ver or '(unknown version)'}")
        else:
            ver_parts.append("Not installed")
        self._detail_version.setText("\n".join(ver_parts))

        # buttons
        busy = self._download_thread is not None
        self._btn_browse.show()
        if is_installed:
            self._btn_install.hide()
            update_available = inst_ver and latest and inst_ver != latest
            self._btn_upgrade.setVisible(bool(update_available))
            self._btn_upgrade.setEnabled(not busy)
            self._btn_uninstall.show()
            self._btn_uninstall.setEnabled(not busy)
        else:
            self._btn_install.show()
            self._btn_install.setEnabled(not busy)
            self._btn_upgrade.hide()
            self._btn_uninstall.hide()

    # -- actions ---

    def _selected_lib(self) -> dict | None:
        row = self._list_widget.currentRow()
        if row < 0:
            return None
        return self._list_widget.item(row).data(Qt.ItemDataRole.UserRole)

    def _on_install(self):
        lib = self._selected_lib()
        if lib:
            self._start_download(lib)

    def _on_upgrade(self):
        lib = self._selected_lib()
        if lib:
            self._start_download(lib)

    def _on_uninstall(self):
        lib = self._selected_lib()
        if not lib:
            return
        name = lib["name"]
        install_as = lib.get("install_as", name)
        dest = _library_dir() / install_as
        if not dest.exists():
            return
        reply = QMessageBox.question(
            self, "Uninstall Library",
            f"Remove {name} from {dest}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        shutil.rmtree(dest, ignore_errors=True)
        self._versions.remove(name)
        self._status_label.setText(f"Uninstalled {name}")
        self._populate_list()
        self._show_detail(lib)

    def _on_browse(self):
        lib = self._selected_lib()
        if lib:
            import webbrowser
            webbrowser.open(lib["download_url"])

    # -- download ---

    def _start_download(self, lib: dict):
        if self._download_thread is not None:
            return
        self._download_cancel.clear()
        self._download_lib_name = lib["name"]
        target = _library_dir()

        worker = _DownloadWorker(lib, target, self._download_cancel)
        thread = QThread()
        worker.moveToThread(thread)

        worker.progress.connect(self._on_download_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_download_finished, Qt.ConnectionType.QueuedConnection)
        worker.errored.connect(self._on_download_error, Qt.ConnectionType.QueuedConnection)
        worker.done.connect(self._on_download_done, Qt.ConnectionType.QueuedConnection)
        thread.started.connect(worker.run)

        self._download_thread = thread
        self._download_worker = worker
        self._progress_bar.setValue(0)
        self._progress_bar.show()
        self._set_buttons_busy(True)
        thread.start()

    @Slot(str, int)
    def _on_download_progress(self, msg: str, pct: int):
        self._status_label.setText(msg)
        self._progress_bar.setValue(pct)

    @Slot(str)
    def _on_download_finished(self, version: str):
        name = self._download_lib_name
        self._versions.set(name, version)
        self._status_label.setText(f"Installed {name} {version}")

    @Slot(str)
    def _on_download_error(self, msg: str):
        self._status_label.setText(f"Error: {msg}")
        QMessageBox.warning(self, "Download Error", msg)

    @Slot()
    def _on_download_done(self):
        thread = self._download_thread
        worker = self._download_worker
        self._download_thread = None
        self._download_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(3000)
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self._progress_bar.hide()
        self._set_buttons_busy(False)
        self._populate_list()
        lib = self._selected_lib()
        if lib:
            self._show_detail(lib)

    def _set_buttons_busy(self, busy: bool):
        self._btn_install.setEnabled(not busy)
        self._btn_upgrade.setEnabled(not busy)
        self._btn_uninstall.setEnabled(not busy)

    # -- check for updates ---

    def _on_check_updates(self):
        if self._update_thread is not None:
            return
        self._update_cancel.clear()
        self._btn_check_updates.setEnabled(False)
        self._status_label.setText("Checking for updates…")

        worker = _UpdateCheckWorker(self._catalog, self._update_cancel)
        thread = QThread()
        worker.moveToThread(thread)

        worker.checked.connect(self._on_update_checked, Qt.ConnectionType.QueuedConnection)
        worker.status.connect(self._on_update_status, Qt.ConnectionType.QueuedConnection)
        worker.done.connect(self._on_update_check_done, Qt.ConnectionType.QueuedConnection)
        thread.started.connect(worker.run)

        self._update_thread = thread
        self._update_worker = worker
        thread.start()

    @Slot(str, str)
    def _on_update_checked(self, name: str, version: str):
        self._remote_versions[name] = version

    @Slot(str)
    def _on_update_status(self, msg: str):
        self._status_label.setText(msg)

    @Slot()
    def _on_update_check_done(self):
        thread = self._update_thread
        worker = self._update_worker
        self._update_thread = None
        self._update_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(3000)
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()
        self._btn_check_updates.setEnabled(True)
        self._status_label.setText(f"Update check complete — {len(self._remote_versions)} libraries checked")
        self._populate_list()
        lib = self._selected_lib()
        if lib:
            self._show_detail(lib)

    # -- thumbnail loading ---

    def _start_thumbnail_load(self):
        requests = []
        for lib in self._catalog:
            url = lib.get("thumbnail_url")
            if url:
                requests.append((lib["name"], url))
        if not requests:
            return

        self._thumb_cancel.clear()
        worker = _ThumbnailLoader(requests, self._thumb_cancel)
        thread = QThread()
        worker.moveToThread(thread)

        worker.loaded.connect(self._on_thumbnail_loaded, Qt.ConnectionType.QueuedConnection)
        worker.done.connect(self._on_thumb_done, Qt.ConnectionType.QueuedConnection)
        thread.started.connect(worker.run)

        self._thumb_thread = thread
        self._thumb_worker = worker
        thread.start()

    @Slot(str, bytes)
    def _on_thumbnail_loaded(self, name: str, data: bytes):
        lib = self._selected_lib()
        if lib and lib["name"] == name:
            pm = QPixmap()
            pm.loadFromData(data)
            if not pm.isNull():
                pm = pm.scaledToWidth(min(pm.width(), 400), Qt.TransformationMode.SmoothTransformation)
                self._detail_thumb.setPixmap(pm)
                self._detail_thumb.setFixedHeight(pm.height())
                self._detail_thumb.show()

    @Slot()
    def _on_thumb_done(self):
        thread = self._thumb_thread
        worker = self._thumb_worker
        self._thumb_thread = None
        self._thumb_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(3000)
        if worker is not None:
            worker.deleteLater()
        if thread is not None:
            thread.deleteLater()

    # -- cleanup ---

    def _stop_thread(self, thread: QThread | None):
        if thread is None or not thread.isRunning():
            return
        thread.quit()
        if not thread.wait(5000):
            thread.terminate()
            thread.wait(2000)

    def closeEvent(self, event):
        self._download_cancel.set()
        self._update_cancel.set()
        self._thumb_cancel.set()
        self._stop_thread(self._download_thread)
        self._stop_thread(self._update_thread)
        self._stop_thread(self._thumb_thread)
        super().closeEvent(event)
