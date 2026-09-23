"use strict";
const assert = require("node:assert/strict");
const {SpeechControls} = require("./site/speech.js");

async function main() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {addEventListener(name, fn) { this[name] = fn; }});
    return elements.get(id);
  };
  let reply = {enabled:true, ready:true, speaking:false, text:"", error:""};
  let fail = false;
  const requests = [];
  const controls = new SpeechControls(element, async (path, body) => {
    requests.push({path, body});
    if (fail) throw new Error("Wait for fresh Coral detections");
    return reply;
  });
  controls.render(null, false);
  assert.equal(element("describe").disabled, true);
  controls.render(reply, true);
  assert.equal(element("describe").disabled, false);
  assert.equal(element("stop-speaking").disabled, true);
  reply = {...reply, speaking:true, text:"I think I can see a cup."};
  await element("describe").click();
  assert.equal(element("describe").disabled, true);
  assert.equal(element("stop-speaking").disabled, false);
  assert.match(element("spoken-description").textContent, /a cup/);
  reply = {...reply, speaking:false};
  await element("stop-speaking").click();
  assert.equal(element("stop-speaking").disabled, true);
  fail = true;
  await element("describe").click();
  assert.match(element("speech-status").textContent, /fresh Coral/);
  controls.render({...reply, ready:false, error:"NB3 mouth not available"}, true);
  assert.equal(element("describe").disabled, true);
  assert.match(element("speech-status").textContent, /mouth not available/);
  reply = {...reply, llm_enabled:true, ready:true, speaking:true, phase:"thinking"};
  controls.render(reply, true);
  assert.match(element("speech-status").textContent, /LLM is writing/);
  assert.equal(element("stop-speaking").textContent, "Cancel description");
  assert.equal(element("use-llm").disabled, true);
  controls.render({...reply, speaking:false, source:"fallback", detail:"Scene changed"}, true);
  assert.match(element("description-source").textContent, /Scene changed/);
  fail = false;
  element("use-llm").checked = true;
  await element("describe").click();
  assert.equal(requests.at(-1).body.use_llm, true);
  assert(requests.every(request => request.path.startsWith('/api/speech/')));
  console.log("Speech UI passed: describe, stop, transcript, stale/device errors, no motor requests.");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
