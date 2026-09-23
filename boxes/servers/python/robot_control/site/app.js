"use strict";
const $ = id => document.getElementById(id);
const directions = [...document.querySelectorAll("[data-direction]")];
let token = null, sequence = 0, desired = "stop", online = false, claiming = false;
let state = null, lastVideo = 0, frameTime = 0, frameNumber = 0, objectURL = null;
const steering = new SteeringInput();
let controlBusy = false;
const calibrationKeys = ["left_forward", "right_forward", "left_backward", "right_backward"];
let calibrationDirty = false, calibrationSaving = false;
const fresh = () => performance.now() - lastVideo < 1000 && lastVideo > 0;

async function api(path, body, keepalive = false) {
  const options = {cache: "no-store", signal: AbortSignal.timeout(1200)};
  if (body !== undefined) Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json", "X-Robot-Control": "1"}, body: JSON.stringify(body), keepalive});
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Robot not responding");
  return data;
}

function render() {
  $("connection").textContent = online ? "Robot connected" : "Disconnected";
  $("connection").className = `badge ${online ? "good" : "bad"}`;
  $("live").textContent = fresh() ? "Live" : "Video paused";
  $("live").className = `badge ${fresh() ? "good" : "bad"}`;
  $("video-message").hidden = fresh();
  $("video-message").textContent = online ? (state?.camera_error || "Waiting for fresh video…") : "Connection lost. Movement has stopped.";
  $("camera").parentElement.classList.toggle("stale", !fresh());
  $("claim").disabled = !online || claiming || calibrationSaving || calibrationDirty || Boolean(state?.fault) || (!token && (state?.busy || !fresh()));
  $("claim").textContent = token ? "Release control" : (state?.busy ? "Another driver has control" : "Take control");
  $("claim").classList.toggle("owned", Boolean(token));
  $("speed").disabled = !online || claiming || Boolean(token) || Boolean(state?.busy);
  $("calibration-fields").disabled = !online || claiming || calibrationSaving || Boolean(token) || Boolean(state?.busy);
  if (state?.calibration && !calibrationDirty && !calibrationSaving) {
    for (const key of calibrationKeys) $(key).value = state.calibration[key];
  }
  for (const button of directions) {
    button.disabled = !online || !token || !fresh();
    button.classList.toggle("active", desired.split("_").includes(button.dataset.direction));
  }
  if (state) {
    $("mode").textContent = state.mode === "preview" ? "Preview mode · motor output disabled" : (state.fault ? "Arduino disconnected" : (state.speed === "full" ? "Arduino connected · FULL SPEED · 2-second limit" : "Arduino connected · slow speed"));
    $("command").textContent = online ? state.command.replaceAll("_", " ") : "Unknown";
  }
}

function clearInput() {
  desired = "stop";
  steering.clear();
  $("speed").value = "slow";
  render();
}

async function sendControl(force = false) {
  if (!token || (controlBusy && !force)) return;
  const currentToken = token;
  if (desired !== "stop" && !fresh()) return emergencyStop("Video paused. Take control again once it returns.");
  const body = {token, sequence: ++sequence, command: desired, frame_time: frameTime};
  controlBusy = true;
  try {
    const result = await api("/api/control", body);
    if (token === currentToken && body.sequence === sequence) {
      state = result;
      $("notice").textContent = state.reason;
    }
  } catch (error) {
    // A superseded request must not invalidate a newer stop or driver session.
    if (token === currentToken && body.sequence === sequence) {
      token = null;
      clearInput();
      $("notice").textContent = error.message;
      // Server and Arduino watchdogs stop even when this request cannot arrive.
      api("/api/stop", {}, true).catch(() => {});
    }
  } finally {
    controlBusy = false;
    render();
  }
}

function updateDirection() {
  const next = steering.command;
  if (next === desired) return;
  desired = next;
  render();
  // Releases and steering changes supersede any heartbeat already in flight.
  sendControl(true);
}

async function emergencyStop(message = "Stopped. Take control when you’re ready.") {
  token = null;
  clearInput();
  $("notice").textContent = message;
  try {state = await api("/api/stop", {}, true);} catch (_) { /* Watchdogs are independent of the browser. */ }
  render();
}

$("claim").addEventListener("click", async () => {
  if (token) return emergencyStop("Control released. The next driver can take over.");
  claiming = true;
  render();
  try {
    const speed = $("speed").value;
    const result = await api("/api/claim", {speed});
    token = result.token;
    sequence = 0;
    $("notice").textContent = speed === "full" ? "Full-speed test ready. Keep wheels raised; briefly hold forward or backward." : "You have control. Hold ↑ + → to curve right.";
    sendControl(true);
  } catch (error) {$("notice").textContent = error.message;}
  finally {claiming = false; render();}
});

