"use strict";
const assert = require("node:assert/strict");
const {DetectionControls, DetectionOverlay, detectionBox} = require("./site/detection.js");
assert.deepEqual(detectionBox([.1, .2, .4, .6], false, 100, 100).map(Math.round), [10,20,30,40]);
assert.deepEqual(detectionBox([.1, .2, .4, .6], true, 100, 100).map(Math.round), [10,40,30,40]);
let rectangles = [], text = [];
const context = {
  clearRect() { rectangles = []; text = []; },
  strokeRect(...args) { rectangles.push(args); },
  fillRect() {}, fillText(label) { text.push(label); },
  measureText(label) { return {width: label.length * 8}; }
};
const badge = {classList: {add() {}}}, summary = {};
const overlay = new DetectionOverlay({width:640, height:480, getContext: () => context}, badge, summary);
overlay.update("live", {inference_ms: 8, objects: [{label:"cup", score:.82, box:[.1,.2,.4,.6]}]});
overlay.render(true, true, false);
assert.equal(rectangles.length, 1);
assert.equal(text[0], "cup 82%");
assert.match(summary.textContent, /cup × 1/);
overlay.render(true, false, false);
assert.equal(rectangles.length, 0);
assert.match(badge.textContent, /Waiting/);
overlay.render(false, true, false);
assert.equal(rectangles.length, 0);
assert.match(badge.textContent, /hidden/);
overlay.update("unavailable", null);
overlay.render(true, true, false);
assert.equal(rectangles.length, 0);
assert.match(badge.textContent, /unavailable/);
overlay.update("live", {inference_ms: 8, objects: []});
overlay.render(true, true, false);
assert.match(summary.textContent, /No objects detected/);
console.log("Detection UI checks passed: geometry, flip, labels, counts, hidden/stale/unavailable states.");

async function controlsTest() {
  const checkbox = {checked:true, addEventListener(type, handler) {this[type] = handler;}};
  let resolve, reject;
  const requests = [];
  const control = new DetectionControls(checkbox, (path, body) => {
    requests.push({path, body});
    return new Promise((yes, no) => {resolve = yes; reject = no;});
  }, () => control.render(true));
  control.update({available:true, enabled:true, running:true, at:1});
  control.render(true);
  checkbox.checked = false;
  const pending = checkbox.change();
  assert.equal(checkbox.disabled, true);
  assert.deepEqual(requests, [{path:'/api/detections', body:{enabled:false}}]);
  overlay.render(false, true, false, control);
  assert.match(badge.textContent, /Updating/);
  await checkbox.change();
  assert.equal(requests.length, 1);
  resolve({available:true, enabled:false, running:true, at:3});
  await pending;
  overlay.render(false, true, false, control);
  assert.match(badge.textContent, /Pausing/);
  control.update({available:true, enabled:true, running:true, at:2});
  control.render(true);
  assert.equal(checkbox.checked, false, 'Old poll must not undo a pause');
  control.update({available:true, enabled:false, running:false, at:4});
  overlay.render(false, true, false, control);
  assert.equal(badge.textContent, 'NPU paused');
  assert.equal(rectangles.length, 0);
  checkbox.checked = true;
  const failed = checkbox.change();
  reject(new Error('offline'));
  await failed;
  assert.equal(checkbox.checked, false);
  overlay.render(false, true, false, control);
  assert.match(summary.textContent, /Could not change/);
  // Another viewer's successful change is reflected without sending a request.
  control.update({available:true, enabled:true, running:true, at:5});
  control.render(true);
  assert.equal(checkbox.checked, true);
  assert.equal(requests.length, 2);
  control.render(false);
  assert.equal(checkbox.disabled, true);
  overlay.render(false, true, false, control);
  assert.equal(badge.textContent, 'NPU status unknown');
  control.update({available:false, enabled:false});
  control.render(true);
  assert.equal(checkbox.disabled, true);
  console.log('NPU checkbox checks passed: pause, resume failure, shared state, stale replies and disabled controls.');
}
controlsTest().catch(error => {console.error(error); process.exitCode = 1;});
