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
  element('search-mode').value = 'snapshot';
  element('search-image-rate').value = '2';
  element('search-max-turns').value = '8';
  element('search-max-steps').value = '4';
  element('search-sequence').value = '3';
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
    const node = () => ({children:[], dataset:{}, setAttribute() {},
      append(...items) {this.children.push(...items);}, replaceChildren(...items) {this.children = items;}});
    global.document = {createElement:node};
    const f = fixture();
    const list = f.element('search-memory-entries');
    list.replaceChildren = function(...items) {this.children = items;};
    const memory = {revision:1, views:1, repeats:1, entries:[{check:7, view:2, seconds:3.1,
      decision:'absent', repeat:true, thumbnail:true, evidence:'<img src=x onerror=alert(1)>',
      departures:[{action:'left', seconds:.3, outcome:'interrupted'}], next_view:3}]};
    f.controls.render({...f.controls.state, at:20, memory}, true);
    assert.equal(list.children.length, 1);
    assert.equal(list.children[0].children[0].src, '/api/search/memory/frame?check=7');
    assert.equal(list.children[0].children[1].children[1].textContent, memory.entries[0].evidence,
      'Model descriptions are rendered as text, never HTML');
    assert.match(list.children[0].children[1].children[2].textContent, /interrupted/);
    const card = list.children[0];
    f.controls.render(undefined, true);
    assert.equal(list.children[0], card, 'Polling must not reload unchanged thumbnails');
    assert.equal(f.element('search-memory-clear').disabled, false);
    await f.controls.clearMemory();
    assert.equal(f.requests.at(-1).path, '/api/search/memory/clear');
    f.controls.render({...f.controls.state, at:21, busy:true, memory}, true);
    const count = f.requests.length;
    await f.controls.clearMemory();
    assert.equal(f.requests.length, count);
    assert.equal(f.element('search-memory-clear').disabled, true);
    f.controls.render({...f.controls.state, at:22, busy:false,
      memory:{revision:2, views:0, repeats:0, entries:[]}}, true);
    assert.equal(list.children.length, 0);
    assert.equal(f.element('search-memory-clear').disabled, true);
  }
  for (const [id, invalid] of [['search-max-turns', '25'], ['search-max-steps', '-1'],
      ['search-sequence', '4'], ['search-max-turns', '1.5'], ['search-max-steps', ''], ['search-mode', 'invalid'], ['search-image-rate', '9'], ['search-image-rate', '']]) {
    const f = fixture(); f.arm(); f.element(id).value = invalid;
    f.controls.render(undefined, true);
    assert.equal(f.element('start-search').disabled, true);
    assert.equal(f.element('search-voice').disabled, true);
    await f.controls.start(); await f.controls.start(true);
    assert.equal(f.requests.length, 0);
  }
  for (const voice of [false, true]) {
    const f = fixture();
    f.arm(); f.element('search-explore').checked = true;
    f.element('search-mode').value = 'live';
    f.element('search-image-rate').value = '4';
    f.element('search-speed').value = 'full';
    f.element('search-max-turns').value = '18';
    f.element('search-max-steps').value = '8';
    f.element('search-sequence').value = '2';
    const started = f.controls.start(voice, 3);
    assert.equal(f.requests[0].body.live, true);
    assert.equal(f.requests[0].body.image_rate, 4);
    assert.equal(f.element('search-mode').disabled, true);
    assert.equal(f.element('search-image-rate').disabled, true);
    assert.equal(f.requests[0].body.explore, true);
    assert.equal(f.requests[0].body.speed, 'full');
    assert.equal(f.requests[0].body.max_turns, 18);
    assert.equal(f.requests[0].body.max_steps, 8);
    assert.equal(f.requests[0].body.sequence_length, 2);
    assert.equal(f.element('search-max-turns').disabled, true);
    assert.equal(f.element('search-speed').disabled, true);
    assert.equal(f.element('search-explore').checked, false, 'Explore permission is one-shot');
    assert.equal(f.element('search-explore').disabled, true);
    f.accept(); await started;
    f.controls.render({at:100, enabled:true, ready:true, busy:true, explore:true,
      live:true, image_rate:4, live_status:{images_sent:12,assessments:3,actual_image_rate:3.9,latency:1.2},
      speed:'full', plan:['forward', 'left'], plan_index:1, action:'forward', action_reason:'Inspect a different view.', steps:1, max_steps:4}, true);
    assert.match(f.element('search-live-status').textContent, /12 images sent · 3 assessments/);
    assert.match(f.element('search-plan').textContent, /forward → left \(1\/2\)/);
    assert.match(f.element('search-progress').textContent, /1 \/ 4 steps/);
    assert.match(f.element('search-progress').textContent, /FULL SPEED/);
    await f.controls.cancel();
    assert.equal(f.element('search-explore').checked, false);
    assert.equal(f.element('search-speed').value, 'slow');
  }
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
    assert.equal(f.requests[0].body.explore, false, 'Forward/backward must be explicitly enabled');
    assert.equal(f.requests[0].body.live, false);
    assert.equal(f.requests[0].body.image_rate, 2);
    assert.equal(f.requests[0].body.speed, 'slow', 'Full speed must be explicitly selected');
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
