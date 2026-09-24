"use strict";

class TalkControls {
  constructor(element, api) {
    this.element = element;
    this.api = api;
    this.state = null;
    this.online = false;
    this.turn = null;
    this.message = "";
    this.heartbeatBusy = false;
    const button = element("talk");
    button.addEventListener("pointerdown", event => {
      if (event.button !== 0 || button.disabled || this.turn?.held) return;
      event.preventDefault();
      button.setPointerCapture(event.pointerId);
      this.start(event.pointerId);
    });
    button.addEventListener("pointerup", event => {
      if (event.pointerId === this.turn?.pointer) this.finish();
    });
    for (const name of ["pointercancel", "lostpointercapture"]) {
      button.addEventListener(name, event => {
        if (event.pointerId === this.turn?.pointer && this.turn?.held) this.cancel();
      });
    }
    button.addEventListener("keydown", event => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      if (!event.repeat && !button.disabled) this.start("keyboard");
    });
    button.addEventListener("keyup", event => {
      if (event.key === "Enter" && this.turn?.pointer === "keyboard") {
        event.preventDefault();
        this.finish();
      }
    });
    button.addEventListener("blur", () => {if (this.turn?.held) this.cancel();});
    button.addEventListener("contextmenu", event => event.preventDefault());
    element("end-conversation").addEventListener("click", () => this.cancel(true));
    element("talk-form").addEventListener("submit", event => {
      event.preventDefault();
      this.sendText();
    });
    element("talk-prompt").addEventListener("input", () => this.render(undefined, this.online));
    element("talk-prompt").addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        if (!event.repeat) this.sendText();
      }
    });
    setInterval(() => this.heartbeat(), 500);
  }

  render(state, online) {
    // Ignore older poll replies arriving after a start/finish response.
    if (state && (!this.state?.at || !state.at || state.at >= this.state.at)) this.state = state;
    this.online = online;
    const current = this.state;
    const held = Boolean(this.turn?.held);
    const pending = Boolean(this.turn?.pending);
    const busy = Boolean(current?.busy);
    const button = this.element("talk");
    button.disabled = !online || !current?.ready || (!held && (pending || busy));
    button.textContent = held ? "Release to send" : "Hold to talk";
    button.setAttribute("aria-pressed", String(held));
    button.classList.toggle("recording", held);
    this.element("send-prompt").disabled = !online || !current?.ready || busy || pending || held ||
      !this.element("talk-prompt").value?.trim();
    this.element("end-conversation").disabled = !online || !(busy || held || pending || current?.turns);
    const phase = {recording: `Listening through the robot’s ears… ${current?.seconds || 0} / 15 s`,
      thinking: "Waiting for the spoken reply…", speaking: "Speaking through the robot’s mouth…",
      cancelling: "Stopping conversation…"}[current?.phase];
    this.element("talk-status").textContent = !online ? "Conversation disconnected"
      : !current?.enabled ? "Conversation not enabled"
      : this.message || phase || current.error || (current.turns
        ? "Ready for a follow-up question." : "Ready. Type a question or hold the button and speak near the robot.");
    this.element("talk-reply").textContent = current?.text ? `Robot: “${current.text}”` : "";
    if (!online && this.turn && !this.turn.cancelled) this.cancel();
    if (this.turn?.active && !busy && !pending) {
      this.turn.active = false;
      this.turn.held = false;
    }
  }

  async sendText() {
    const prompt = this.element("talk-prompt").value || "";
    if (!prompt.trim() || prompt.length > 500) return;
    return this.start(null, prompt);
  }

  async start(pointer, prompt = null) {
    if (!this.online || !this.state?.ready || this.state.busy || this.turn?.pending || this.turn?.held) return;
    // getRandomValues works on plain LAN HTTP, where randomUUID may be unavailable.
    const token = Array.from(crypto.getRandomValues(new Uint8Array(24)), n => n.toString(16).padStart(2, "0")).join("");
    const turn = {token, pointer, held:prompt === null, pending:true, active:false, cancelled:false};
    this.turn = turn;
    this.message = prompt === null ? "Starting microphone…" : "Sending question…";
    this.render(undefined, this.online);
    turn.started = this.api(prompt === null ? "/api/talk/start" : "/api/talk/text",
      prompt === null ? {token} : {token, prompt});
    try {
      const state = await turn.started;
      turn.active = true;
      this.message = "";
      if (prompt !== null && !turn.cancelled && this.element("talk-prompt").value === prompt) {
        this.element("talk-prompt").value = "";
      }
      this.render(state, this.online);
    } catch (error) {
      turn.held = false;
      this.message = error.message;
      // A timed-out HTTP request could still have started recording on the Pi.
      this.cancel();
    } finally {
      turn.pending = false;
      this.render(undefined, this.online);
    }
  }

  async finish() {
    const turn = this.turn;
    if (!turn?.held) return;
    turn.held = false;
    this.render(undefined, this.online);
    try {
      // A fast release must not overtake the start request.
      await turn.started;
      if (turn.cancelled || turn !== this.turn) return;
      this.render(await this.api("/api/talk/finish", {token:turn.token}), this.online);
    } catch (error) {
      this.message = error.message;
      this.cancel();
    }
  }

  async heartbeat() {
    const turn = this.turn;
    if (!turn?.active || turn.cancelled || this.heartbeatBusy) return;
    this.heartbeatBusy = true;
    try {
      const state = await this.api("/api/talk/heartbeat", {token:turn.token});
      if (turn === this.turn) this.render(state, this.online);
    } catch (_) {
      // Completion may beat a final heartbeat. Polling supplies the finished state.
    } finally {this.heartbeatBusy = false;}
  }

  async cancel(explicit = false) {
    const turn = this.turn;
    if (!turn && !explicit) return;
    if (turn) {
      turn.cancelled = true;
      turn.held = turn.active = false;
    }
    try {
      const state = await this.api("/api/talk/cancel", {token:turn?.token || "end-conversation-idle"}, true);
      if (explicit) this.message = "";
      this.render(state, this.online);
    } catch (_) { /* Server heartbeat timeout stops recording and playback. */ }
    this.render(undefined, this.online);
  }
}

if (typeof module !== "undefined") module.exports = {TalkControls};
