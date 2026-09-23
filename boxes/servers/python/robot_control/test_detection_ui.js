"use strict";
const assert = require("node:assert/strict");
const {DetectionOverlay, detectionBox} = require("./site/detection.js");
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
