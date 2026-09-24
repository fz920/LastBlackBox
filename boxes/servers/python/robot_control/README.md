# NB3 robot control

A local website with camera video, hold-to-drive buttons, arrow/WASD keys, and
Space/Escape to stop. One browser can drive at a time; other browsers can watch
and press Stop. Driving, Coral detection and basic speech work offline. Optional
push-to-talk conversation uses OpenAI Realtime over the internet.

The page places live video beside the conversation on laptops and tablets, with
the latest reply above the question box. Long replies scroll inside their own
panel. Phones use a compact camera preview that stays visible as you scroll.
STOP stays in the fixed top bar; manual driving sits below the video/conversation,
and offline descriptions and conversation help are expandable. Offline speech
also displays its result in the reply panel. Focus that panel to scroll long
replies with arrow keys without sending driving commands.

## Start with motor output disabled

On the Pi, from the repository root, use `/usr/bin/python3` explicitly. The
website needs the system-installed Picamera2 and pyserial packages. This also
works when the `LBB` virtual environment is active; that environment has an
unrelated package named `serial` and does not include the camera dependencies.


```bash
/usr/bin/python3 boxes/servers/python/robot_control/server.py
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
/usr/bin/python3 boxes/servers/python/robot_control/server.py --detect
# Include the existing Arduino controls:
/usr/bin/python3 boxes/servers/python/robot_control/server.py --serial /dev/ttyUSB0 --detect
```

Run only one server at a time. Refresh the website and leave **Detections**
checked. Green boxes show object names and confidence scores, with a count below
the camera. Try a person, cup, bottle, or chair in good light. **Coral live**
confirms inference is running even if no objects exceed the 50% threshold.
Uncheck the box to **pause Coral NPU processing** and use the faster raw camera
preview. The badge shows **Pausing NPU…** until the worker exits, then **NPU paused**.
This releases the worker's USB connection and clears its old results; it does not
cut USB power. Check the box again to load the model in a new worker and resume.
The setting is shared by all viewers: refreshing or opening another page reflects
the current state without enabling it again. Starting the server with `--detect`
enables detection again; closing the browser alone does not pause it.

Pausing keeps the camera, driving, API conversation and API object search available.
The local **Describe what I see** feature requires fresh Coral detections and asks
you to resume first. **Flip vertically** flips the
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
thread. This basic description needs no API key, cloud connection,
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

## Voice and text conversation (OpenAI)

The local Qwen model and llama.cpp runtime have been removed from this Pi to
recover storage. The website now offers cloud conversation alongside offline
Coral descriptions. No local language model is required.

Install the small WebSocket dependency once, from the repository root:

```bash
bash boxes/servers/python/robot_control/setup-realtime.sh
```

Start the website with the new mode:

```bash
/usr/bin/python3 -B boxes/servers/python/robot_control/server.py --serial /dev/ttyUSB0 --detect --speech --realtime
```

For a background process (stop any existing server first):

```bash
systemd-run --user --unit=nb3-robot-control --collect \
  --property=WorkingDirectory="$PWD" -- \
  /usr/bin/python3 -B "$PWD/boxes/servers/python/robot_control/server.py" \
  --serial /dev/ttyUSB0 --detect --speech --realtime
```

Stop it with `systemctl --user stop nb3-robot-control`. This transient service
must be started again after reboot. Leave off `--serial` for motor-disabled preview.

### Add your API key later

Without a key, the website shows **API key not configured yet** and disables
**Hold to talk** and **Send question**. No microphone or API connection starts. Keep your key out of
chat, browser code, Git and command history. On the Pi, run this in a Bash terminal:

```bash
install -d -m 700 ~/.config/nb3
( umask 077
  read -rsp 'OpenAI API key: ' nb3_api_key
  printf '\n'
  printf '%s' "$nb3_api_key" > ~/.config/nb3/openai-api-key
  unset nb3_api_key
)
```

The prompt hides your input. The key file is outside the repository and readable
only by your account. The server checks it automatically; refresh the website,
with no server restart needed. `OPENAI_API_KEY` in the server's environment is
also supported and takes precedence. API billing/model access must be enabled
in your OpenAI account; a ChatGPT subscription does not pay for API usage.

