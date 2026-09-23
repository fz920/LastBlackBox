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

## Show what the Coral detects

Complete the [Coral setup](../../../intelligence/NPU/coral/Setup-Trixie.md), then
run this once from the repository root (internet required for downloads):

```bash
bash boxes/servers/python/robot_control/setup-detection.sh
```

Start the website with detection enabled:

```bash
python3 boxes/servers/python/robot_control/server.py --detect
# Include the existing Arduino controls:
python3 boxes/servers/python/robot_control/server.py --serial /dev/ttyUSB0 --detect
```

Run only one server at a time. Refresh the website and leave **Show detections**
checked. Green boxes show object names and confidence scores, with a count below
the camera. Try a person, cup, bottle, or chair in good light. **Coral live**
confirms inference is running even if no objects exceed the 50% threshold.
Uncheck the box for the faster raw camera preview. **Flip vertically** flips the
display and boxes together; it does not change the image sent to the model.

This uses Google's Edge TPU SSD MobileNet V2 model trained on COCO's 80 common
object categories. It does not recognise arbitrary objects, identities, or
distances. Labels can be wrong and missing boxes do not mean the path is clear.
With `--detect` alone, this step only displays detections. Add spoken descriptions
as described below; detection never initiates robot movement.

Detection runs at up to five analysed frames per second. Each JPEG is delivered
with its own boxes so moving objects stay aligned. The camera still captures
at 15 fps; the raw website preview polls at up to about 12 fps. The displayed
inference time excludes JPEG decoding, camera capture, and network delivery.
Only the latest result is retained in memory; no camera images are saved or sent
to a cloud service. If detection fails, the website shows raw video without old
boxes and the worker retries. Existing camera and motor watchdogs still apply.

The system Python owns the camera. A separate worker uses
`_tmp/coral/detection-venv` (Python 3.9.25, Google's `tflite-runtime==2.5.0.post1`,
NumPy 1.26.4, Pillow 11.3.0). This older, isolated runtime is needed on this Pi:
the earlier Python 3.11 / TFLite 2.14 environment passed classification but
segfaulted when loading this detector. The setup script checks the downloaded
wheel/model/labels against SHA-256 hashes. Models and environments stay under
ignored `_tmp/coral/`; the script and application code are versioned.

For diagnosis, open `/api/detections` for current status. CLI overrides are
`--detector-python`, `--detection-model`, and `--detection-labels`; the worker
expects the same SSD output layout, not arbitrary models.

To run it in the background for the current Pi session:

```bash
systemctl --user stop nb3-robot-control
systemd-run --user --unit=nb3-robot-control --collect \
  --property=WorkingDirectory="$PWD" -- \
  /usr/bin/python3 -B "$PWD/boxes/servers/python/robot_control/server.py" \
  --serial /dev/ttyUSB0 --detect
```

This transient service does not automatically start after a reboot.

## Spoken descriptions through the robot's mouth

Install the [NB3 mouth/ears driver](../../../audio/i2s/driver/Setup-Pi.md) and
prepare the offline speech engine once:

```bash
bash boxes/servers/python/robot_control/setup-speech.sh
```