for (const key of calibrationKeys) $(key).addEventListener("input", () => {
  calibrationDirty = true;
  $("calibration-notice").textContent = "Save your changes before taking control.";
  render();
});
$("reset-calibration").addEventListener("click", () => {
  for (const key of calibrationKeys) $(key).value = 12;
  calibrationDirty = true;
  $("calibration-notice").textContent = "Defaults selected. Save to apply them.";
  render();
});
$("apply-calibration").addEventListener("click", async () => {
  const values = Object.fromEntries(calibrationKeys.map(key => [key, Number($(key).value)]));
  if (Object.values(values).some(value => !Number.isInteger(value) || value < 6 || value > 24)) {
    $("calibration-notice").textContent = "Use whole numbers from 6 to 24.";
    return;
  }
  calibrationSaving = true;
  render();
  try {
    state = await api("/api/calibration", values);
    calibrationDirty = false;
    $("calibration-notice").textContent = "Saved on the robot. Select slow speed and take control to test.";
  } catch (error) {$("calibration-notice").textContent = error.message;}
  finally {calibrationSaving = false; render();}
});

for (const button of directions) {
  button.addEventListener("pointerdown", event => {
    if (event.button !== 0 || !online || !token || !fresh()) return;
    event.preventDefault();
    steering.pointers.set(event.pointerId, button.dataset.direction);
    button.setPointerCapture(event.pointerId);
    updateDirection();
  });
  for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) {
    button.addEventListener(name, event => {
      if (steering.pointers.delete(event.pointerId)) updateDirection();
    });
  }
  button.addEventListener("contextmenu", event => event.preventDefault());
}

window.addEventListener("keydown", event => {
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (key === " " || key === "Escape") {
    event.preventDefault();
    if (!event.repeat) emergencyStop();
    return;
  }
  if (!SteeringInput.keys[key] || event.ctrlKey || event.metaKey || event.altKey) return;
  if (["SELECT", "INPUT", "TEXTAREA"].includes(event.target?.tagName)) return;
  event.preventDefault();
  if (event.repeat || !online || !token || !fresh()) return;
  steering.keys.add(key);
  updateDirection();
});
window.addEventListener("keyup", event => {
  const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
  if (steering.keys.delete(key)) updateDirection();
});
$("stop").addEventListener("click", () => emergencyStop());
$("flip").addEventListener("click", () => {
  const flipped = $("camera").classList.toggle("flipped");
  $("flip").textContent = flipped ? "Reset orientation" : "Flip vertically";
});
window.addEventListener("blur", () => {if (token) emergencyStop("Window lost focus. Take control to continue.");});
document.addEventListener("visibilitychange", () => {if (document.hidden && token) emergencyStop("Control released while away.");});
window.addEventListener("pagehide", () => {if (token) emergencyStop();});

async function statusLoop() {
  try {
    state = await api("/api/status");
    online = true;
    if (token && (!state.busy || state.fault)) {
      token = null;
      clearInput();
      $("notice").textContent = state.reason;
    }
    if (!token && !claiming) $("notice").textContent = state.fault || (state.busy ? "You can watch, or press Stop at any time." : "Take control to try the direction buttons.");
  } catch (_) {
    online = false;
    if (token) emergencyStop("Connection lost. Reconnect and take control again.");
  }
  render();
  setTimeout(statusLoop, 500);
}

async function videoLoop() {
  let nextURL = null;
  try {
    if (!document.hidden) {
      const response = await fetch("/api/frame", {cache: "no-store", signal: AbortSignal.timeout(1500)});
      if (!response.ok) throw new Error("No video");
      const number = Number(response.headers.get("X-Frame-Number"));
      if (number !== frameNumber) {
        const blob = await response.blob();
        nextURL = URL.createObjectURL(blob);
        // Decode before replacing the displayed image to avoid flicker.
        const decoded = new Image();
        decoded.src = nextURL;
        await decoded.decode();
        $("camera").src = nextURL;
        $("camera").hidden = false;
        if (objectURL) URL.revokeObjectURL(objectURL);
        objectURL = nextURL;
        nextURL = null;
        frameNumber = number;
        frameTime = Number(response.headers.get("X-Frame-Time"));
        lastVideo = performance.now();
      } else {
        await response.body.cancel();
      }
    }
  } catch (_) {if (nextURL) URL.revokeObjectURL(nextURL);}
  render();
  setTimeout(videoLoop, 80);
}

setInterval(() => {
  if (token && desired !== "stop" && !fresh()) emergencyStop("Video paused. Movement stopped.");
  else sendControl();
  render();
}, 150);
statusLoop();
videoLoop();
