#!/usr/bin/env python3
"""Local NB3 controller. Keeps one JPEG in RAM; never records video to disk."""

import argparse
import io
import json
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parent
# The legacy letter protocol uses the opposite direction convention on this
# robot. Full-speed commands use this corrected mapping. Calibrated slow mode
# instead sends physical wheel outputs using WHEELS below.
COMMANDS = {
    "forward": b"b", "backward": b"f", "left": b"r", "right": b"l", "stop": b"x",
    "forward_left": b"z", "forward_right": b"c",
    "backward_left": b"q", "backward_right": b"e",
}
WATCHDOG = 0.5
LEASE = 2.0
FRAME_MAX_AGE = 1.2
FULL_SPEED_LIMIT = 2.0
FIRMWARE_ID = b"NB3-DEMO-5 SERVO WATCHDOG=600 SPEED=12 FULL=90 LIMIT=2000 TRIM=24 LEFT=9 RIGHT=10"
DEFAULT_CALIBRATION = {"left_forward": 12, "right_forward": 12,
                       "left_backward": 12, "right_backward": 12}
# Physical wheel directions; curves use half the calibrated inner-wheel offset.
WHEELS = {"forward": (1, 1), "backward": (-1, -1),
          "left": (-1, 1), "right": (1, -1),
          "forward_left": (0.5, 1), "forward_right": (1, 0.5),
          "backward_left": (-0.5, -1), "backward_right": (-1, -0.5)}


def validate_calibration(values):
    if not isinstance(values, dict) or values.keys() != DEFAULT_CALIBRATION.keys():
        raise ValueError("Provide all four wheel settings")
    if any(type(value) is not int or not 6 <= value <= 24 for value in values.values()):
        raise ValueError("Wheel settings must be whole numbers from 6 to 24")
    return dict(values)


def slow_command(command, calibration):
    angles = []
    for wheel, direction, sign in zip(("left", "right"), WHEELS[command], (1, -1)):
        offset = calibration[wheel + ("_forward" if direction > 0 else "_backward")]
        # Round half offsets upward so an odd setting changes a curve predictably.
        strength = offset if abs(direction) == 1 else (offset + 1) // 2
        angles.append(90 + sign * (strength if direction > 0 else -strength))
    return f"@{angles[0]:02X}{angles[1]:02X}\n".encode()


class Frames(io.BufferedIOBase):
    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None
        self.timestamp = 0.0
        self.number = 0
        self.error = "Camera starting"

    def write(self, data):
        with self.lock:
            self.frame = bytes(data)
            self.timestamp = time.monotonic()
            self.number += 1
            self.error = ""
        return len(data)

    def latest(self):
        with self.lock:
            return self.frame, self.timestamp, self.number, self.error


