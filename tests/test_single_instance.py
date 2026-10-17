"""#393: a second launch hands its file to the running instance -- over
D-Bus on Linux (#805), a QLocalServer elsewhere. Both backends are checked
wherever they can run: D-Bus inside a private `dbus-run-session`.

QLocalServer wants an event loop, so the check runs in a subprocess with a
QCoreApplication -- the same subprocess pattern the GUI tests use, keeping
pytest's own process Qt-free.
"""
import json
import os
import shutil
import subprocess
import sys

import pytest

DRIVER = '''
import json, os, subprocess, sys, uuid
from PySide6.QtCore import QCoreApplication, QTimer
app = QCoreApplication([])
from belfryscad.single_instance import InstanceServer, hand_off
backend = sys.argv[1]
name = ("com.belfrydw.test.t" if backend == "dbus" else "belfryscad-test-") + uuid.uuid4().hex[:8]
out = {}
if backend == "dbus":
    from PySide6.QtDBus import QDBusConnection
    if not QDBusConnection.sessionBus().isConnected():
        print(json.dumps({"no_bus": True})); sys.stdout.flush(); os._exit(0)
out["handoff_with_nobody_listening"] = hand_off(["a.scad"], name=name, timeout_ms=300, backend=backend)
server = InstanceServer(name=name, backend=backend)
out["listening"] = server.is_listening()
got = []
server.paths_received.connect(lambda paths: got.extend(paths))
# The client is a second PROCESS, as it is for real. In-process, Windows'
# named pipes never complete the write until the server side reads, and
# the server side is this same thread waiting for the write.
client = subprocess.Popen([sys.executable, "-c",
    "import sys; from belfryscad.single_instance import hand_off; "
    "print(hand_off(['rel.scad', '/abs/two.scad'], name=sys.argv[1], backend=sys.argv[2]))",
    name, backend],
    stdout=subprocess.PIPE, text=True)
deadline = QTimer(); deadline.setSingleShot(True); deadline.start(10000)
while (not got or client.poll() is None) and deadline.isActive():
    app.processEvents()
client.wait(timeout=10)
out["handoff_with_server"] = client.stdout.read().strip() == "True"
out["received"] = got
out["first_is_absolute"] = os.path.isabs(got[0]) if got else None
# A stale socket from a crashed run must not block a fresh listener. D-Bus
# has no such state: the bus drops a dead process's name itself.
if backend == "local":
    del server
    server2 = InstanceServer(name=name, backend=backend)
    out["relisten_after_stale"] = server2.is_listening()
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


@pytest.mark.parametrize("backend", ["local", "dbus"])
def test_hand_off_reaches_a_listener_and_fails_cleanly_without_one(backend, tmp_path):
    cmd = [sys.executable, "-c", DRIVER, backend]
    if backend == "dbus":
        # A private bus: never the developer's real session, where a running
        # BelfrySCAD would own the name.
        runner = shutil.which("dbus-run-session")
        if runner is None:
            pytest.skip("no dbus-run-session")
        # Our own config: Homebrew's default session config wants launchd.
        config = tmp_path / "session.conf"
        config.write_text(
            "<busconfig><type>session</type>"
            "<listen>unix:tmpdir=/tmp</listen>"   # tmp_path is too long for a socket on macOS
            # The stock session policy: without the bare allow, the bus
            # rejects even its own replies to the client.
            "<policy context='default'><allow send_destination='*' eavesdrop='true'/>"
            "<allow eavesdrop='true'/><allow own='*'/></policy>"
            "</busconfig>")
        cmd = [runner, f"--config-file={config}", "--"] + cmd
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    if out.get("no_bus"):
        # macOS: QtDBus loads libdbus at runtime, and a Homebrew one of the
        # wrong architecture (or none) leaves it unconnected. CI's Linux job
        # installs dbus so this runs there.
        pytest.skip("QtDBus cannot reach the session bus here")
    assert out["handoff_with_nobody_listening"] is False
    assert out["listening"] is True
    assert out["handoff_with_server"] is True
    assert len(out["received"]) == 2
    assert out["first_is_absolute"] is True
    assert out["received"][0].endswith("rel.scad")
    assert out["received"][1].endswith("two.scad")   # abspath differs per OS (drive letter on Windows)
    if backend == "local":
        assert out["relisten_after_stale"] is True
