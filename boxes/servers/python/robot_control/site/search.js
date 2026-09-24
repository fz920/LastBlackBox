"use strict";

class SearchControls {
  constructor(element, api, context, stopAll) {
    this.element = element;
    this.api = api;
    this.context = context;
    this.stopAll = stopAll;
    this.state = null;
    this.online = false;
    this.job = null;
    this.message = "";
    this.heartbeatBusy = false;
    this.memoryRevision = null;
    element('search-memory-clear').addEventListener('click', () => this.clearMemory());
    element("conversation-tab").addEventListener("click", () => this.select(false));
    element("search-tab").addEventListener("click", () => this.select(true));
    element("search-form").addEventListener("submit", event => {event.preventDefault(); this.start();});
    for (const id of ["search-target", "search-arm", "search-explore", "search-speed", "search-max-turns", "search-max-steps", "search-sequence", "search-mode", "search-image-rate"]) {
      element(id).addEventListener("input", () => this.render(undefined, this.online));
      element(id).addEventListener("change", () => this.render(undefined, this.online));
    }
    element("cancel-search").addEventListener("click", () => this.stopAll());
    const voice = element("search-voice");
    voice.addEventListener("pointerdown", event => {
      if (event.button !== 0 || voice.disabled || this.job?.held) return;
      event.preventDefault();
      voice.setPointerCapture(event.pointerId);
      this.start(true, event.pointerId);
    });
    voice.addEventListener("pointerup", event => {
      if (this.job?.pointer === event.pointerId) this.finishRecording();
    });
    for (const event of ["pointercancel", "lostpointercapture", "blur"]) {
      voice.addEventListener(event, () => {if (this.job?.held) this.cancel();});
    }
    voice.addEventListener("keydown", event => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      if (!event.repeat && !voice.disabled) this.start(true, "keyboard");
    });
    voice.addEventListener("keyup", event => {
      if (event.key === "Enter" && this.job?.pointer === "keyboard") {
        event.preventDefault(); this.finishRecording();
      }
    });
    voice.addEventListener("contextmenu", event => event.preventDefault());
    setInterval(() => this.heartbeat(), 400);
  }

  select(searching) {
    if (!searching) this.cancel();
    this.element("conversation-controls").hidden = searching;
    this.element("search-controls").hidden = !searching;
    for (const id of ["talk-reply", "spoken-description"]) this.element(id).hidden = searching;
    this.element("search-output").hidden = !searching;
    this.element("conversation-tab").setAttribute("aria-pressed", String(!searching));
    this.element("search-tab").setAttribute("aria-pressed", String(searching));
  }

  limits() {
    const read = id => {
      const value = this.element(id).value;
      return value.trim() === '' ? NaN : Number(value);
    };
    return {max_turns:read('search-max-turns'), max_steps:read('search-max-steps'),
      sequence_length:read('search-sequence')};
  }

  validLimits() {
    const l = this.limits();
    return Number.isInteger(l.max_turns) && l.max_turns >= 0 && l.max_turns <= 24 &&
      Number.isInteger(l.max_steps) && l.max_steps >= 0 && l.max_steps <= 12 &&
      Number.isInteger(l.sequence_length) && l.sequence_length >= 1 && l.sequence_length <= 3 &&
      ['snapshot', 'live'].includes(this.element('search-mode').value) &&
      [0.5, 1, 2, 4].includes(Number(this.element('search-image-rate').value));
  }

  render(state, online) {
    if (state && (!this.state?.at || state.at >= this.state.at)) this.state = state;
    this.online = online;
    const s = this.state, c = this.context();
    const pending = Boolean(this.job?.pending), busy = Boolean(s?.busy);
    this.renderMemory(s?.memory);
    this.element('search-memory-clear').disabled = !online || pending || busy || !s?.memory?.entries?.length;
    const held = Boolean(this.job?.held);
    const target = this.element("search-target").value?.trim();
    this.element("start-search").disabled = !online || !s?.ready || !c.fresh || c.driving ||
      pending || busy || !this.validLimits() || !target || target.length > (s?.max_clue || 100) || !this.element("search-arm").checked;
    const voice = this.element("search-voice");
    voice.disabled = !online || !s?.voice_ready || (!held &&
      (!s?.ready || !c.fresh || c.driving || pending || busy || !this.validLimits() || !this.element("search-arm").checked));
    voice.textContent = held ? "Release to search" : "Hold to give a clue";
    voice.setAttribute("aria-pressed", String(held));
    this.element("cancel-search").disabled = !online || !(pending || busy);
    this.element("search-target").disabled = pending || busy;
    this.element("search-arm").disabled = !online || pending || busy;
    this.element("search-explore").disabled = !online || pending || busy;
    this.element("search-speed").disabled = !online || pending || busy;
    for (const id of ['search-max-turns', 'search-max-steps', 'search-sequence']) {
      this.element(id).disabled = !online || pending || busy;
    }
    this.element('search-mode').disabled = !online || pending || busy;
    this.element('search-image-rate').disabled = !online || pending || busy || this.element('search-mode').value !== 'live';
    this.element('search-live-help').hidden = this.element('search-mode').value !== 'live';
    const live = this.element('search-live-status'), ls = s?.live_status;
    live.hidden = !s?.live;
    live.textContent = s?.live ? `${busy ? 'LIVE' : 'Live search ended'} · target ${s.image_rate} images/s · ${ls?.actual_image_rate || 0} sent/s · ${ls?.images_sent || 0} images sent · ${ls?.assessments || 0} assessments · ${ls?.skipped_monitor_frames || 0} redundant monitor frames skipped${ls?.latency != null ? ` · ${ls.latency}s last response` : ''}${ls?.error ? ` · ${ls.error}` : ''}` : '';
    this.element("search-result").textContent = this.message || s?.message ||
      "Choose an object below. I’ll look from here, turning a little between views.";
    this.element("search-evidence").textContent = s?.evidence ? `Last image check: ${s.evidence}` : "";
    this.element("search-alignment").textContent = s?.centering_note || "";
    this.element("search-request").textContent = s?.request_text ? `Your clue: ${s.request_text}` : "";
    this.element("search-plan").textContent = s?.action_reason ? `Plan: ${(s.plan?.length ? s.plan.join(' → ') : s.action)}${s.plan_index ? ` (${s.plan_index}/${s.plan.length})` : ''} · ${s.action_reason}` : "";
    this.element("search-progress").textContent = !online ? "Search disconnected"
      : !s?.enabled ? "Restart the server with --realtime to enable search."
      : s.error || s.announcement_error || (s.phase === "recording" ? `Listening · ${s.seconds || 0} / ${s.max_recording || 15} s`
        : s.phase === "interpreting" ? "Understanding the clue · wheels stopped"
        : busy ? `${s.speed === 'full' ? 'FULL SPEED · ' : ''}${s.turns} / ${s.max_turns} search turns · ${s.center_turns || 0} / ${s.max_center_turns || 0} centring turns${s.explore ? ` · ${s.steps || 0} / ${s.max_steps} steps` : ''} · ${s.checks} / ${s.max_checks} checks`
        : s.preview ? "Preview mode · wheel commands are simulated" : "Ready · adaptive search");
    const photo = this.element("search-match");
    photo.hidden = !s?.result_id;
    if (s?.result_id && photo.dataset.result !== String(s.result_id)) {
      photo.dataset.result = String(s.result_id);
      photo.src = `/api/search/frame?result=${s.result_id}`;
    }
    if (!s?.result_id) delete photo.dataset.result;
    this.element("search-image-note").hidden = !s?.result_id;
    if (this.job?.active && !busy && !pending) {
      this.job.active = false;
      this.job.held = false;
      this.element("search-arm").checked = false;
      this.element("search-explore").checked = false;
      this.element("search-speed").value = 'slow';
    }
    if ((!online || !c.fresh) && this.job && !this.job.cancelled && (pending || this.job.active)) this.cancel();
  }

  renderMemory(memory) {
    if (!memory || memory.revision === this.memoryRevision) return;
    this.memoryRevision = memory.revision;
    this.element('search-memory-summary').textContent = `Search memory · ${memory.views} views · ${memory.repeats} likely revisits`;
    const list = this.element('search-memory-entries');
    const cards = memory.entries.map(entry => {
      const card = document.createElement('div');
      card.className = 'search-memory-card';
      card.setAttribute('role', 'listitem');
      if (entry.thumbnail) {
        const image = document.createElement('img');
        image.src = `/api/search/memory/frame?check=${encodeURIComponent(entry.check)}`;
        image.alt = `View ${entry.view}, check ${entry.check}`;
        image.width = 80; image.height = 60;
        image.loading = 'lazy';
        card.append(image);
      }
      const description = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = `View ${entry.view} · ${entry.seconds}s · ${entry.decision}${entry.repeat ? ' · likely revisit' : ''}`;
      const evidence = document.createElement('p');
      evidence.textContent = entry.evidence;
      const actions = document.createElement('p');
      actions.className = 'detection-help';
      actions.textContent = (entry.departures || []).map(a => `${a.action} ${a.seconds}s (${a.outcome})`).join(' → ') +
        (entry.next_view ? ` → view ${entry.next_view}` : '');
      description.append(title, evidence, actions);
      card.append(description);
      return card;
    });
    list.replaceChildren(...cards);
  }

  async clearMemory() {
    if (!this.online || this.state?.busy || this.job?.pending) return;
    try {
      this.render(await this.api('/api/search/memory/clear', {}), this.online);
    } catch (error) {
      this.message = error.message;
      this.render(undefined, this.online);
    }
  }

  async start(voice = false, pointer = null) {
    const c = this.context(), target = this.element("search-target").value?.trim();
    if (!this.online || !this.state?.ready || this.state.busy || this.job?.pending ||
        !c.fresh || c.driving || !this.validLimits() || !this.element("search-arm").checked ||
        (voice ? !this.state.voice_ready : !target || target.length > (this.state.max_clue || 100))) return;
    const token = Array.from(crypto.getRandomValues(new Uint8Array(24)), n => n.toString(16).padStart(2, "0")).join("");
    const job = {token, pending:true, active:false, cancelled:false, held:voice, pointer};
    const explore = this.element("search-explore").checked === true;
    const speed = this.element("search-speed").value || 'slow';
    this.job = job;
    this.message = voice ? "Starting microphone…" : "Understanding your clue…";
    this.element("search-arm").checked = false;
    this.element("search-explore").checked = false;
    this.render(undefined, this.online);
    try {
      job.started = this.api(voice ? "/api/search/listen" : "/api/search/start", {token,
        ...(voice ? {} : {target}), allow_turns:true, explore, speed, ...this.limits(),
        live:this.element('search-mode').value === 'live', image_rate:Number(this.element('search-image-rate').value),
        frame_time:c.frameTime, stop_generation:c.stopGeneration});
      const state = await job.started;
      job.active = !job.cancelled;
      this.message = "";
      this.render(state, this.online);
    } catch (error) {
      this.message = error.message;
      this.cancel();
    } finally {
      job.pending = false;
      this.render(undefined, this.online);
    }
  }

  async finishRecording() {
    const job = this.job;
    if (!job?.held) return;
    job.held = false;
    this.render(undefined, this.online);
    try {
      await job.started;
      if (job.cancelled || this.job !== job) return;
      this.render(await this.api("/api/search/finish", {token:job.token}), this.online);
    } catch (error) {
      this.message = error.message;
      this.cancel();
    }
  }

  async heartbeat() {
    const job = this.job;
    if (!job?.active || job.cancelled || this.heartbeatBusy) return;
    const c = this.context();
    if (!this.online || !c.fresh) return this.cancel();
    this.heartbeatBusy = true;
    try {
      const state = await this.api("/api/search/heartbeat", {token:job.token, frame_time:c.frameTime});
      if (job === this.job) this.render(state, this.online);
    } catch (_) { /* Polling shows completion; the server independently expires the lease. */ }
    finally {this.heartbeatBusy = false;}
  }

  async cancel() {
    this.element("search-arm").checked = false;
    this.element("search-explore").checked = false;
    this.element("search-speed").value = 'slow';
    const job = this.job;
    if (!job || job.cancelled || (!job.pending && !job.active)) return;
    job.cancelled = true;
    job.held = false;
    job.active = false;
    this.message = "Stopping search…";
    try {
      const state = await this.api("/api/search/cancel", {token:job.token}, true);
      if (this.job === job) {this.message = ""; this.render(state, this.online);}
    } catch (_) { /* The motor and browser watchdogs stop independently. */ }
  }
}

if (typeof module !== "undefined") module.exports = {SearchControls};
