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
FIRMWARE_ID = b"NB3-DEMO-6 SERVO WATCHDOG=600 SPEED=12 FULL=90 LIMIT=NONE TRIM=24 LEFT=9 RIGHT=10"
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
    SEARCH_TURN_SECONDS = 0.3
    SEARCH_MAX_TURNS = 8
    CENTER_TURN_SECONDS = 0.12
    CENTER_MAX_TURNS = 6
    SEARCH_MAX_SECONDS = 90
    SEARCH_STEP_SECONDS = 0.4
    SEARCH_RETREAT_SECONDS = 0.3
    SEARCH_MAX_STEPS = 4
    SEARCH_RETREAT_WINDOW = 10
    SEARCH_TURN_CAP = 24
    SEARCH_STEP_CAP = 12
    SEARCH_PLAN_CAP = 3

    def __init__(self, frames, serial_port=None, clock=time.monotonic, calibration_path=None):
        self.frames = frames
        self.serial = serial_port
        self.clock = clock
        self.lock = threading.RLock()
        self.owner = None
        self.owner_kind = None
        self.stop_generation = 0
        self.turn_deadline = self.search_deadline = None
        self.search_turns = self.center_turns = 0
        self.search_steps = 0
        self.search_turn_limit = self.SEARCH_MAX_TURNS
        self.search_step_limit = self.SEARCH_MAX_STEPS
        self.search_explore = False
        self.search_speed = 'slow'
        self.retreat_until = 0
        self.sequence = -1
        self.last_contact = 0.0
        self.last_move = 0.0
        self.command = "stop"
        self.reason = "Ready"
        self.fault = ""
        self.speed = "slow"
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
                self.owner_kind = None
                self.command = "stop"
                self.speed = "slow"
                self.reason = "Serial connection lost; Arduino timeout stops motors"
                raise ValueError(self.fault) from exc
        if command == "stop":
            self.turn_deadline = None
        self.command = command

    def _stop(self, reason, release=False):
        try:
            self._send("stop")
        except ValueError:
            pass
        self.reason = reason
        if release:
            self.owner = None
            self.owner_kind = None
            self.search_deadline = None
            self.speed = "slow"
            self.search_explore = False
            self.search_speed = 'slow'
            self.retreat_until = 0

    def tick(self):
        with self.lock:
            now = self.clock()
            _, timestamp, _, _ = self.frames.latest()
            if self.owner_kind == 'search' and now >= self.search_deadline:
                self._stop('Search time limit reached', release=True)
            if self.command != "stop":
                if self.turn_deadline is not None and now >= self.turn_deadline:
                    completed = self.command
                    self._stop('Search turn completed')
                    if self.owner_kind == 'search' and completed == 'forward':
                        self.retreat_until = now + self.SEARCH_RETREAT_WINDOW
                elif now - self.last_move > WATCHDOG:
                    self._stop("Control timed out — take control again", release=True)
                elif now - timestamp > FRAME_MAX_AGE:
                    self._stop("Camera paused — take control again", release=True)
            if self.owner and now - self.last_contact > LEASE:
                self._stop("Driver disconnected", release=True)

    def search_limits(self, max_turns=None, max_steps=None, sequence_length=3):
        max_turns = self.SEARCH_MAX_TURNS if max_turns is None else max_turns
        max_steps = self.SEARCH_MAX_STEPS if max_steps is None else max_steps
        for value, lower, cap, label in ((max_turns, 0, self.SEARCH_TURN_CAP, 'Search turns'),
                                       (max_steps, 0, self.SEARCH_STEP_CAP, 'Explore steps'),
                                       (sequence_length, 1, self.SEARCH_PLAN_CAP, 'Moves per image')):
            if type(value) is not int or not lower <= value <= cap:
                raise ValueError(f'{label} must be a whole number from {lower} to {cap}.')
        return max_turns, max_steps, sequence_length

    def claim(self, speed="slow", *, kind="manual", explore=False, max_turns=None, max_steps=None):
        with self.lock:
            if type(explore) is not bool or (explore and kind != 'search'):
                raise ValueError('Invalid exploration setting')
            limits = self.search_limits(max_turns, max_steps)
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
            self.owner_kind = kind
            self.search_turns = self.center_turns = 0
            self.search_steps = 0
            self.search_turn_limit, self.search_step_limit = limits[:2]
            self.search_explore = explore
            self.search_speed = speed if kind == 'search' else 'slow'
            self.retreat_until = 0
            self.search_deadline = self.clock() + self.SEARCH_MAX_SECONDS if kind == 'search' else None
            self.sequence = -1
            self.last_contact = self.clock()
            self.reason = "Control granted"
            return self.owner

    def control(self, token, sequence, command, frame_time):
        with self.lock:
            self.tick()
            if not self.owner or not secrets.compare_digest(str(token), self.owner):
                raise ValueError("Take control before driving")
            if self.owner_kind != 'manual':
                raise ValueError('Search owns the motors. Stop it before driving.')
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
            self.stop_generation += 1
            self._stop("Stopped — take control to drive again", release=True)

    def search_contact(self, token):
        """Renew only the search lease, never a movement command."""
        with self.lock:
            self.tick()
            if self.owner_kind != 'search' or self.owner != token:
                raise ValueError('Search motor control was released')
            self.last_contact = self.clock()

    def search_turn(self, token, direction='right', *, centering=False):
        """Bounded search turns; precise centring always uses calibrated speed."""
        with self.lock:
            self.search_contact(token)
            if type(centering) is not bool or direction not in ('left', 'right'):
                raise ValueError('Invalid search turn')
            _, timestamp, _, _ = self.frames.latest()
            if self.clock() - timestamp > FRAME_MAX_AGE:
                self._stop('Camera paused', release=True)
                raise ValueError('Camera paused. Search stopped.')
            turns = self.center_turns if centering else self.search_turns
            limit = self.CENTER_MAX_TURNS if centering else self.search_turn_limit
            if turns >= limit or self.command != 'stop':
                raise ValueError('Search turn limit reached or a turn is still active')
            self.speed = 'slow' if centering else self.search_speed
            self.retreat_until = 0
            if centering:
                self.center_turns += 1
            else:
                self.search_turns += 1
            duration = self.CENTER_TURN_SECONDS if centering else self.SEARCH_TURN_SECONDS
            self.last_move = self.clock()
            self.turn_deadline = self.clock() + duration
            self._send(direction)
            self.reason = ('Centring: ' if centering else 'Looking around: ') + 'short ' + direction + ' turn'
            return duration

    def search_actions(self, token):
        with self.lock:
            self.search_contact(token)
            actions = ['inspect', 'stop']
            if self.search_turns < self.search_turn_limit:
                actions += ['left', 'right']
            if self.search_explore and self.search_steps < self.search_step_limit:
                actions.append('forward')
                if self.clock() < self.retreat_until:
                    actions.append('backward')
            return actions

    def search_step(self, token, direction):
        with self.lock:
            self.search_contact(token)
            if direction not in ('forward', 'backward') or direction not in self.search_actions(token):
                raise ValueError('This exploration step is not permitted. Search stopped.')
            _, timestamp, _, _ = self.frames.latest()
            if self.clock() - timestamp > FRAME_MAX_AGE or self.command != 'stop':
                raise ValueError('A step needs fresh video and stopped wheels.')
            self.retreat_until = 0
            self.search_steps += 1
            self.speed = self.search_speed
            duration = self.SEARCH_STEP_SECONDS if direction == 'forward' else self.SEARCH_RETREAT_SECONDS
            self.last_move = self.clock()
            self.turn_deadline = self.clock() + duration
            self._send(direction)
            self.reason = 'Exploring: short ' + direction + ' step'
            return duration

    def pause_search(self, token, reason='Checking live observation'):
        with self.lock:
            self.search_contact(token)
            self._stop(reason)
            # A partially completed forward pulse cannot authorize a retreat.
            self.retreat_until = 0

    def release_search(self, token, reason='Search finished'):
        with self.lock:
            if self.owner_kind == 'search' and self.owner == token:
                self._stop(reason, release=True)

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
                    "owner_kind": self.owner_kind, "stop_generation": self.stop_generation,
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
            detector = getattr(self.server, "detector", None)
            speech = getattr(self.server, "speech", None)
            talk = getattr(self.server, "talk", None)
            search = getattr(self.server, "search", None)
            return self.reply(200, {**self.server.controller.status(),
                "detection": detector.snapshot()[1] if detector else {"available": False, "enabled": False},
                "speech": speech.status() if speech else {"enabled": False},
                "talk": talk.status() if talk else {"enabled": False},
                "search": search.status() if search else {"enabled": False}})
        if path == '/api/search/frame':
            search = getattr(self.server, 'search', None)
            if search:
                with search.lock:
                    jpeg = search.matched_jpeg
                if jpeg:
                    return self.reply(200, jpeg, 'image/jpeg')
            return self.reply(404, {'error': 'No confirmed match image'})
        if path == '/api/search/memory/frame':
            search = getattr(self.server, 'search', None)
            check = parse_qs(urlsplit(self.path).query).get('check', [''])[0]
            if search and check.isascii() and check.isdigit() and len(check) <= 12:
                with search.lock:
                    jpeg = search.memory.thumbnail(int(check))
                if jpeg:
                    return self.reply(200, jpeg, 'image/jpeg')
            return self.reply(404, {'error': 'Search memory image no longer available'})
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
                 "/talk.js": ("talk.js", "text/javascript"),
                 "/search.js": ("search.js", "text/javascript"),
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
            path = urlsplit(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= (4096 if path in ('/api/talk/text', '/api/search/start') else 2048):
                raise ValueError("Request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            path = urlsplit(self.path).path
            controller = self.server.controller
            if path == '/api/detections':
                detector = getattr(self.server, 'detector', None)
                if not detector:
                    raise ValueError('Start the server with --detect to enable the NPU.')
                return self.reply(200, detector.set_enabled(body.get('enabled')))
            if path.startswith('/api/search/'):
                search = getattr(self.server, 'search', None)
                if not search:
                    raise ValueError('Search is not enabled. Start the server with --realtime.')
                action = path.removeprefix('/api/search/')
                if action == 'start':
                    if body.get('target') is None:
                        raise ValueError('Enter a clue or use Hold to give a clue.')
                    result = search.start(body.get('token'), body.get('target'), body.get('allow_turns'),
                                          body.get('frame_time'), body.get('stop_generation'), explore=body.get('explore', False),
                                          speed=body.get('speed', 'slow'), max_turns=body.get('max_turns'),
                                          max_steps=body.get('max_steps'), sequence_length=body.get('sequence_length', 3),
                                          live=body.get('live', False), image_rate=body.get('image_rate', 2))
                elif action == 'listen' and hasattr(search, 'finish_recording'):
                    result = search.start(body.get('token'), None, body.get('allow_turns'),
                                          body.get('frame_time'), body.get('stop_generation'), explore=body.get('explore', False),
                                          speed=body.get('speed', 'slow'), max_turns=body.get('max_turns'),
                                          max_steps=body.get('max_steps'), sequence_length=body.get('sequence_length', 3),
                                          live=body.get('live', False), image_rate=body.get('image_rate', 2))
                elif action == 'finish' and hasattr(search, 'finish_recording'):
                    result = search.finish_recording(body.get('token'))
                elif action == 'heartbeat':
                    result = search.heartbeat(body.get('token'), body.get('frame_time'))
                elif action == 'memory/clear':
                    result = search.clear_memory()
                elif action == 'cancel':
                    # Global Stop is separate; this route cancels only the caller's search.
                    search._validate_token(body.get('token'))
                    result = search.cancel(body['token'])
                else:
                    raise ValueError('Unknown search action')
                return self.reply(200, result)
            if path in ("/api/speech/describe", "/api/speech/stop"):
                speech = getattr(self.server, "speech", None)
                if not speech:
                    raise ValueError("Speech is not enabled on this server")
                return self.reply(200, speech.describe() if path.endswith('/describe') else speech.stop())
            if path.startswith('/api/talk/'):
                talk = getattr(self.server, 'talk', None)
                if not talk:
                    raise ValueError('Conversation is not enabled on this server')
                action = path.removeprefix('/api/talk/')
                if action == 'text':
                    result = talk.send_text(body.get('token'), body.get('prompt'))
                else:
                    result = talk.start(body.get('token')) if action == 'start' else talk.command(action, body.get('token'))
                return self.reply(200, result)
            if path == "/api/claim":
                return self.reply(200, {"token": controller.claim(body.get("speed", "slow"))})
            if path == "/api/control":
                controller.control(body.get("token"), body.get("sequence"), body.get("command"), body.get("frame_time"))
            elif path == "/api/stop":
                controller.stop_all()
                search = getattr(self.server, 'search', None)
                if search:
                    search.cancel()
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
        raise RuntimeError("NB3-DEMO-6 firmware required. Stop the website and upload arduino/robot_demo; older firmware still has the two-second cutoff. Run without --serial for preview.")
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
    parser.add_argument("--realtime", action="store_true", help="Enable OpenAI push-to-talk through the robot ears and mouth")
    parser.add_argument("--openai-key-file", type=Path, default=Path.home() / '.config/nb3/openai-api-key')
    parser.add_argument("--realtime-model", default="gpt-realtime")
    parser.add_argument("--search-model", default="gpt-4.1-mini", help="Vision model for look-around searches")
    parser.add_argument("--speech-engine", type=Path, default=ROOT.parents[3] / "_tmp/speech/bin/espeak-ng")
    parser.add_argument("--speech-device", default="auto", help="ALSA output device; auto selects the NB3/MAX98357A mouth")
    args = parser.parse_args()
    if args.speech and not args.detect:
        parser.error("--speech requires --detect")
    frames = Frames()
    port = connect_arduino(args.serial) if args.serial else None
    controller = Controller(frames, port, calibration_path=args.calibration_file)
    camera = None
    server = None
    detector = None
    speech = None
    talk = None
    search = None
    audio_lock = threading.Lock()
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
            speech = Speech(detector, args.speech_engine, args.speech_device, audio_lock=audio_lock)
        server.speech = speech
        if args.realtime:
            from talk import Talk
            talk = Talk(frames, audio_lock, args.openai_key_file, args.realtime_model, args.speech_device)
        server.talk = talk
        if talk:
            from hunt import Hunt
            from search_vision import SearchVision
            search = Hunt(controller, talk._key, speech, SearchVision(args.search_model),
                          audio_lock=audio_lock, device=args.speech_device, model=args.realtime_model,
                          mirrored=args.flip in ('horizontal', 'both'))
        server.search = search

        def watchdog():
            while not finished.wait(0.05):
                controller.tick()
                if search:
                    search.tick()
                if talk:
                    talk.tick()

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
        if search:
            search.close()
        if talk:
            talk.close()
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
