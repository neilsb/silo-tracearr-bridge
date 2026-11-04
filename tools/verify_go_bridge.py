"""Run the existing HTTP contracts against the native Go executable."""

import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from urllib.request import urlopen
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from test_bridge import IntegrationTests, NativeHandler, SESSION
import copy


class GoBridgeTests(IntegrationTests):
    def setUp(self):
        self.native = ThreadingHTTPServer(("127.0.0.1", 0), NativeHandler)
        self.native.sessions = [copy.deepcopy(SESSION)]
        self.native.failures, self.native.requests, self.native.writes = {}, [], []
        self.native.bad_shape = self.native.loop_cursor = False
        self.native.artwork_type = "image/webp"
        self.native.controls = self.native.revoke = True
        thread = threading.Thread(target=self.native.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.stop, self.native, thread)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, SILO_URL=f"http://127.0.0.1:{self.native.server_port}",
                   SILO_API_KEY="silo-secret", BRIDGE_API_KEY="bridge-secret", BRIDGE_LISTEN=f"127.0.0.1:{port}")
        binary = ROOT / "plugin/dist/tracearr-bridge-linux-amd64"
        process = subprocess.Popen([str(binary), "serve"], env=env, stdout=subprocess.DEVNULL)
        self.addCleanup(self.stop_process, process)
        self.bridge = SimpleNamespace(server_port=port)
        deadline = time.monotonic() + 5
        while True:
            try:
                with urlopen(f"http://127.0.0.1:{port}/health/live", timeout=.5):
                    break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Go bridge failed to start")
                time.sleep(.02)

    @staticmethod
    def stop_process(process):
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def test_stale_termination_reason_is_not_reused(self):
        # Expiry uses an injected timestamp in Go's own unit test. Here check
        # that a successful stop consumes the reason before a repeated request.
        path = "/Sessions/native-play-42"
        self.request(path + "/Message", method="POST", body={"Text": "First reason"})
        self.request(path + "/Playing/Stop", method="POST")
        self.request(path + "/Playing/Stop", method="POST")
        self.assertEqual(self.native.writes[-1][1], {"reason": "Stream terminated by Tracearr"})


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(GoBridgeTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
