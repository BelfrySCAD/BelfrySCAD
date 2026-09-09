"""#393: a second launch hands its file to the running instance.

QLocalServer wants an event loop, so the check runs in a subprocess with a
QCoreApplication -- the same subprocess pattern the GUI tests use, keeping
pytest's own process Qt-free.
"""
import json
import os
import subprocess
import sys

DRIVER = '''
import json, os, subprocess, sys, uuid
from PySide6.QtCore import QCoreApplication, QTimer
app = QCoreApplication([])
from belfryscad.single_instance import InstanceServer, hand_off
name = "belfryscad-test-" + uuid.uuid4().hex[:8]
out = {}
out["handoff_with_nobody_listening"] = hand_off(["a.scad"], name=name, timeout_ms=300)
server = InstanceServer(name=name)
out["listening"] = server.is_listening()
got = []
server.paths_received.connect(lambda paths: got.extend(paths))
# The client is a second PROCESS, as it is for real. In-process, Windows'
# named pipes never complete the write until the server side reads, and
# the server side is this same thread waiting for the write.
client = subprocess.Popen([sys.executable, "-c",
    "import sys; from belfryscad.single_instance import hand_off; "
    "print(hand_off(['rel.scad', '/abs/two.scad'], name=sys.argv[1]))", name],
    stdout=subprocess.PIPE, text=True)
deadline = QTimer(); deadline.setSingleShot(True); deadline.start(10000)
while (not got or client.poll() is None) and deadline.isActive():
    app.processEvents()
client.wait(timeout=10)
out["handoff_with_server"] = client.stdout.read().strip() == "True"
out["received"] = got
out["first_is_absolute"] = os.path.isabs(got[0]) if got else None
# A stale socket from a crashed run must not block a fresh listener.
del server
server2 = InstanceServer(name=name)
out["relisten_after_stale"] = server2.is_listening()
print(json.dumps(out)); sys.stdout.flush()
os._exit(0)
'''


def test_hand_off_reaches_a_listener_and_fails_cleanly_without_one():
    proc = subprocess.run([sys.executable, "-c", DRIVER], capture_output=True, text=True,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["handoff_with_nobody_listening"] is False
    assert out["listening"] is True
    assert out["handoff_with_server"] is True
    assert len(out["received"]) == 2
    assert out["first_is_absolute"] is True
    assert out["received"][0].endswith("rel.scad")
    assert out["received"][1].endswith("two.scad")   # abspath differs per OS (drive letter on Windows)
    assert out["relisten_after_stale"] is True
