"use strict";

// Keep each physical input independently: releasing ArrowUp must not release W.
class SteeringInput {
  static keys = {ArrowUp: "forward", ArrowDown: "backward", ArrowLeft: "left", ArrowRight: "right", w: "forward", s: "backward", a: "left", d: "right"};

  constructor() {
    this.keys = new Set();
    this.pointers = new Map();
  }

  clear() {
    this.keys.clear();
    this.pointers.clear();
  }

  get command() {
    const held = new Set([...this.keys].map(key => SteeringInput.keys[key]));
    for (const direction of this.pointers.values()) held.add(direction);
    // Opposite directions cancel on their own axis.
    const forward = Number(held.has("forward")) - Number(held.has("backward"));
    const right = Number(held.has("right")) - Number(held.has("left"));
    const turn = right > 0 ? "right" : right < 0 ? "left" : "";
    if (!forward) return turn || "stop";
    return (forward > 0 ? "forward" : "backward") + (turn ? `_${turn}` : "");
  }
}

if (typeof module !== "undefined") module.exports = SteeringInput;
