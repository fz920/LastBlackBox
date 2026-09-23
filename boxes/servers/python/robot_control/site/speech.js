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

  render(state, online) {
    this.state = state;
    this.online = online;
    const enabled = Boolean(state?.enabled), speaking = Boolean(state?.speaking);
    this.element("use-llm").disabled = !online || !state?.llm_enabled || speaking || this.pending;
    this.element("stop-speaking").textContent = ["loading", "thinking"].includes(state?.phase) ? "Cancel description" : "Stop speaking";
    this.element("describe").disabled = !online || !enabled || !state.ready || speaking || this.pending;
    this.element("stop-speaking").disabled = !online || !enabled || !speaking || this.pending;
    this.element("speech-status").textContent = !online ? "Speech disconnected"
      : !enabled ? "Speech not enabled"
      : speaking ? ({loading:"Loading the local LLM…", thinking:"LLM is writing a description…", preparing:"Preparing description…"}[state.phase] || "Speaking through the robot’s mouth…")
      : this.pending ? "Preparing description…"
      : state.error || this.message || "Ready to describe what the robot sees.";
    this.element("spoken-description").textContent = state?.text ? `Description: “${state.text}”` : "";
    const source = state?.source === "llm" ? "Local LLM · checked against detections"
      : state?.source === "fallback" ? `Basic fallback · ${state.detail}`
      : state?.text ? "Basic description" : "";
    this.element("description-source").textContent = source + (source && state?.generation_seconds != null ? ` · ${state.generation_seconds}s` : "");
  }

  async request(action) {
    if (this.pending) return;
    this.pending = true;
    this.message = "";
    this.render(this.state, this.online);
    try {
      this.state = await this.api(`/api/speech/${action}`, action === "describe"
        ? {use_llm: Boolean(this.state?.llm_enabled && this.element("use-llm").checked)} : {});
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
