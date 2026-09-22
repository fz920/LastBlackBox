# NB3 robot control

A local website with camera video, hold-to-drive buttons, arrow/WASD keys, and
Space/Escape to stop. One browser can drive at a time; other browsers can watch
and press Stop. No cloud service, frontend build step, or internet is required.

## Start with motor output disabled

On the Pi, from the repository root:

```bash
python3 boxes/servers/python/robot_control/server.py
```

Open **http://192.168.1.203:8000** on a phone or laptop on the same Wi-Fi.
That was this Pi's address during setup; use `hostname -I` if it changes.
On the Pi itself, use http://localhost:8000.

The default is **preview mode**: live camera and simulated direction commands,
with no serial port opened and no motor output. Press **Take control** to try
the buttons. Releasing a direction stops it. **Stop** also releases driver control.

If school Wi-Fi blocks connections between devices, use a shared local hotspot
or another network that permits device-to-device connections.

## Enable the Arduino

The included sketch is for **Arduino Nano + continuous-rotation servos**, with
right servo on D9 and left servo on D10, matching the remote-NB3 tutorial.
It is **not** the DC-motor/H-bridge sketch. Keep motors disconnected during upload.

With Arduino CLI and its AVR core / Servo library installed:

```bash
~/.local/bin/arduino-cli compile --fqbn arduino:avr:nano --build-path /tmp/nb3-demo-build boxes/servers/python/robot_control/arduino/robot_demo
~/.local/bin/arduino-cli upload --fqbn arduino:avr:nano --port /dev/ttyUSB0 --input-dir /tmp/nb3-demo-build
```

The standard `arduino:avr:nano` target was compiled and successfully uploaded
on this robot. Its previous firmware is backed up at
`_tmp/robot-control/previous-firmware.hex` (about 77 KB). The firmware identification
and Stop commands were verified after upload; physical wheel motion has not been
tested because the motors are disconnected.

For other Nanos with the old bootloader, use `arduino:avr:nano:cpu=atmega328old`
for both commands. Stop anything using the serial port before uploading.

Stop the preview server with Ctrl+C, then run:

```bash
python3 boxes/servers/python/robot_control/server.py --serial /dev/ttyUSB0
```

The server sends only Stop and an identification request during startup. It
requires the included demo firmware before allowing movement. Opening serial
can reset the Nano. No automatic movement occurs after a restart/reconnection;
the student must take control and hold a direction.

Raise the wheels before reconnecting motor power. Check that neutral stops both
servos and that the four directions match the buttons. Continuous-rotation servos
may need neutral calibration: adjust `LEFT_NEUTRAL` / `RIGHT_NEUTRAL` in the sketch
and re-upload. The demo uses offsets of 12 from neutral instead of the tutorial's
90; actual speed depends on the servos and load.

## Video and disconnect behaviour

- Video is 640×480 at 15 camera frames/second. The browser fetches the newest JPEG
  about 12 times/second, with at most one image request in flight.
- The encoder writes to an in-memory sink. Only the latest JPEG is retained;
  **no photos, video files, history, or per-frame access logs are saved**.
- Browser object URLs are released after each replacement. Slow clients skip
  old frames rather than building a queue.
- Use **Flip vertically** if the camera image needs a vertical flip.
  Server options `--flip vertical`, `--flip horizontal`, or `--flip both` adjust
  camera orientation for everyone. `both` rotates the image 180 degrees.
- Video stale for around a second disables driving and is visibly marked.
- Releasing input sends Stop immediately. Changing tabs, losing window focus,
  releasing control, or pressing Space/Escape sends Stop and releases control.
- The Pi stops movement after 500 ms without control updates; the Arduino stops
  independently after 600 ms without movement commands, including USB loss or
  a crashed Pi process. These are command timeouts, not measured stopping distances.
- The UI reports **commands sent**, not measured wheel motion. A serial failure
  requires restarting the server after reconnecting the USB cable.
- Use this server on a trusted local network. It has no login and is not intended
  to be exposed to the public internet.

## Checks

```bash
cd boxes/servers/python/robot_control
python3 -B -m unittest -v
```

Tests cover command ordering, driver exclusivity, frame freshness, Pi timeouts,
serial failure, preview mode, and HTTP routes. Use `--no-camera` to inspect the
unavailable-camera UI. Python's standard library handles HTTP; the only hardware
packages needed are the Pi's `picamera2` and `pyserial`.

During setup, all 15 server tests passed. Desktop and 390-pixel-wide mobile
layouts, keyboard/touch release, single-driver control, focus loss, frozen
video, and network loss passed Chromium interaction checks through a local
request bridge (the Pi's automated Chromium networking stalled even on a
minimal test page). The real camera and HTTP endpoints were checked separately.
The actual Arduino sketch also passed a simulated-hardware check of its
direction outputs, neutral startup, and independent 600 ms command timeout.

Camera encoding follows the [official Picamera2 streaming example](https://github.com/raspberrypi/picamera2/blob/main/examples/mjpeg_server_2.py),
using its memory-output pattern. Arduino setup follows the [Arduino CLI guide](https://docs.arduino.cc/arduino-cli/getting-started).
