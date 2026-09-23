"use strict";
// Exercise the actual browser handlers with an in-memory API; no robot access.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor() {
    this.listeners = {};
    this.classList = {toggle() { return false; }, contains() { return false; }, add() {}};
    this.parentElement = {classList: this.classList};
    this.dataset = {};
    this.value = "slow";
  }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setPointerCapture() {}
  getContext() { return {clearRect() {}, strokeRect() {}, fillRect() {}, fillText() {}, measureText() { return {width: 20}; }}; }
}

async function main() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const buttons = ["forward", "backward", "left", "right"].map(direction => {
    const button = new Element();
    button.dataset.direction = direction;
    return button;
  });
  const window = new Element();
  const requests = [];
  const defaults = {left_forward: 12, right_forward: 12, left_backward: 12, right_backward: 12};
  let status = {mode: "robot", speed: "slow", busy: false, command: "stop", camera_live: true, calibration: defaults};
  const context = vm.createContext({
    document: {getElementById: element, querySelectorAll: () => buttons, addEventListener() {}, hidden: false},
    window, performance: {now: () => 1000}, AbortSignal,
    setTimeout() {}, setInterval() {},
    fetch: async (path, options) => {
      if (path.startsWith("/api/frame")) return {ok: false};
      const body = options.body === undefined ? undefined : JSON.parse(options.body);
      requests.push({path, body});
      let reply;
      if (path === "/api/claim") {
        status = {...status, busy: true, speed: body.speed};
        reply = {token: "test-driver"};
      } else {
        if (path === "/api/control") status = {...status, command: body.command};
        if (path === "/api/stop") status = {...status, busy: false, command: "stop", speed: "slow"};
        if (path === "/api/calibration") status = {...status, calibration: body};
        reply = {...status};
      }
      return {ok: true, json: async () => reply};
    },
  });
  for (const filename of ["steering.js", "detection.js", "speech.js", "app.js"])
    vm.runInContext(fs.readFileSync(`${__dirname}/site/${filename}`, "utf8"), context);
  const settle = () => new Promise(resolve => setImmediate(resolve));
  await settle();
  vm.runInContext("lastVideo = 1000; frameTime = 1; render()", context);
  assert.equal(element("speed").value, "slow");
  assert.equal(element("speed").disabled, false);
  element("speed").value = "full";
  assert.equal(requests.some(request => request.path === "/api/control"), false);
  await element("claim").listeners.click();
  await settle();
  assert.deepEqual(requests.find(request => request.path === "/api/claim").body, {speed: "full"});
  assert.equal(element("speed").disabled, true);
  assert.equal(element("calibration-fields").disabled, true);
  assert.match(element("mode").textContent, /FULL SPEED/);
  assert.equal(status.command, "stop");
  buttons[0].listeners.pointerdown({button: 0, pointerId: 1, preventDefault() {}});
  await settle();
  assert.equal(status.command, "forward");
  buttons[0].listeners.pointerup({pointerId: 1});
  await settle();
  assert.equal(status.command, "stop");
  assert.equal(status.speed, "full");
  // A server-enforced test limit releases control and resets the selector.
  status = {...status, busy: false, speed: "slow", reason: "Full-speed test finished"};
  await vm.runInContext("statusLoop()", context);
  assert.equal(element("speed").value, "slow");
  assert.equal(element("speed").disabled, false);
  assert.equal(buttons[0].disabled, true);
  // The next session must explicitly select full speed again.
  await element("claim").listeners.click();
  await settle();
  assert.equal(status.speed, "slow");
  window.listeners.blur();
  await settle();
  assert.equal(status.busy, false);
  assert.equal(status.command, "stop");
  element("speed").value = "full";
  await element("claim").listeners.click();
  await settle();
  await element("stop").listeners.click();
  assert.equal(status.command, "stop");
  assert.equal(status.speed, "slow");
  assert.equal(element("speed").value, "slow");
  assert.equal(element("calibration-fields").disabled, false);
  const movementsBeforeSave = requests.filter(request => request.path === "/api/control").length;
  element("left_forward").value = "30";
  element("left_forward").listeners.input();
  assert.equal(element("claim").disabled, true);
  await element("apply-calibration").listeners.click();
  assert.equal(requests.some(request => request.path === "/api/calibration"), false);
  element("left_forward").value = "11";
  element("right_backward").value = "11";
  await element("apply-calibration").listeners.click();
  assert.deepEqual(status.calibration, {...defaults, left_forward: 11, right_backward: 11});
  assert.equal(element("claim").disabled, false);
  assert.equal(requests.filter(request => request.path === "/api/control").length, movementsBeforeSave);
  element("reset-calibration").listeners.click();
  assert.equal(element("claim").disabled, true);
  await element("apply-calibration").listeners.click();
  assert.deepEqual(status.calibration, defaults);
  console.log("Full-speed UI checks passed: selection, claim, hold/release, limit, focus loss, Stop, slow default.");
  console.log("Calibration UI checks passed: defaults, stopped-only editing, validation, save without movement, reset.");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
