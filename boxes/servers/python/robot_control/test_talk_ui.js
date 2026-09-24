"use strict";
const assert = require("node:assert/strict");
const {webcrypto} = require("node:crypto");
const {TalkControls} = require("./site/talk.js");
if (!global.crypto) global.crypto = webcrypto;
global.setInterval = () => {};

const settle = () => new Promise(resolve => setImmediate(resolve));
function fixture() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      handlers:{}, classList:{toggle() {}}, setAttribute() {}, setPointerCapture() {},
      addEventListener(name, fn) {this.handlers[name] = fn;}
    });
    return elements.get(id);
  };
  let state = {at:1, enabled:true, ready:true, busy:false, phase:"idle", text:"", error:"", turns:0};
  let startReply;
  const requests = [];
  const controls = new TalkControls(element, async (path, body) => {
    requests.push({path, body});
    if (path.endsWith("/start") || path.endsWith("/text")) return new Promise(resolve => {startReply = resolve;});
    if (path.endsWith("/finish")) state = {...state, at:state.at + 1, phase:"thinking"};
    if (path.endsWith("/cancel")) state = {...state, at:state.at + 1, busy:false, phase:"idle", turns:0};
    return {...state};
  });
  controls.render(state, true);
  return {element, controls, requests,
    ready() {state = {...state, at:state.at + 1, busy:true, phase:"recording"}; startReply({...state});}};
}

async function main() {
  {
    const f = fixture();
    f.element("talk-prompt").value = "What can you see?";
    const sending = f.controls.sendText();
    const count = f.requests.length;
    await f.controls.sendText();
    assert.equal(f.requests.length, count);
    assert.equal(f.requests[0].path, "/api/talk/text");
    assert.equal(f.requests[0].body.prompt, "What can you see?");
    assert.equal(f.controls.turn.held, false);
    assert.equal(f.element("send-prompt").disabled, true);
    f.ready();
    await sending;
    assert.equal(f.element("talk-prompt").value, "");
    await f.controls.heartbeat();
    assert.equal(f.requests.at(-1).path, "/api/talk/heartbeat");
    await f.controls.cancel(true);
    assert.equal(f.requests.at(-1).path, "/api/talk/cancel");
    assert.equal(f.requests.some(r => r.path.endsWith("/start") || r.path.endsWith("/finish")), false);
  }
  {
    const f = fixture();
    for (const value of [" ", "x".repeat(501)]) {
      f.element("talk-prompt").value = value;
      await f.controls.sendText();
    }
    assert.equal(f.requests.length, 0);
    f.element("talk-prompt").value = "Hello";
    let prevented = false;
    const key = {key:"Enter", shiftKey:true, preventDefault() {prevented = true;}};
    f.element("talk-prompt").handlers.keydown(key);
    f.element("talk-prompt").handlers.keydown({...key,shiftKey:false,isComposing:true});
    assert.equal(prevented, false);
    assert.equal(f.requests.length, 0);
    f.element("talk-prompt").handlers.keydown({...key,shiftKey:false});
    assert.equal(prevented, true);
    assert.equal(f.requests[0].path, "/api/talk/text");
    f.ready();
    await settle();
  }
  {
    const {element, controls, requests} = fixture();
    controls.render({at:2, enabled:true, ready:false, error:"API key not configured yet"}, true);
    assert.equal(element("talk").disabled, true);
    assert.match(element("talk-status").textContent, /API key/);
    await controls.start(1);
    assert.equal(requests.length, 0);
  }
  {
    const f = fixture();
    const start = f.controls.start(1);
    const finish = f.controls.finish();
    assert.deepEqual(f.requests.map(r => r.path), ["/api/talk/start"]);
    f.ready();
    await start;
    await finish;
    assert.deepEqual(f.requests.map(r => r.path), ["/api/talk/start", "/api/talk/finish"]);
    assert.equal(f.requests[0].body.token, f.requests[1].body.token);
    assert.equal(f.element("talk").disabled, true);
    await f.controls.heartbeat();
    assert.equal(f.requests.at(-1).path, "/api/talk/heartbeat");
    f.controls.render({at:5, enabled:true, ready:true, busy:false, phase:"idle", text:"A cup.", turns:1}, true);
    assert.match(f.element("talk-reply").textContent, /A cup/);
    assert.equal(f.element("talk").disabled, false);
    const count = f.requests.length;
    await f.controls.heartbeat();
    assert.equal(f.requests.length, count);
    // An older poll cannot erase a newer state.
    f.controls.render({at:4, enabled:true, ready:false, busy:true}, true);
    assert.equal(f.element("talk").disabled, false);
    assert(f.requests.every(r => r.path.startsWith("/api/talk/")));
  }
  {
    const f = fixture();
    const start = f.controls.start(1);
    const finish = f.controls.finish();
    await f.controls.cancel();
    f.ready();
    await start;
    await finish;
    assert.equal(f.requests.some(r => r.path.endsWith("/finish")), false);
    assert.equal(f.controls.turn.cancelled, true);
    assert.equal(f.controls.turn.held, false);
  }
  {
    const f = fixture();
    f.element("talk").handlers.pointerdown({button:0, pointerId:7, preventDefault() {}});
    f.ready();
    await settle();
    f.element("talk").handlers.pointercancel({pointerId:7});
    await settle();
    assert.equal(f.requests.at(-1).path, "/api/talk/cancel");
    assert.equal(f.requests.some(r => r.path.endsWith("/finish")), false);
  }
  {
    const f = fixture();
    f.element("talk").handlers.keydown({key:"Enter", repeat:false, preventDefault() {}});
    f.ready();
    await settle();
    f.element("talk").handlers.keyup({key:"Enter", preventDefault() {}});
    await settle();
    assert.equal(f.requests.at(-1).path, "/api/talk/finish");
    f.controls.render(undefined, false);
    await settle();
    assert.equal(f.requests.at(-1).path, "/api/talk/cancel");
  }
  console.log("Talk UI passed: missing key, quick release, cancellation, heartbeat, keyboard, stale replies and no motor requests.");
}
main().catch(error => {console.error(error); process.exitCode = 1;});
