"use strict";
const assert = require("node:assert/strict");
const {SearchControls} = require("./site/search.js");
if (!global.crypto) global.crypto = require("node:crypto").webcrypto;
global.setInterval = () => {};
const settle = () => new Promise(resolve => setImmediate(resolve));

function fixture() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {value:"", checked:false, dataset:{}, handlers:{},
      addEventListener(name, fn) {this.handlers[name] = fn;}, setAttribute() {}, setPointerCapture() {}});
    return elements.get(id);
  };
  let state = {at:1, enabled:true, ready:true, clues:true, voice_ready:true, max_clue:500,
    busy:false, phase:"idle", turns:0, checks:0, max_turns:8, max_checks:20};
  const context = {fresh:true, frameTime:123, driving:false, stopGeneration:7};
  const requests = [];
  let resolveStart;
  const api = async (path, body) => {
    requests.push({path, body});
    if (path.endsWith('/start') || path.endsWith('/listen')) return new Promise(resolve => {resolveStart = resolve;});
    if (path.endsWith('/cancel')) state = {...state, busy:false, phase:'cancelled', message:'Search stopped.'};
    return {...state, at:++state.at};
  };
  const controls = new SearchControls(element, api, () => context, () => api('/api/stop', {}));
  controls.render(state, true);
  return {element, context, controls, requests,
    arm() {element('search-target').value = 'a red bottle'; element('search-arm').checked = true; controls.render(undefined, true);},
    accept() {state = {...state, at:++state.at, busy:true, phase:'looking'}; resolveStart({...state});}};
}

async function main() {
  {
    const f = fixture();
    f.arm();
    const started = f.controls.start(true, 7);
    const finished = f.controls.finishRecording();
    assert.deepEqual(f.requests.map(r => r.path), ['/api/search/listen']);
    assert.equal(f.requests[0].body.target, undefined, 'Voice request does not reuse typed clue');
    f.accept(); await started; await finished;
    assert.deepEqual(f.requests.map(r => r.path), ['/api/search/listen', '/api/search/finish']);
    assert.equal(f.controls.job.held, false);
    await f.controls.heartbeat();
    assert.equal(f.requests.at(-1).path, '/api/search/heartbeat');
  }
  {
    const f = fixture();
    await f.controls.start(true, 7);
    assert.equal(f.requests.length, 0, 'Voice needs the same one-shot movement enable');
    f.arm();
    const started = f.controls.start(true, 7);
    const finished = f.controls.finishRecording();
    await f.controls.cancel();
    f.accept(); await started; await finished;
    assert.equal(f.requests.some(r => r.path.endsWith('/finish')), false);
    assert.equal(f.controls.job.active, false);
  }
  {
    const f = fixture();
    f.arm();
    f.element('search-voice').handlers.pointerdown({button:0, pointerId:4, preventDefault() {}});
    f.accept(); await settle();
    f.element('search-voice').handlers.pointercancel({}); await settle();
    assert.equal(f.requests.at(-1).path, '/api/search/cancel');
    assert.equal(f.controls.job.held, false);
  }
  {
    const f = fixture();
    f.arm();
    f.element('search-voice').handlers.keydown({key:'Enter', repeat:false, preventDefault() {}});
    f.accept(); await settle();
    f.element('search-voice').handlers.keyup({key:'Enter', preventDefault() {}}); await settle();
    assert.equal(f.requests.at(-1).path, '/api/search/finish');
    f.controls.render({at:100, enabled:true, ready:true, voice_ready:true, busy:false,
      phase:'clarify', request_text:'Find that thing.', message:'What does it look like?'}, true);
    assert.equal(f.controls.job.active, false);
    assert.match(f.element('search-result').textContent, /What does/);
    assert.match(f.element('search-request').textContent, /Find that thing/);
    const count = f.requests.length;
    await f.controls.heartbeat();
    assert.equal(f.requests.length, count, 'Clarification never automatically restarts');
  }
  {
    const f = fixture();
    f.controls.render({at:100, enabled:true, ready:true, busy:true, phase:'centering',
      message:'Centring: turning left briefly…', turns:2, max_turns:8,
      center_turns:1, max_center_turns:6, checks:5, max_checks:20}, true);
    assert.match(f.element('search-result').textContent, /turning left/);
    assert.match(f.element('search-progress').textContent, /1 \/ 6 centring turns/);
    assert.equal(f.element('start-search').disabled, true);
    assert.equal(f.element('cancel-search').disabled, false);
    f.controls.render({at:101, enabled:true, ready:true, busy:false, phase:'found',
      centered:false, centering_note:'Found, but the centring turn limit was reached; stopped.'}, true);
    assert.match(f.element('search-alignment').textContent, /limit/);
  }
  {
    const f = fixture();
    f.element('search-target').value = 'bottle';
    await f.controls.start();
    assert.equal(f.requests.length, 0, 'Searching needs explicit turn enable');
    f.arm();
    f.context.driving = true;
    await f.controls.start();
    assert.equal(f.requests.length, 0, 'Cannot take over a manual driver');
    f.context.driving = false;
    f.context.fresh = false;
    await f.controls.start();
    assert.equal(f.requests.length, 0, 'Cannot start with stale video');
  }
  {
    const f = fixture();
    f.arm();
    const started = f.controls.start();
    assert.equal(f.element('search-arm').checked, false, 'Enable is one-shot');
    assert.equal(f.requests[0].path, '/api/search/start');
    assert.equal(f.requests[0].body.stop_generation, 7);
    assert.equal(f.requests[0].body.frame_time, 123);
    assert.equal(f.requests[0].body.allow_turns, true);
    await f.controls.start();
    assert.equal(f.requests.length, 1, 'No duplicate starts');
    f.accept();
    await started;
    await f.controls.heartbeat();
    assert.equal(f.requests.at(-1).path, '/api/search/heartbeat');
    f.context.fresh = false;
    await f.controls.heartbeat();
    assert.equal(f.requests.at(-1).path, '/api/search/cancel');
    assert.equal(f.requests.some(r => r.path === '/api/control'), false);
  }
  {
    const f = fixture();
    f.arm();
    const started = f.controls.start();
    await f.controls.cancel();
    f.accept();
    await started;
    assert.equal(f.controls.job.active, false);
    const count = f.requests.length;
    await f.controls.heartbeat();
    assert.equal(f.requests.length, count, 'Cancelled late start never renews its lease');
  }
  {
    const f = fixture();
    f.arm();
    const started = f.controls.start();
    f.accept(); await started;
    f.controls.render({at:100, enabled:true, ready:true, busy:false, phase:'found',
      message:'Likely bottle on the left.', evidence:'A red bottle.', result_id:1}, true);
    assert.equal(f.element('search-match').hidden, false);
    assert.equal(f.element('search-match').src, '/api/search/frame?result=1');
    assert.match(f.element('search-result').textContent, /bottle/);
    assert.equal(f.element('search-arm').checked, false);
    f.controls.select(true);
    assert.equal(f.element('conversation-controls').hidden, true);
    assert.equal(f.element('search-output').hidden, false);
    f.controls.select(false);
    assert.equal(f.element('search-controls').hidden, true);
    await f.element('cancel-search').handlers.click();
    assert.equal(f.requests.at(-1).path, '/api/stop');
    await settle();
  }
  console.log('Search UI passed: explicit enable, driver/video guards, STOP, cancel races, heartbeat and matched image.');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
