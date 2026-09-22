"""Motor-command and HTTP contract tests; no camera or serial device required."""
import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from server import Controller, Frames, Handler, LEASE, WATCHDOG


class FakeSerial:
    def __init__(self):
        self.commands = []
        self.failed = False

    def write(self, command):
        if self.failed:
            raise OSError("Cable removed")
        self.commands.append(command)
        return len(command)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.now = time.monotonic()
        self.frames = Frames()
        self.frames.write(b"jpeg")
        self.serial = FakeSerial()
        self.robot = Controller(self.frames, self.serial, clock=lambda: self.now)

    def test_starts_idle_and_claim_only_sends_stop(self):
        self.assertEqual(self.robot.status()["command"], "stop")
        self.assertEqual(self.serial.commands, [])
        self.robot.claim()
        self.assertEqual(self.serial.commands, [b"x"])

    def test_only_one_driver_and_stop_revokes_old_token(self):
        token = self.robot.claim()
        with self.assertRaises(ValueError):
            self.robot.claim()
        self.robot.stop_all()
        with self.assertRaises(ValueError):
            self.robot.control(token, 1, "forward", self.now)
        self.assertNotEqual(token, self.robot.claim())

    def test_release_and_out_of_order_motion(self):
        token = self.robot.claim()
        self.robot.control(token, 1, "forward", self.now)
        self.robot.control(token, 3, "stop", self.now)
        with self.assertRaises(ValueError):
            self.robot.control(token, 2, "forward", self.now)
        self.assertEqual(self.serial.commands, [b"x", b"f", b"x"])

    def test_browser_timeout_stops_and_requires_new_claim(self):
        token = self.robot.claim()
        self.robot.control(token, 1, "left", self.now)
        self.now += WATCHDOG + 0.01
        self.robot.tick()
        self.assertEqual(self.serial.commands[-1], b"x")
        self.assertIsNone(self.robot.owner)
        with self.assertRaises(ValueError):
            self.robot.control(token, 2, "left", self.now)

    def test_idle_driver_lease_expires(self):
        self.robot.claim()
        self.now += LEASE + 0.01
        self.robot.tick()
        self.assertIsNone(self.robot.owner)

    def test_old_future_and_invalid_frames_cannot_start_movement(self):
        for timestamp in (self.now - 2, self.now + 1, None, float("nan")):
            with self.subTest(timestamp=timestamp):
                token = self.robot.claim()
                with self.assertRaises(ValueError):
                    self.robot.control(token, 1, "forward", timestamp)
                self.assertEqual(self.serial.commands[-1], b"x")

    def test_camera_failure_stops_even_with_recent_control(self):
        token = self.robot.claim()
        self.robot.control(token, 1, "right", self.now)
        self.frames.timestamp = self.now - 2
        self.robot.tick()
        self.assertEqual(self.robot.command, "stop")
        self.assertIsNone(self.robot.owner)

    def test_serial_failure_revokes_control(self):
        token = self.robot.claim()
        self.serial.failed = True
        with self.assertRaises(ValueError):
            self.robot.control(token, 1, "forward", self.now)
        self.assertIsNone(self.robot.owner)
        self.assertEqual(self.robot.status()["arduino"], "disconnected")
        with self.assertRaises(ValueError):
            self.robot.claim()

    def test_preview_has_no_serial_output(self):
        robot = Controller(self.frames, clock=lambda: self.now)
        robot.control(robot.claim(), 1, "forward", self.now)
        self.assertEqual(robot.status()["mode"], "preview")
        self.assertEqual(robot.command, "forward")

    def test_frame_storage_replaces_instead_of_accumulating(self):
        for _ in range(100):
            self.frames.write(b"next")
        self.assertEqual(self.frames.latest()[0], b"next")
        self.assertEqual(self.frames.number, 101)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frames = Frames()
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.http.controller = Controller(cls.frames)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.http.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def request(self, path, body=None, headers=None):
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                          headers=headers or {})
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read(), response.headers

    def test_no_camera_gives_explicit_error(self):
        self.frames.timestamp = 0
        self.assertEqual(self.request("/api/frame")[0], 503)

    def test_frame_delivery_disables_caching(self):
        self.frames.write(b"jpeg")
        status, data, headers = self.request("/api/frame")
        self.assertEqual((status, data), (200, b"jpeg"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIsNotNone(headers["X-Frame-Time"])

    def test_static_allowlist(self):
        self.assertEqual(self.request("/")[0], 200)
        self.assertEqual(self.request("/../server.py")[0], 404)
        self.assertEqual(self.request("/command/forward")[0], 404)

    def test_cross_origin_commands_rejected(self):
        self.assertEqual(self.request("/api/claim", {})[0], 403)
        self.assertEqual(self.request("/api/claim", {}, {"X-Robot-Control": "1", "Origin": "https://example.org"})[0], 403)

    def test_claim_drive_stop_roundtrip(self):
        headers = {"X-Robot-Control": "1"}
        self.request("/api/stop", {}, headers)
        status, data, _ = self.request("/api/claim", {}, headers)
        self.assertEqual(status, 200)
        token = json.loads(data)["token"]
        self.frames.write(b"jpeg")
        command = {"token": token, "sequence": 1, "command": "forward", "frame_time": self.frames.timestamp}
        self.assertEqual(self.request("/api/control", command, headers)[0], 200)
        self.assertEqual(self.request("/api/stop", {}, headers)[0], 200)
        command["sequence"] = 2
        self.assertEqual(self.request("/api/control", command, headers)[0], 409)


if __name__ == "__main__":
    unittest.main()
