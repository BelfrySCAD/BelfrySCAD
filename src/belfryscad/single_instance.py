"""Open a file in the BelfrySCAD that is already running, not a new one (#393).

A second launch with a file on its command line -- what a double-click in a
file manager is -- tries to hand the path to a running instance over a
QLocalServer named for the current user. If something is listening, the
path goes over and this process exits before it builds a window; if not,
this process becomes the listener. macOS already delivers a Finder
double-click to the running app as a FileOpen event, so this mostly
matters on Windows and Linux, but the server runs everywhere so the
behaviour is the same wherever the launch came from.

Wire protocol: one absolute path per line, UTF-8, then the client
disconnects. Nothing comes back.
"""
from __future__ import annotations

import getpass
import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def server_name() -> str:
    # Per user: two people logged into one machine must not share a window.
    return f"BelfrySCAD-{getpass.getuser()}"


def hand_off(paths: list[str], name: str | None = None, timeout_ms: int = 1500) -> bool:
    """Send `paths` to a running instance. True if one took them."""
    sock = QLocalSocket()
    sock.connectToServer(name or server_name())
    if not sock.waitForConnected(timeout_ms):
        return False
    payload = "".join(os.path.abspath(p) + "\n" for p in paths).encode("utf-8")
    sock.write(payload)
    ok = sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        sock.waitForDisconnected(timeout_ms)
    return ok


class InstanceServer(QObject):
    """The listening side. Emits `paths_received` with a list of absolute paths."""

    paths_received = Signal(list)

    def __init__(self, name: str | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self._name = name or server_name()
        self._server = QLocalServer(self)
        # Only this user may connect: anyone else on the machine could
        # otherwise make the app open files of their choosing.
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        # A crashed instance leaves its socket file behind on Unix, and
        # listen() refuses to reuse it. Nobody is listening (hand_off just
        # failed to connect), so it is safe to clear.
        QLocalServer.removeServer(self._name)
        self._listening = self._server.listen(self._name)
        self._server.newConnection.connect(self._accept)

    def is_listening(self) -> bool:
        return self._listening

    def _accept(self):
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            buf = bytearray()

            def on_ready(s=sock, b=buf):
                b += bytes(s.readAll())

            def on_done(s=sock, b=buf):
                on_ready(s, b)
                paths = [line for line in b.decode("utf-8", "replace").split("\n") if line.strip()]
                s.deleteLater()
                if paths:
                    self.paths_received.emit(paths)

            sock.readyRead.connect(on_ready)
            sock.disconnected.connect(on_done)