### Use it

For a typed question, use **Type a question**, then click **Send question** or
press **Enter**. **Shift+Enter** inserts a new line. Questions can contain up to
500 characters. Your text and one fresh camera image go to OpenAI, and its
reply plays through the robot's mouth with a transcript below. The microphone
is never opened for typed questions. Text and voice share the same short-lived
conversation context, so you can alternate between them for follow-ups.
Ordinary spaces and WASD/arrow keys work normally while editing the text box;
Escape and the on-screen Stop button retain their Stop function.

For a spoken question:

1. Speak **near the robot**, not your laptop. Hold **Hold to talk**, ask a question
   such as “What can you see?”, then release. With a keyboard, focus the button
   and hold **Enter**. Space/Escape retain their Stop function.
2. On release, the Pi sends your recorded question and one fresh 640×480 camera
   snapshot to OpenAI. The answer streams through the robot's mouth and its
   transcript appears on the page. This is snapshots plus speech, not continuous video.
3. Wait for the reply to finish, then hold again for a follow-up. **End conversation**
   interrupts recording/playback and clears the API session. To interrupt a reply
   and ask something new, end it first and wait for the button to become available.

Recordings are limited to 15 seconds; exceeding that limit discards the question.
Changing tabs, losing window focus, or closing the page cancels the turn. The Pi
also cancels after 2.5 seconds without browser heartbeats. No sound is captured
between questions, so the speaker cannot feed its answer into the next recording.
Basic offline descriptions and conversation share exclusive access to audio.
The microphone uses ALSA PCM16 mono at 24 kHz; ALSA converts from the NB3 hardware
format. The cloud reply uses the same format, streamed directly into `aplay`.

