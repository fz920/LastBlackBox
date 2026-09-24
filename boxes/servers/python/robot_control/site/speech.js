"use strict";

class SpeechControls {
  constructor(element, api) {
    this.element = element;
    this.api = api;
    this.pending = false;
    this.state = null;
    this.online = false;
    this.message = "";
    element("describe").addEventListener("click", () => this.request("describe"));
    element("stop-speaking").addEventListener("click", () => this.request("stop"));
  }

  render(state, online, audioBusy = false) {
    if (state !== undefined) this.state = state;
    state = this.state;
    this.online = online;
    const enabled = Boolean(state?.enabled), speaking = Boolean(state?.speaking);
    this.element("describe").disabled = !online || !enabled || !state.ready || speaking || this.pending || audioBusy;
    this.element("stop-speaking").disabled = !online || !enabled || !speaking || this.pending;
    this.element("speech-status").textContent = !online ? "Speech disconnected"
      : !enabled ? "Speech not enabled"
      : speaking ? "Speaking through the robot’s mouth…"
      : this.pending ? "Preparing description…"
      : state.error || this.message || "Ready to describe what the robot sees.";
    this.element("spoken-description").textContent = state?.text ? `Description: “${state.text}”` : "";

  }

  async request(action) {
    if (this.pending) return;
    this.pending = true;
    this.message = "";
    this.render(this.state, this.online);
    try {
      this.state = await this.api(`/api/speech/${action}`, {});
      this.message = action === "stop" ? "Speech stopped." : "";
    } catch (error) {
      this.message = error.message;
    } finally {
      this.pending = false;
      this.render(this.state, this.online);
    }
  }
}

if (typeof module !== "undefined") module.exports = {SpeechControls};