class Controller:
    def __init__(self, frames, serial_port=None, clock=time.monotonic, calibration_path=None):
        self.frames = frames
        self.serial = serial_port
        self.clock = clock
        self.lock = threading.RLock()
        self.owner = None
        self.sequence = -1
        self.last_contact = 0.0
        self.last_move = 0.0
        self.command = "stop"
        self.reason = "Ready"
        self.fault = ""
        self.speed = "slow"
        self.full_started = None
        self.calibration_path = Path(calibration_path) if calibration_path else None
        self.calibration = dict(DEFAULT_CALIBRATION)
        if self.calibration_path and self.calibration_path.exists():
            self.calibration = validate_calibration(json.loads(self.calibration_path.read_text()))

    def _send(self, command):
        data = COMMANDS[command]
        if self.speed == "full" and command != "stop":
            data = data.upper()
        elif command != "stop":
            data = slow_command(command, self.calibration)
        if self.serial:
            try:
                if self.serial.write(data) != len(data):
                    raise OSError("Incomplete serial write")
            except Exception as exc:
                self.fault = f"Arduino disconnected: {exc}"
                self.owner = None
                self.command = "stop"
                self.speed = "slow"
                self.full_started = None
                self.reason = "Serial connection lost; Arduino timeout stops motors"
                raise ValueError(self.fault) from exc
        if command == "stop":
            self.full_started = None
        elif self.speed == "full" and self.full_started is None:
            self.full_started = self.clock()
        self.command = command

    def _stop(self, reason, release=False):
        try:
            self._send("stop")
        except ValueError:
            pass
        self.reason = reason
        if release:
            self.owner = None
            self.speed = "slow"

    def tick(self):
        with self.lock:
            now = self.clock()
            _, timestamp, _, _ = self.frames.latest()
            if self.command != "stop":
                if self.full_started is not None and now - self.full_started >= FULL_SPEED_LIMIT:
                    self._stop("Full-speed test finished — take control to test again", release=True)
                elif now - self.last_move > WATCHDOG:
                    self._stop("Control timed out — take control again", release=True)
                elif now - timestamp > FRAME_MAX_AGE:
                    self._stop("Camera paused — take control again", release=True)
            if self.owner and now - self.last_contact > LEASE:
                self._stop("Driver disconnected", release=True)

    def claim(self, speed="slow"):
        with self.lock:
            self.tick()
            if self.fault:
                raise ValueError(self.fault)
            if self.owner:
                raise ValueError("Someone else is driving. Ask them to release control.")
            if speed not in ("slow", "full"):
                raise ValueError("Choose slow or full speed")
            self._send("stop")
            self.speed = speed
            self.owner = secrets.token_urlsafe(24)
            self.sequence = -1
            self.last_contact = self.clock()
            self.reason = "Control granted"
            return self.owner

    def control(self, token, sequence, command, frame_time):
        with self.lock:
            self.tick()
            if not self.owner or not secrets.compare_digest(str(token), self.owner):
                raise ValueError("Take control before driving")
            if type(sequence) is not int or sequence <= self.sequence:
                raise ValueError("Out-of-order command ignored")
            if command not in COMMANDS:
                raise ValueError("Unknown direction")
            self.sequence = sequence
            now = self.clock()
            if command != "stop":
                _, timestamp, _, _ = self.frames.latest()
                if (not isinstance(frame_time, (int, float)) or
                        not 0 <= now - frame_time <= FRAME_MAX_AGE or
                        now - timestamp > FRAME_MAX_AGE):
                    self._stop("Waiting for fresh video", release=True)
                    raise ValueError("Video is stale. Wait for live video, then take control again.")
                if self.fault:
                    raise ValueError(self.fault)
            self._send(command)
            self.last_contact = now
            self.last_move = now
            self.reason = "Command sent" if self.serial else "Preview command — motors disabled"

    def stop_all(self):
        with self.lock:
            self._stop("Stopped — take control to drive again", release=True)

    def set_calibration(self, values):
        values = validate_calibration(values)
        with self.lock:
            self.tick()
            if self.owner:
                raise ValueError("Press Stop to release control before changing wheel settings")
            self._send("stop")
            if self.calibration_path:
                try:
                    self.calibration_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.calibration_path.with_suffix(".tmp")
                    temporary.write_text(json.dumps(values, indent=2) + "\n")
                    temporary.replace(self.calibration_path)
                except OSError as exc:
                    raise ValueError(f"Could not save wheel settings: {exc}") from exc
            self.calibration = values
            self.reason = "Wheel settings saved. Take control to test slow speed."

    def status(self):
        with self.lock:
            self.tick()
            _, timestamp, number, error = self.frames.latest()
            age = self.clock() - timestamp if number else None
            return {"mode": "robot" if self.serial else "preview",
                    "arduino": "disconnected" if self.fault else ("connected" if self.serial else "disabled"),
                    "busy": self.owner is not None, "command": self.command, "speed": self.speed,
                    "reason": self.reason, "fault": self.fault,
                    "calibration": dict(self.calibration),
                    "camera_live": age is not None and age <= FRAME_MAX_AGE,
                    "camera_error": error, "frame": number}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(3)

    def log_message(self, fmt, *args):
        # No per-frame access log: avoid filling terminal/log storage.
        pass

    def reply(self, code, data, content_type="application/json", extra=None):
        if content_type == "application/json":
            data = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        for name, value in (extra or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/status":
            speech = getattr(self.server, "speech", None)
            return self.reply(200, {**self.server.controller.status(),
                "speech": speech.status() if speech else {"enabled": False}})
        if path == "/api/detections":
            detector = getattr(self.server, "detector", None)
            status = detector.snapshot()[1] if detector else {"enabled": False, "live": False, "objects": [], "error": "Detection is not enabled"}
            return self.reply(200, status)
        if path == "/api/frame":
            data, timestamp, number, error = self.server.controller.frames.latest()
            detection_headers = {"X-Detection-State": "off"}
            if parse_qs(urlsplit(self.path).query).get("detect") == ["1"]:
                detector = getattr(self.server, "detector", None)
                result, status = detector.snapshot() if detector else (None, {"enabled": False})
                detection_headers["X-Detection-State"] = "unavailable" if status['enabled'] else "disabled"
                if result:
                    data, timestamp, number = result['jpeg'], result['frame_time'], result['frame']
                    detection_headers = {"X-Detection-State": "live", "X-Detections": json.dumps(
                        {key: result[key] for key in ('objects', 'inference_ms', 'width', 'height')}, separators=(',', ':'))}
            if data is None or time.monotonic() - timestamp > FRAME_MAX_AGE:
                return self.reply(503, {"error": error or "Camera paused"})
            return self.reply(200, data, "image/jpeg", {"X-Frame-Time": timestamp, "X-Frame-Number": number, **detection_headers})
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/style.css": ("style.css", "text/css"),
                 "/steering.js": ("steering.js", "text/javascript"),
                 "/detection.js": ("detection.js", "text/javascript"),
                 "/speech.js": ("speech.js", "text/javascript"),
                 "/app.js": ("app.js", "text/javascript")}
        if path not in files:
            return self.reply(404, {"error": "Not found"})
        filename, content_type = files[path]
        self.reply(200, (ROOT / "site" / filename).read_bytes(), content_type)

    def do_POST(self):
        # Browser requests must originate from this page, using our custom header.
        origin = self.headers.get("Origin")
        if (self.headers.get("X-Robot-Control") != "1" or
                (origin and origin != "http://" + self.headers.get("Host", ""))):
            return self.reply(403, {"error": "Use the robot controller page"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 2048:
                raise ValueError("Request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            path = urlsplit(self.path).path
            controller = self.server.controller
            if path in ("/api/speech/describe", "/api/speech/stop"):
                speech = getattr(self.server, "speech", None)
                if not speech:
                    raise ValueError("Speech is not enabled on this server")
                return self.reply(200, speech.describe(body.get('use_llm', False)) if path.endswith('/describe') else speech.stop())
            if path == "/api/claim":
                return self.reply(200, {"token": controller.claim(body.get("speed", "slow"))})
            if path == "/api/control":
                controller.control(body.get("token"), body.get("sequence"), body.get("command"), body.get("frame_time"))
            elif path == "/api/stop":
                controller.stop_all()
            elif path == "/api/calibration":
                controller.set_calibration(body)
            else:
                return self.reply(404, {"error": "Not found"})
            self.reply(200, controller.status())
        except (ValueError, TypeError) as exc:
            self.reply(409, {"error": str(exc)})


def connect_arduino(path):
    import serial
    port = serial.Serial(path, 115200, timeout=0.2, write_timeout=0.2, exclusive=True)
    try:
        # Opening USB serial may reset a Nano. Wait for its bootloader.
        time.sleep(2)
        port.reset_input_buffer()
        port.write(b"x?")  # STOP and identify; never probe with a movement command.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if port.readline().strip() == FIRMWARE_ID:
                return port
        raise RuntimeError("Demo firmware not detected. Upload arduino/robot_demo first, or run without --serial for preview.")
    except Exception:
        port.close()
        raise


def start_camera(frames, flip):
    from picamera2 import Picamera2
    from picamera2.encoders import MJPEGEncoder
    from picamera2.outputs import FileOutput
    from libcamera import Transform
    camera = Picamera2()
    try:
        camera.configure(camera.create_video_configuration(
            main={"size": (640, 480)}, controls={"FrameRate": 15},
            transform=Transform(vflip=flip in ("vertical", "both"), hflip=flip in ("horizontal", "both")),
            buffer_count=4))
        # FileOutput accepts a memory sink: no filename or filesystem writes.
        camera.start_recording(MJPEGEncoder(bitrate=6000000), FileOutput(frames))
        return camera
    except Exception:
        camera.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--serial", help="Arduino device, e.g. /dev/ttyUSB0. Omit to disable all motor output.")
    parser.add_argument("--flip", choices=["none", "vertical", "horizontal", "both"], default="none")
    parser.add_argument("--no-camera", action="store_true", help="Test the disconnected camera interface")
    parser.add_argument("--calibration-file", type=Path,
                        default=ROOT.parents[3] / "_tmp/robot-control/slow-calibration.json")
    parser.add_argument("--detect", action="store_true", help="Enable Coral object detection")
    parser.add_argument("--detector-python", type=Path, default=ROOT.parents[3] / "_tmp/coral/detection-venv/bin/python")
    parser.add_argument("--detection-model", type=Path, default=ROOT.parents[3] / "_tmp/coral/models/ssd_mobilenet_v2_coco_quant_postprocess_edgetpu.tflite")
    parser.add_argument("--detection-labels", type=Path, default=ROOT.parents[3] / "_tmp/coral/models/coco_labels.txt")
    parser.add_argument("--speech", action="store_true", help="Enable spoken descriptions through the NB3 mouth")
    parser.add_argument("--llm", action="store_true", help="Enable on-demand local LLM descriptions (requires --speech)")
    parser.add_argument("--speech-engine", type=Path, default=ROOT.parents[3] / "_tmp/speech/bin/espeak-ng")
    parser.add_argument("--speech-device", default="auto", help="ALSA output device; auto selects the NB3/MAX98357A mouth")
    args = parser.parse_args()
    if args.speech and not args.detect:
        parser.error("--speech requires --detect")
    if args.llm and not args.speech:
        parser.error("--llm requires --speech")
    frames = Frames()
    port = connect_arduino(args.serial) if args.serial else None
    controller = Controller(frames, port, calibration_path=args.calibration_file)
    camera = None
    server = None
    detector = None
    speech = None
    finished = threading.Event()
    try:
        if not args.no_camera:
            try:
                camera = start_camera(frames, args.flip)
            except Exception as exc:
                frames.error = f"Camera unavailable: {exc}"
                print(frames.error, flush=True)
        else:
            frames.error = "Camera disabled for testing"
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        server.daemon_threads = True
        server.controller = controller
        if args.detect:
            from detection import Detection
            detector = Detection(frames, args.detector_python, args.detection_model, args.detection_labels, FRAME_MAX_AGE)
            detector.start()
        server.detector = detector
        if args.speech:
            from speech import Speech
            llm = None
            if args.llm:
                from llm import LocalLLM
                llm = LocalLLM(ROOT.parents[3] / '_tmp/llm')
            speech = Speech(detector, args.speech_engine, args.speech_device, llm=llm)
        server.speech = speech

        def watchdog():
            while not finished.wait(0.05):
                controller.tick()

        threading.Thread(target=watchdog, daemon=True).start()

        def shutdown(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, shutdown)
        print(f"Robot control: http://<pi-address>:{args.port}", flush=True)
        print("Motor output: " + (args.serial or "DISABLED (preview mode)"), flush=True)
        print("Video: 640×480, 15 fps, latest frame in RAM only. Ctrl+C to stop.", flush=True)
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        finished.set()
        controller.stop_all()
        if speech:
            speech.close()
        if detector:
            detector.close()
        if server:
            server.server_close()
        if camera:
            camera.stop_recording()
            camera.close()
        if port:
            port.close()


if __name__ == "__main__":
    main()