The API key is read only on the server. Audio and JPEGs are held in RAM, with no
local recording files. Explicitly submitted questions and snapshots are sent to
OpenAI and are subject to its [API data controls](https://developers.openai.com/api/docs/guides/your-data).
Conversation context remains in the API session for up to six turns, then starts
fresh; it also resets after two minutes of inactivity or any cancellation/error.
The last answer text remains on the website until the next question or restart.
Conversation uploads go directly from the Pi to OpenAI; the browser supplies
controls and displays the local camera preview and reply text. Costs include
input audio, images, replies and retained conversation context. Only submitted
questions request model responses; idle preview/detection makes no API calls.

The default model is `gpt-realtime`, with the `marin` voice, no automatic turn
detection, a 384-token response cap and a 30-second output-audio limit. Select a
compatible Realtime model with `--realtime-model`. The API receives no hardware
tools and cannot command the wheels. The browser's manual driving controls and
motor watchdogs are independent of conversation.

Implementation follows OpenAI's
[Realtime conversations and push-to-talk guide](https://developers.openai.com/api/docs/guides/realtime-conversations)
and [server WebSocket guide](https://developers.openai.com/api/docs/guides/realtime-websocket).
`test_talk.py` uses fake WebSocket events and local subprocesses to check protocol,
streaming, exclusive audio, cancellation, limits, stale images, key handling and
HTTP protection. `test_talk_ui.js` checks button ordering and cancellation. The API key stays on the Pi for both text and voice requests.

Setup checks on this Pi: the Python regression suite and all five JavaScript
suites pass. A local hardware test captured nonzero 24 kHz audio from the ears
and streamed a synthesized test reply through the mouth without any cloud call.
Desktop/mobile Chromium rendering and mocked hold/release/follow-up/cancel flows
passed with no JavaScript errors. Chromium network navigation stalled in this
environment, so those rendering checks loaded the actual page sources directly;
the running website's HTTP status, live camera and stopped Arduino were checked
separately.

After configuring the key, one live test through `/api/talk/text` sent a short
question and a fresh camera image to `gpt-realtime`. The robot began playback
at about 2.5 seconds and finished at 6.1 seconds, with a completed response and
no playback errors. The reply was “I can see a stack of chairs in the room.”
The robot's wheels remained stopped. This verifies key/model access, image +
text input, API speech streaming and speaker playback; natural spoken-question
recognition still needs a user speaking near the robot. Latency varies with the
network and response length. The text-input update passes 66 Python tests,
including validation, no microphone capture, shared voice/text context and
protected HTTP handling.

## Look around: spoken and typed clues

With `--realtime`, the **Look around** tab is available beside **Conversation**.
It uses the saved API key: Realtime interprets a spoken or typed clue, then a
separate image-check model (`gpt-4.1-mini` by default; override with
`--search-model`) checks the camera. The interpreter uses `--realtime-model`
(`gpt-realtime` by default). No extra packages or model downloads are needed.
Start or restart the website in your terminal from the repository root:

```bash
/usr/bin/python3 -B boxes/servers/python/robot_control/server.py --serial /dev/ttyUSB0 --detect --speech --realtime
```

1. Put the robot on a clear, level area with room to turn. For an initial wheel
   direction check, lift the wheels; a useful visual search needs the body to rotate.
2. Release manual control with **STOP**, then select **Look around**.
3. Check **Allow short turns to find and centre**, then either type a clue and
   select **Find from text**, or hold **Hold to give a clue**, speak near the
   robot's ears and release. Typed clues can be up to 500 characters; recordings
   must be shorter than 15 seconds. You can also hold Enter while the voice button
   is focused. Optionally check **Explore: allow forward steps and retreat** to
   permit short translations in a clear area you have checked. Both checkboxes
   reset after starting/cancelling each search. Explore is off by default.
   **Movement speed** defaults to **Calibrated speed**; choose **Full speed · short
   bursts** for maximum servo output during search turns and Explore steps. This
   selection applies to one search and resets on completion or cancellation.
   Set **Search turns** (0–24, default 8), **Explore steps** (0–12, default 4),
   and **Moves per image** (1–3, default 3) before starting. These budgets count
   individual motor pulses, not API requests; steps share the forward/backward
   budget and require Explore enabled. Zero disables that category of search
   movement. Centring has its own fixed limit. Settings remain in the page for
   the next search, reset on page reload, and cannot change a running search.
4. Try **Find a blue water bottle** or **Find something red I could drink from**.
   The wheels stay stopped while the API interprets the clue. The page shows
   the understood request and search criteria, then searching starts automatically.
   An ambiguous clue prompts a spoken/on-screen question without moving. Re-enable
   turns and give a new, complete clue to answer it; search clues have no chat history.
5. The robot inspects a stationary image. GPT proposes an ordered sequence of
   up to **Moves per image** actions, using the image and up to eight recent
   observation/action records. For example: forward → backward → left.
   Turns are always available within their budget; Explore additionally permits
   forward and eligible retreats. Inspect or stop can be the final action.
   The page shows the plan, its current action number, and a brief reason.
   Each pulse stops and settles, but **no new image assessment happens between
   actions in a sequence**. After the sequence, it checks a fresh stationary
   image and replans. Choose 1 to restore a check after each movement. Larger
   sequences can miss objects passed between views; especially at full speed,
   supervise in a cleared area. History is not a map or measured position.
6. A possible match needs a second, newer stationary image to agree. The robot
   then turns left or right in **0.12-second slow steps**, stopping and settling
   before each new image. It aims to put the object's horizontal midpoint within
   the **middle third** of the camera view.
7. Two consecutive stationary images must agree that the target is near the
   centre. The robot stops, displays the final **matched snapshot**, and announces
   the find in one sentence using OpenAI's Marin voice. Check the picture yourself:
   two model checks can still make the same mistake.

### Search memory

Both search modes now keep a diary for the current search. Expand **Search memory**
in the result panel to see inspected thumbnails, evidence, elapsed time, completed
or interrupted timed movements, and likely revisits. The diary holds at most
30 checked views in RAM, with thumbnails no larger than 160×120; it writes no
images or history to disk. It resets automatically for each new search, including
a new spoken clue. **Clear memory** is available once the search has stopped.
The previous diary remains available after completion until cleared or replaced.

A local spatial-colour comparison suggests whether the current image resembles
an earlier view. Blank or low-detail images are not matched. GPT receives a
bounded text summary of relevant previous views and actual movement outcomes,
alongside the current image. It is instructed to try a different useful viewpoint
when an earlier sequence has revisited the same views, or stop when no useful
permitted alternative remains. Memory thumbnails are not uploaded as extra images.
These are visual hints, not measured positions, distances, a map or proof of
clearance. Similarity can miss revisits or mistake similar scenes for one another.
An interrupted command is never recorded as a completed movement.

Every stationary assessment still uses a fresh image, including both target
confirmation checks. In Live mode, only near-identical **stopped, settling**
background frames may skip an assessment following an absent result in the same
plan. A fresh background check is due within one second; movement, a changed view,
a new plan, or a foreground request bypasses this suppression. The website shows
skipped monitor frames separately from uploads and completed assessments. Upload
rate and both models are unchanged. Memory may reduce repeated search loops, but
its text summary also adds input tokens, so cost savings are not guaranteed.

Memory uses the system Pillow package already installed with this Pi's camera
environment. Regression checks use generated images and simulated hardware:
`python3 -B -m unittest test_search_memory test_search test_live_search` and
`node test_search_ui.js`. No paid API call is needed to run these tests.

### Live search and image rate

Choose **Look around → Search mode → Live search**, then choose **Images per
second**: **0.5, 1, 2 (default), or 4**. These settings apply to both typed and
spoken searches, are locked while searching, and reset to Snapshot search / 2
images per second on page reload. The mode does not arm the robot: the existing
movement checkbox, Explore permission, speed and per-search budgets still apply.

Live search uses a separate persistent `gpt-realtime` connection for the visual
search. It starts after the clue is understood and closes on completion,
cancellation or failure. The camera feed continues uploading while timed moves
run and while GPT is generating an assessment. Images are sampled at up to the
selected rate, with no unsent frame queue or catch-up bursts. The page reports
actual upload rate, images sent, completed assessments and the most recent
response latency. An uploaded image is not necessarily assessed: one assessment
runs at a time, and newer frames replace older views waiting for attention.
Higher image rates can increase bandwidth and API usage without speeding up GPT.

The session retains at most three recent image items plus an image pinned by an
active assessment. Each response explicitly references its assessed frame and
receives current search context; old image items are deleted rather than growing
an unbounded conversation. Only images and assessment text are used here; the
existing voice/clue and spoken-announcement features remain separate. No photos
or videos are written to disk. There is no local model download or new dependency.
The transport follows the official [Realtime image and conversation guide](https://developers.openai.com/api/docs/guides/realtime-conversations).

During movement and settling, background assessments may interrupt the current
sequence for a possible match, uncertainty or a stop request. They cannot authorize
new motion or count as a confirmed find. A possible match is rechecked in fresh
stationary images using the normal two-image confirmation and centring logic.
Interrupted forward pulses do not authorize retreat. Late observations are tied
to their search and plan, and observations over two seconds old cannot interrupt
a new movement. STOP, lost video, a disconnected browser, invalid responses and
API failures still end the search; a failed Live session never silently changes
to Snapshot mode. An API response timeout is eight seconds; motor pulses keep
their much shorter independent deadlines throughout.

Both modes retain the **90-second total deadline and 20 stationary assessments**.
Live mode additionally permits up to **20 background monitoring assessments**,
then stops if another is needed. Images sent and assessments are different
counters; image uploads are bounded by the selected rate and total deadline.
The robot can still pause waiting for GPT. This is interruptible short movement
with ongoing image uploads, not guaranteed instantaneous reaction or obstacle
avoidance. Choose Snapshot search to retain the original between-sequence checks.

The result panel reports whether centring was confirmed or stopped at a limit.
If the target becomes absent or uncertain, centring ends immediately without
resuming the search. An unclear position (including ambiguous multiple matches)
also prevents further turns. A still-visible object at a limit is reported as
found, with an explicit note that centring was not confirmed. A lost target is
not announced as a current find. Describe a distinctive, stationary object for
best results; this is approximate visual alignment, not precise tracking.
The server accounts for `--flip horizontal` / `--flip both` when choosing a
search or centring direction; the webpage's vertical flip does not change steering.

Explore uses the selected movement speed, with **0.40-second forward steps**
and **0.30-second backward retreats**, up to your selected **Explore steps** budget (default four).
The controller permits one retreat only after a completed forward step, within
10 seconds and with no intervening turn or retreat. A new forward step replaces
that opportunity. Backward distance is not measured and this does not guarantee
retracing the same path: the rear camera view is unavailable. Supervise on clear,
level floor away from stairs, edges, people and pets. Start with a wheel-direction
check with the wheels lifted; then measure a short step on the floor before
further exploration. There is no obstacle sensing or automatic approach to a find.

Full speed uses the existing Arduino uppercase commands, bypassing the four
saved wheel trim settings. Search turns remain 0.3 seconds, forward steps 0.40
seconds and retreats 0.30 seconds; the robot may travel farther per pulse.
Centring always uses the saved calibrated speed for precision. The independent
Arduino watchdog remains enabled. Search steps still have individual Pi-enforced deadlines. Choosing the option
does not move the robot; movement starts only with an explicitly armed search.
The running search shows **FULL SPEED** in its progress line. Check full-speed
wheel directions with the wheels raised before a supervised floor test.

Successful matches use one extra text-to-speech call (`gpt-4o-mini-tts`, Marin),
with the same API key. The sentence includes the target, image position and a
brief detail from the final confirmed view; for example, “I think I
found a red cup, on the right — a red ceramic cup sits on the desk.” The exact
sentence appears in the result panel. This is an AI-generated voice. Motors
stop before speech generation begins. Audio stays in RAM (at most 20 seconds,
under 1 MB); no downloads, recordings or extra packages are stored on the Pi.
Clarification questions use the same voice. STOP cancels generation/playback.
If audio is already in use the announcement
is skipped; if the speech API fails, the result remains visible with a voice
error. Unsuccessful searches and offline Coral descriptions retain local speech.
The implementation follows the official [OpenAI speech guide](https://developers.openai.com/api/docs/guides/text-to-speech).

The application defaults to **8 search turns** and, when Explore is enabled,
**4 forward/backward steps**. The website permits at most **24 search turns**
and **12 steps** per search; **6 centring turns** remain a separate fixed limit, with
**20 image checks and 90 seconds shared across the whole operation**, including
recording and clue interpretation. A turn
resets the consecutive-centred-image count; reaching a limit always stops movement. There is no full-circle or degree guarantee because this
robot does not measure wheel rotation. Its position may drift as it turns. It
stops travelling once a match is confirmed and has no obstacle avoidance.
With Explore off, only turns are permitted. If unsuccessful, it reports that it could not
confirm the object **from here**, rather than claiming the object is absent.

**STOP** or **Stop search** immediately releases the search's motor control.
Switching back to Conversation, changing browser tabs, losing focus, losing live
video, or disconnecting also cancels it. A browser heartbeat must arrive within
1.5 seconds; motor ownership additionally uses the existing controller lease.
Each turn has its own Pi-enforced deadline, and the existing independent Arduino
600 ms command timeout still applies if the Pi stops responding. Search owns the
motors exclusively, so manual driving and calibration cannot compete with it.
A late API response cannot resume cancelled motion or stop a newly claimed driver.
The program reads a new image only after stopping and settling. The clue interpreter
supplies validated search criteria or a clarification question; the vision model
supplies a match assessment, image position, description and proposed sequence.
In Snapshot mode, the array uses OpenAI [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
Local code validates the whole sequence, simulating budgets and retreat eligibility,
before its first pulse. It rechecks permissions, camera/browser freshness, the
search deadline and decision age before every action, then issues a fixed-duration
pulse at the user-selected speed. An invalid plan stops without executing a valid
prefix. Match/uncertain replies permit only inspect or stop; centring still looks
after each individual turn. Visual replies and remaining plan actions based on
images older than eight seconds are discarded without further movement. GPT cannot supply speeds, durations, new actions or turn limits.
Two stationary checks still confirm a match; local centring uses the model's
left/right position with shorter pulses. API errors stop the operation, without
falling back to an automatic sweep. Snapshot search keeps the wheels stopped during API calls; Live monitoring can run during bounded movements.

The typed clue or released microphone recording goes to OpenAI in a separate,
short-lived Realtime session. Recording uses the robot's microphones, not your
browser microphone, and shares the audio lock with Conversation and speech.
Stop active speech/conversation before recording a clue. Cancelled recordings
are not submitted; audio is capped at 720 KB and stays in RAM.
Each search makes one clue-interpretation request before any image checks.

The interpreted description and each checked JPEG go directly to OpenAI through the Responses
API with `store: false`, structured output, a short history and a 450-token output cap. That setting
does not override OpenAI's API data-retention policies. Search keeps only the
matched JPEG and small status fields after completion; no search photos or audio
are written to disk. Clue interpretation, image checks and cloud speech incur API
usage. Cancelling stops the robot immediately, but an already submitted request may still
incur a charge. Refusals, malformed output, API failures and timeouts stop the
search. API/model access errors are shown without exposing the key.

Leave off `--serial` for preview mode: search turns are simulated, so the camera
will keep facing the same physical direction. Normal chat remains conversational;
use the dedicated Look around tab to explicitly authorize a physical search.

The implementation follows the official [vision input guide](https://developers.openai.com/api/docs/guides/images-vision)
and [structured output guide](https://developers.openai.com/api/docs/guides/structured-outputs).
`test_search.py` exercises bounded turns, the two-image match check, fresh frames,
adaptive choices and history, exploration opt-in, retreat eligibility, independent
step deadlines, stale decisions, API errors, STOP races, disconnects, exclusive ownership and HTTP protection using
simulated serial hardware. `test_search_ui.js` checks explicit enable, stale video,
manual-control conflicts, cancel ordering, hold/release ordering and matched-image
display. `test_hunt.py` exercises typed/audio clue handoff, clarification without
motion, microphone cleanup/limits and cancellation during interpretation.
Real API checks interpreted a typed functional clue and a synthetic spoken object
request successfully, without opening the microphone, speaker or motor connection.
Chromium checks with simulated requests passed for search controls, matched-image
display and five desktop/phone viewport sizes (320–1440 pixels wide).
Physical turn coverage and search accuracy still need supervised testing on this robot.

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
on the servos, surface and load. The server requires **NB3-DEMO-6** firmware,
so re-upload the sketch when upgrading from the initial controller version.

## Compare slow and full speed

Keep both wheels raised off the ground for this comparison. Refresh the website,
choose **Full speed · hold to drive** under **Driving speed**, then **Take control**.
Briefly hold forward, release it, then try backward. Straight full-speed motion
uses `Servo.write(0)` and `Servo.write(180)`, matching the original remote-NB3
sketch's command range while retaining the corrected website directions.

Manual full-speed movement continues while you hold a direction and fresh
commands keep arriving; there is no two-second movement cap. The normal 500 ms
Pi / 600 ms Arduino disconnect timeouts still apply. Releasing the direction
stops immediately; STOP or loss of control also resets the selection to slow.
Choose full speed again before taking control after that reset. Search still
uses timed pulses (0.40 seconds forward, 0.30 seconds backward); increasing these
further requires accounting for both watchdogs, not simply changing a duration.
Install **NB3-DEMO-6** before restarting the website: older firmware retains
its two-second cutoff and is rejected by the new server.
The speed selector is locked while someone has control. Slow remains the default;
its default output and both servos' stop values are unchanged.

This feature requires the included **NB3-DEMO-6** firmware. Full-speed movement
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
The current speed and calibration options require uploading the NB3-DEMO-6 sketch.
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

On 2026-09-24, NB3-DEMO-6 was compiled and uploaded to this robot to remove
the manual full-speed cutoff. Its identification and STOP handshake passed; no
physical movement test was run. The prior flash image is saved at
`_tmp/robot-control/before-continuous-full-speed-20260924.hex`. The updated
software passed all 145 Python tests (including compiled firmware simulation)
and six JavaScript suites.

For other Nanos with the old bootloader, use `arduino:avr:nano:cpu=atmega328old`
for both commands. Stop anything using the serial port before uploading.

Stop the preview server with Ctrl+C, then run:

```bash
/usr/bin/python3 boxes/servers/python/robot_control/server.py --serial /dev/ttyUSB0
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
unavailable-camera UI. Python's standard library handles HTTP; the hardware
packages are the Pi's `picamera2` and `pyserial`. Realtime adds only
`websocket-client` in the ignored runtime directory.

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