This downloads Debian's `espeak-ng`, `libespeak-ng1`, `espeak-ng-data`,
`libpcaudio0`, and `libsonic0` into ignored `_tmp/speech/`, without changing
system packages. eSpeak NG 1.52.0 was tested on this Pi. This follows the
[repo's speech generation example](../../../audio/signal-processing/python/generation/README.md).

Stop the old website and start with both `--detect` and `--speech`:

```bash
systemctl --user stop nb3-robot-control
systemd-run --user --unit=nb3-robot-control --collect \
  --property=WorkingDirectory="$PWD" -- \
  /usr/bin/python3 -B "$PWD/boxes/servers/python/robot_control/server.py" \
  --serial /dev/ttyUSB0 --detect --speech
```

Refresh the website. **Describe what I see** speaks the current detected object
names and counts, for example, “I think I can see a person and 2 cups.” The
description also appears below the camera. **Stop speaking** interrupts playback.
Speech comes from the robot's mouth, not the phone/laptop browser. No driver
claim or motor command is needed; there is no automatic narration or movement.

Descriptions use fresh results above 50% confidence, mentioning at most three
object categories. They do not infer activities, identities, distance, or a safe
route. With no qualifying objects, the robot says it is unsure what it is looking
at. If the detector is stale or disconnected, the request is rejected. Only one
utterance is allowed at a time; repeated clicks do not build a queue.

The service uses offline eSpeak NG (British English, 155 words/minute, amplitude
65) and ALSA `aplay`, with bounded subprocess timeouts and a cancellable background
thread. Basic mode needs no LLM. Neither mode needs an API key, cloud connection,
microphone recording, or saved audio files. Voice generation and playback are independent of camera,
NPU, and motor watchdogs. The transcript is retained in memory until replaced or
the server restarts.

The default output is the ALSA card named `MAX98357A`/NB3, discovered by name.
There is no fallback to HDMI or headphones if the mouth is missing. Use
`--speech-device plughw:CARD=MAX98357A,DEV=0` to select it explicitly, or
`--speech-engine /usr/bin/espeak-ng` if using a system installation.
Missing engine/device and playback failures appear in the website's speech status.

On this Pi the repo's unmodified driver built for `6.18.50+rpt-rpi-v8`, registered
both playback and capture, and bound as `nb3_audio`. A live description completed
through ALSA without errors while camera and detection stayed live and motors
remained stopped. The user confirmed hearing it clearly from the mouth.
Chromium checks passed for describe/stop, transcript, and desktop/mobile layouts.

## Local LLM descriptions

The website can use the installed [Qwen model](../../../intelligence/LLMs/local-NB3/README.md)
to phrase its spoken descriptions. Start the server with:

```bash
python3 boxes/servers/python/robot_control/server.py --serial /dev/ttyUSB0 --detect --speech --llm
```

For the background service, append `--llm` to the `systemd-run` command above.
Refresh the page, leave **Use local LLM** checked, and press **Describe what I see**.
The website shows loading/thinking progress, the resulting text, and whether it
came from the LLM or a basic fallback. **Cancel description** stops model loading
or generation; the same button becomes **Stop speaking** during audio playback.
Uncheck **Use local LLM** for the faster basic description.

This reuses the existing model and runtime: no additional model downloads or
copies. The model process starts only for a description, uses two CPU threads,
and exits before audio playback, releasing its memory. Requests are private to
the website process through a temporary Unix socket; there is no extra network
listener. Loading is limited to 45 seconds and generation to 30 seconds. A
two-category test took 13.3 seconds, and a live website/browser test took 27.2
seconds on this Pi. First use or a busy Pi may take longer.

Because unrestricted Qwen answers failed the earlier factuality checks, this
integration limits what it can generate. Application code forms the correct
count/name phrases for up to three detected categories. The LLM chooses their
ordering and introductory wording under a grammar that preserves all those
phrases. A separate validator rejects output outside the permitted sentences,
including truncation, incorrect counts, or invented attributes. This provides
limited wording variation, not unrestricted scene understanding. It cannot add
activities, colours, identities, or distances, and never commands movement.
The detector itself can still misidentify or miss objects.

If the model is unavailable, times out, or produces invalid output, the existing
basic description is used and the website explains the fallback. Empty detections
skip the model. After generation the current detections are checked again: if
the selected object counts changed, a fresh basic description replaces the old
answer; if detection is stale, nothing is spoken. Stop/cancel prevents later
playback, including when pressed during loading.

Tests in `test_llm.py` cover the permitted vocabulary, exact counts, rejecting
invented/truncated answers, process cleanup, cancellation, timeouts, opt-out,
empty scenes, and scene changes. All 51 Python tests passed. Live Chromium checks
passed for LLM description/playback, cancel, opt-out, and desktop/mobile layouts,
with the camera live and no motor commands sent.

## Preview controls

The default is **preview mode**: live camera and simulated direction commands,
with no serial port opened and no motor output. Press **Take control** to try
the buttons. Releasing all directions stops it. **Stop** also releases driver control.

## Steer while moving

- Hold **↑ + →** (or **W + D**) to curve forward-right; **↑ + ←** curves left.
- Release the turn key while holding forward to go straight again. Release all
  directions to stop. Left/right alone still turn in place.
- **↓ + ←/→** curves in reverse: the rear of the robot moves toward the selected
  side. Both wheels reverse; this differs from which way the nose turns.
- On a touchscreen, hold forward/back with one finger and left/right with another.
  Keyboard and pointer inputs can also be combined.
- Opposite inputs cancel on their axis: ↑ + ↓ stops forward/back movement,
  and ← + → cancels turning.

By default, slow curves use an offset of 12 on the outside wheel and 6 on the
inside wheel. With calibration, the inner wheel uses half its directional
setting, rounded up.
These are control settings, not measured wheel speeds; the actual curve depends
on the servos, surface and load. The server requires **NB3-DEMO-5** firmware,
so re-upload the sketch when upgrading from the initial controller version.

## Compare slow and full speed

Keep both wheels raised off the ground for this comparison. Refresh the website,
choose **Full speed · 2-second test** under **Test speed**, then **Take control**.
Briefly hold forward, release it, then try backward. Straight full-speed motion
uses `Servo.write(0)` and `Servo.write(180)`, matching the original remote-NB3
sketch's command range while retaining the corrected website directions.

Each uninterrupted full-speed movement is capped at two seconds by both the Pi
and Arduino. The Arduino blocks further full-speed commands after its limit
until Stop is received. The normal 500 ms Pi / 600 ms Arduino disconnect
timeouts still apply. Releasing the direction stops immediately; the STOP button,
loss of control, or the Pi's test limit also resets the selection to slow.
Choose full speed again before taking control for another test after that reset.
The speed selector is locked while someone has control. Slow remains the default;
its default output and both servos' stop values are unchanged.

This feature requires the included **NB3-DEMO-5** firmware. Full-speed movement
uses uppercase serial direction letters. Calibrated slow movement uses a
bounded packet containing both servo settings; legacy lowercase commands
remain available with their original fixed offsets.
Full-speed turns are supported, with an offset of 45 for the inner wheel on curves.
Wheel speeds are not measured: compare forward and backward physically.

If school Wi-Fi blocks connections between devices, use a shared local hotspot
or another network that permits device-to-device connections.

## Tune slow wheel speeds

Open **Adjust slow wheel speeds** on the website. Four independent settings
control the command strength for each physical wheel in forward and reverse.
They are servo offsets, not RPM or percentages. All default to 12; allowed values
are whole numbers from 6 to 24. The working neutral/Stop settings stay at 90.
Full-speed tests ignore these settings.

The user confirmed that full speed worked well and tuned slow mode to roughly
matching wheel speeds on 2026-09-23. The saved settings for this robot are:

| Wheel | Forward | Reverse |
| --- | ---: | ---: |
| Left | 15 | 7 |
| Right | 12 | 15 |

These settings were chosen by observation, not measured RPM, and are specific
to this robot. Other robots should start at the default and tune independently.

1. Press **STOP** to release control before editing.
2. Change one direction's faster-wheel setting by one step and **Save wheel settings**.
3. Select **Slow**, take control, and briefly test that direction with wheels raised.
4. Repeat as needed. If the faster wheel becomes slower, move back one step.
   If reducing it makes it stop or move inconsistently, restore that setting and
   try increasing the slower wheel by one step instead.
5. After both directions match reasonably well in the air, check straight travel
   on the floor; load can change the result. Exact speed matching needs feedback.

The fields are locked while a driver has control. Saving settings sends only Stop;
movement requires taking control and holding a direction. Settings are stored on
the Pi in `_tmp/robot-control/slow-calibration.json` and survive server restarts.
**Reset all to 12**, then **Save wheel settings**, restores the original slow mode.
The same directional settings are used during turns and curves.

### Keep or restore the tuned settings

The runtime file is ignored by Git. A versioned snapshot is saved alongside this
README as `calibration.fengzhe-nb3.json`. To restore it, stop the website, run
these commands from the repository root, then start the website again:

```bash
mkdir -p _tmp/robot-control
cp boxes/servers/python/robot_control/calibration.fengzhe-nb3.json _tmp/robot-control/slow-calibration.json
```

After further tuning, copy the runtime file back to the snapshot and commit it
to keep a history of settings:

```bash
cp _tmp/robot-control/slow-calibration.json boxes/servers/python/robot_control/calibration.fengzhe-nb3.json
```

The current regression checks cover calibrated outputs for all directions,
unchanged full-speed output, packet bounds/malformed input, Stop interrupting a
partial packet, watchdogs, persistence, validation, and website editing controls.

## Enable the Arduino

The included sketch is for **Arduino Nano + continuous-rotation servos**, with
physical left servo on D9 and right servo on D10, as confirmed on this robot.
This is the opposite pin assignment to the remote-NB3 tutorial. On the current
robot, the user found all four directions reversed. The website corrects this
in `server.py` by reversing both wheels' commands before sending them to the
firmware. Forward now uses a higher signal on D9 and a
lower signal on D10; left/right turns are reversed too. Curves reverse travel
while retaining the slower inner wheel. Browser labels and status still show
the requested direction. No firmware upload is needed for this correction;
do not also reverse the firmware signs, which would undo the correction.
The current speed and calibration options require uploading the NB3-DEMO-5 sketch.
It is **not** the DC-motor/H-bridge sketch. Keep motors disconnected during upload.

With Arduino CLI and its AVR core / Servo library installed:

```bash
~/.local/bin/arduino-cli compile --fqbn arduino:avr:nano --build-path /tmp/nb3-demo-build boxes/servers/python/robot_control/arduino/robot_demo
~/.local/bin/arduino-cli upload --fqbn arduino:avr:nano --port /dev/ttyUSB0 --input-dir /tmp/nb3-demo-build
```

The standard `arduino:avr:nano` target was compiled and successfully uploaded
on this robot. Its previous firmware is backed up at
`_tmp/robot-control/previous-firmware.hex` (about 77 KB) during the original setup.
The backup taken before adding full speed is
`_tmp/robot-control/before-full-speed-20260923.hex`. The pre-calibration backup is
`_tmp/robot-control/before-calibration-20260923.hex`. The firmware identification
and Stop commands were verified after upload. Basic physical movement was then
confirmed by the user. The new steering combinations still need a physical check.

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
90; actual speed depends on the servos and load. These stop and straight-drive
values have been preserved during the pin-mapping correction. The reported speed
imbalance was addressed with the directional settings saved above; further
tuning may be needed under different loads. The current `Servo.write(90)` maps to about 1472 microseconds
with the installed library, which may differ from the servo's specified neutral.
Confirm the motor model before changing it; equal command offsets are not proof
of equal wheel speeds.

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
- Releasing all directions sends Stop immediately. Changing tabs, losing window focus,
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

The Python suite also compiles the actual Arduino sketch against simulated servos
when `g++` is available, checking all curved wheel outputs and their stop timeouts.
Run the input-combination tests with `node test_steering.js` and the speed-selector
handler tests with `node test_speed_ui.js` on a machine with
Node.js (Node is not required to run the website).

Speech adds `python3 -B -m unittest -v test_speech` and
`node test_speech_ui.js`. Tests cover counts/plurals, empty scenes, stale/failed
detection rejection, device selection, no speech queue, subprocess cancellation,
playback errors, protected HTTP routes, and no motor requests. The combined
suite passes 42 Python tests and all four JavaScript suites.

Detection adds `node test_detection_ui.js` for box geometry, vertical flipping,
labels/counts, and unavailable/stale states. The Python detection tests cover
model-output decoding, worker pipes and shutdown, stale results, and exact
JPEG/metadata pairing with raw-video fallback. All 34 Python tests and the three
JavaScript suites passed on the Pi. Hardware checks detected a person and tie
in Coral's Grace Hopper test image (about 15 ms warm inference), then processed
the live camera. Chromium checks covered desktop/mobile layout, toggling, and
orientation. Terminating the worker confirmed raw video continued without old
boxes and detection recovered automatically; the robot stayed stopped.

The speed/calibration update passed all 27 Python tests, 42 JavaScript steering
assertions, and the actual browser-handler checks in `test_speed_ui.js`. The
Arduino sketch also compiled for `arduino:avr:nano` and was uploaded with
verification. Live camera, firmware identification, settings persistence, and
stopped startup were checked on the Pi. Physical wheel matching was assessed
by the user.

The earlier steering update passed all 18 Python tests, 42 JavaScript input-combination
assertions, and 17 checks of the actual input handlers using a fake DOM and
mocked requests. Those checks include partial releases, keyboard aliases,
opposing inputs, two-finger input, pointer cancellation, Stop, focus loss and
stale video. Movement commands were tested only with simulated hardware.

During the initial setup, all 15 server tests passed. Desktop and 390-pixel-wide mobile
layouts, keyboard/touch release, single-driver control, focus loss, frozen
video, and network loss passed Chromium interaction checks through a local
request bridge (the Pi's automated Chromium networking stalled even on a
minimal test page). The real camera and HTTP endpoints were checked separately.
The actual Arduino sketch also passed a simulated-hardware check of its
direction outputs, neutral startup, and independent 600 ms command timeout.

Camera encoding follows the [official Picamera2 streaming example](https://github.com/raspberrypi/picamera2/blob/main/examples/mjpeg_server_2.py),
using its memory-output pattern. Arduino setup follows the [Arduino CLI guide](https://docs.arduino.cc/arduino-cli/getting-started).
