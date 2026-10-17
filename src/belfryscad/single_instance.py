"""Open a file in the BelfrySCAD that is already running, not a new one (#393).

A second launch with a file on its command line -- what a double-click in a
file manager is -- tries to hand the path to a running instance. If one takes
it, this process exits before it builds a window; if not, this process
becomes the one listening. macOS already delivers a Finder double-click to
the running app as a FileOpen event, so this mostly matters on Windows and
Linux, but the listener runs everywhere so the behaviour is the same wherever
the launch came from.

Two transports, one interface:

* **Linux: D-Bus.** The app owns its ID on the session bus and exports the
  standard `org.freedesktop.Application` interface; a second launch calls
  its `Open` with file URIs. It is how Linux desktops do this, it needs no
  file in /tmp that two Flatpak sandboxes might not share, and QtDBus needs
  only libdbus. QtNetwork links libgssapi_krb5, which the Flatpak runtime
  does not ship, so importing it killed the Flatpak at startup (#805).
* **Elsewhere: a QLocalServer** named for the current user. Wire protocol:
  one absolute path per line, UTF-8, then the client disconnects. Nothing
  comes back.

Each backend imports its Qt module only when used.
"""
from __future__ import annotations

import getpass
import os
import sys

from PySide6.QtCore import ClassInfo, QObject, QUrl, Signal, Slot

APP_ID = "com.belfrydw.belfryscad"
_DBUS_INTERFACE = "org.freedesktop.Application"


def default_backend() -> str:
    return "dbus" if sys.platform.startswith("linux") else "local"


def server_name(backend: str | None = None) -> str:
    if (backend or default_backend()) == "dbus":
        return APP_ID   # the session bus is already per user
    # Per user: two people logged into one machine must not share a window.
    return f"BelfrySCAD-{getpass.getuser()}"


def _object_path(name: str) -> str:
    return "/" + name.replace(".", "/")


def hand_off(paths: list[str], name: str | None = None, timeout_ms: int = 1500,
             backend: str | None = None) -> bool:
    """Send `paths` to a running instance. True if one took them."""
    backend = backend or default_backend()
    name = name or server_name(backend)
    paths = [os.path.abspath(p) for p in paths]
    if backend == "dbus":
        return _dbus_hand_off(paths, name, timeout_ms)
    return _local_hand_off(paths, name, timeout_ms)


def _dbus_hand_off(paths, name, timeout_ms) -> bool:
    from PySide6.QtDBus import QDBus, QDBusConnection, QDBusMessage
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected() or not bus.interface().isServiceRegistered(name).value():
        return False
    msg = QDBusMessage.createMethodCall(name, _object_path(name), _DBUS_INTERFACE, "Open")
    msg.setArguments([[QUrl.fromLocalFile(p).toString() for p in paths], {}])
    reply = bus.call(msg, QDBus.CallMode.Block, timeout_ms)
    return reply.type() == QDBusMessage.MessageType.ReplyMessage


def _local_hand_off(paths, name, timeout_ms) -> bool:
    from PySide6.QtNetwork import QLocalSocket
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write("".join(p + "\n" for p in paths).encode("utf-8"))
    ok = sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
        sock.waitForDisconnected(timeout_ms)
    return ok


@ClassInfo({"D-Bus Interface": _DBUS_INTERFACE})
class _ApplicationAdaptor(QObject):
    """`org.freedesktop.Application`, as a desktop's file manager or a
    second launch calls it. `Activate` with no files just raises the window."""

    def __init__(self, server: InstanceServer):
        super().__init__(server)
        self._server = server

    @Slot("QVariantMap")
    def Activate(self, platform_data):
        self._server.paths_received.emit([])

    @Slot("QStringList", "QVariantMap")
    def Open(self, uris, platform_data):
        paths = [QUrl(u).toLocalFile() for u in uris]
        self._server.paths_received.emit([p for p in paths if p])

    @Slot(str, "QVariantList", "QVariantMap")
    def ActivateAction(self, action, parameter, platform_data):
        pass


class InstanceServer(QObject):
    """The listening side. Emits `paths_received` with a list of absolute
    paths -- empty when the instance should only come to the front."""

    paths_received = Signal(list)

    def __init__(self, name: str | None = None, parent: QObject | None = None,
                 backend: str | None = None):
        super().__init__(parent)
        backend = backend or default_backend()
        self._name = name or server_name(backend)
        if backend == "dbus":
            self._listening = self._listen_dbus()
        else:
            self._listening = self._listen_local()

    def is_listening(self) -> bool:
        return self._listening

    def _listen_dbus(self) -> bool:
        from PySide6.QtDBus import QDBusConnection
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return False
        self._adaptor = _ApplicationAdaptor(self)
        if not bus.registerObject(_object_path(self._name), self._adaptor,
                                  QDBusConnection.RegisterOption.ExportAllSlots):
            return False
        # The bus releases the name when the process exits, crash or not,
        # so there is no stale state to clear as there is with a socket.
        return bus.registerService(self._name)

    def _listen_local(self) -> bool:
        from PySide6.QtNetwork import QLocalServer
        self._server = QLocalServer(self)
        # Only this user may connect: anyone else on the machine could
        # otherwise make the app open files of their choosing.
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        # A crashed instance leaves its socket file behind on Unix, and
        # listen() refuses to reuse it. Nobody is listening (hand_off just
        # failed to connect), so it is safe to clear.
        QLocalServer.removeServer(self._name)
        self._server.newConnection.connect(self._accept)
        return self._server.listen(self._name)

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
