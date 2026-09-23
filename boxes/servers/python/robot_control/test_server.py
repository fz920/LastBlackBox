"""Motor-command and HTTP contract tests; no camera or serial device required."""
import json
import threading
import time
import unittest
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from server import Controller, Frames, Handler, LEASE, WATCHDOG, FULL_SPEED_LIMIT, DEFAULT_CALIBRATION


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
        self.frames.timestamp = self.now
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
        self.assertEqual(self.serial.commands, [b"x", b"@664E\n", b"x"])

    def test_browser_timeout_stops_and_requires_new_claim(self):
        token = self.robot.claim()
        self.robot.control(token, 1, "left", self.now)
        self.now += WATCHDOG + 0.01
        self.robot.tick()
        self.assertEqual(self.serial.commands[-1], b"x")
        self.assertIsNone(self.robot.owner)
        with self.assertRaises(ValueError):
            self.robot.control(token, 2, "left", self.now)

    def test_steering_sequence_keeps_remaining_direction_then_stops(self):
        token = self.robot.claim()
        for sequence, command in enumerate(["forward", "forward_right", "forward", "stop"], 1):
            self.robot.control(token, sequence, command, self.now)
        self.assertEqual(self.serial.commands, [b"x", b"@664E\n", b"@6654\n", b"@664E\n", b"x"])

    def test_each_curve_uses_its_protocol_byte_and_times_out(self):
        for command, byte in [("forward_left", b"@604E\n"), ("forward_right", b"@6654\n"),
                              ("backward_left", b"@5466\n"), ("backward_right", b"@4E60\n")]:
            with self.subTest(command=command):
                self.frames.timestamp = self.now
                token = self.robot.claim()
                self.robot.control(token, 1, command, self.now)
                self.assertEqual(self.serial.commands[-1], byte)
                self.now += WATCHDOG + 0.01
                self.robot.tick()
                self.assertEqual(self.serial.commands[-1], b"x")
                self.assertIsNone(self.robot.owner)

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

    def test_full_speed_is_explicit_and_release_restores_slow_default(self):
        with self.assertRaises(ValueError):
            self.robot.claim("invalid")
        token = self.robot.claim("full")
        self.robot.control(token, 1, "forward", self.frames.timestamp)
        self.assertEqual(self.serial.commands, [b"x", b"B"])
        self.assertEqual(self.robot.status()["speed"], "full")
        self.robot.stop_all()
        self.assertEqual(self.serial.commands[-1], b"x")
        self.assertEqual(self.robot.status()["speed"], "slow")
        token = self.robot.claim()
        self.robot.control(token, 1, "forward", self.frames.timestamp)
        self.assertEqual(self.serial.commands[-1], b"@664E\n")

    def test_full_speed_limit_survives_heartbeats_and_direction_changes(self):
        token = self.robot.claim("full")
        start = self.now
        for sequence in range(10):
            self.now = start + sequence * 0.2
            self.frames.write(b"jpeg")
            self.frames.timestamp = self.now
            self.robot.control(token, sequence, "forward" if sequence % 2 else "backward", self.now)
        self.now = start + FULL_SPEED_LIMIT
        self.robot.tick()
        status = self.robot.status()
        self.assertEqual(status["command"], "stop")
        self.assertFalse(status["busy"])
        self.assertEqual(status["speed"], "slow")
        self.assertIn("test finished", status["reason"])
        with self.assertRaises(ValueError):
            self.robot.control(token, 11, "forward", self.now)

    def test_full_speed_disconnect_and_stale_video_still_stop(self):
        for failure in ("timeout", "video"):
            with self.subTest(failure=failure):
                self.frames.timestamp = self.now
                token = self.robot.claim("full")
                self.robot.control(token, 1, "forward", self.now)
                if failure == "timeout":
                    self.now += WATCHDOG + 0.01
                else:
                    self.frames.timestamp = self.now - 2
                self.robot.tick()
                self.assertEqual(self.serial.commands[-1], b"x")
                self.assertIsNone(self.robot.owner)


    def test_calibration_changes_only_selected_wheel_and_travel_direction(self):
        self.robot.set_calibration({**DEFAULT_CALIBRATION, "left_forward": 11, "right_backward": 10})
        token = self.robot.claim()
        self.robot.control(token, 1, "forward", self.now)
        self.assertEqual(self.serial.commands[-1], b"@654E\n")
        self.robot.control(token, 2, "backward", self.now)
        self.assertEqual(self.serial.commands[-1], b"@4E64\n")
        self.robot.control(token, 3, "stop", self.now)
        self.assertEqual(self.serial.commands[-1], b"x")
        self.robot.stop_all()
        token = self.robot.claim("full")
        self.robot.control(token, 1, "forward", self.now)
        self.assertEqual(self.serial.commands[-1], b"B")

    def test_calibration_requires_stopped_unowned_robot_and_valid_values(self):
        self.robot.claim()
        with self.assertRaises(ValueError):
            self.robot.set_calibration(DEFAULT_CALIBRATION)
        self.robot.stop_all()
        for value in (5, 25, 12.5, True, "12", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.robot.set_calibration({**DEFAULT_CALIBRATION, "left_forward": value})
        with self.assertRaises(ValueError):
            self.robot.set_calibration({"left_forward": 12})
        self.assertEqual(self.robot.calibration, DEFAULT_CALIBRATION)

    def test_calibration_persists_without_moving_wheels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            robot = Controller(self.frames, self.serial, calibration_path=path)
            values = {**DEFAULT_CALIBRATION, "left_forward": 11}
            robot.set_calibration(values)
            self.assertEqual(self.serial.commands, [b"x"])
            restored = Controller(self.frames, calibration_path=path)
            self.assertEqual(restored.status()["calibration"], values)

    def test_partial_calibrated_serial_write_revokes_control(self):
        token = self.robot.claim()
        self.serial.write = lambda data: len(data) - 1
        with self.assertRaises(ValueError):
            self.robot.control(token, 1, "forward", self.now)
        self.assertFalse(self.robot.status()["busy"])
        self.assertEqual(self.robot.status()["command"], "stop")


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
        self.assertEqual(self.request("/steering.js")[0], 200)
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
        command = {"token": token, "sequence": 1, "command": "forward_right", "frame_time": self.frames.timestamp}
        self.assertEqual(self.request("/api/control", command, headers)[0], 200)
        self.assertEqual(self.request("/api/stop", {}, headers)[0], 200)
        command["sequence"] = 2
        self.assertEqual(self.request("/api/control", command, headers)[0], 409)

    def test_full_speed_selection_and_invalid_speed(self):
        headers = {"X-Robot-Control": "1"}
        self.request("/api/stop", {}, headers)
        self.assertEqual(self.request("/api/claim", {"speed": "turbo"}, headers)[0], 409)
        status, _, _ = self.request("/api/claim", {"speed": "full"}, headers)
        self.assertEqual(status, 200)
        _, body, _ = self.request("/api/status")
        self.assertEqual(json.loads(body)["speed"], "full")
        _, body, _ = self.request("/api/stop", {}, headers)
        self.assertEqual(json.loads(body)["speed"], "slow")

    def test_calibration_http_save_validation_and_driver_exclusion(self):
        headers = {"X-Robot-Control": "1"}
        self.request("/api/stop", {}, headers)
        settings = {**DEFAULT_CALIBRATION, "left_forward": 11}
        self.assertEqual(self.request("/api/calibration", settings)[0], 403)
        self.assertEqual(self.request("/api/calibration", {"left_forward": 11}, headers)[0], 409)
        status, data, _ = self.request("/api/calibration", settings, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["calibration"], settings)
        self.request("/api/claim", {}, headers)
        self.assertEqual(self.request("/api/calibration", DEFAULT_CALIBRATION, headers)[0], 409)
        self.request("/api/stop", {}, headers)
        self.assertEqual(self.request("/api/calibration", DEFAULT_CALIBRATION, headers)[0], 200)


if __name__ == "__main__":
    unittest.main()
