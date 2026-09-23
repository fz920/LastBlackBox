"use strict";

// Boxes use normalized coordinates from the exact JPEG shown underneath.
function detectionBox(box, flipped, width, height) {
  const [x1, y1, x2, y2] = box;
  return [x1 * width, (flipped ? 1 - y2 : y1) * height,
    (x2 - x1) * width, (y2 - y1) * height];
}

class DetectionOverlay {
  constructor(canvas, badge, summary) {
    this.canvas = canvas;
    this.context = canvas.getContext("2d");
    this.badge = badge;
    this.summary = summary;
    this.state = "starting";
    this.result = null;
  }

  update(state, result) {
    this.state = state;
    this.result = result;
  }

  render(enabled, fresh, flipped) {
    const ctx = this.context, width = this.canvas.width, height = this.canvas.height;
    ctx.clearRect(0, 0, width, height);
    this.badge.className = "badge";
    if (!enabled) {
      this.badge.textContent = "Detection hidden";
      this.summary.textContent = "Show detections to see object labels.";
      return;
    }
    if (!fresh) {
      this.badge.textContent = "Waiting for video";
      this.summary.textContent = "Detections will return with fresh video.";
      return;
    }
    if (this.state !== "live" || !this.result) {
      this.badge.textContent = this.state === "disabled" ? "Detection not enabled" : "Detection unavailable";
      this.summary.textContent = "Live camera is available. Waiting for the detector.";
      return;
    }
    this.badge.textContent = `Coral live · ${this.result.inference_ms} ms`;
    this.badge.classList.add("good");
    const counts = new Map();
    ctx.font = "bold 16px system-ui, sans-serif";
    ctx.lineWidth = 3;
    for (const object of this.result.objects) {
      counts.set(object.label, (counts.get(object.label) || 0) + 1);
      const [x, y, w, h] = detectionBox(object.box, flipped, width, height);
      ctx.strokeStyle = "#d1f878";
      ctx.strokeRect(x, y, w, h);
      const text = `${object.label} ${Math.round(object.score * 100)}%`;
      const labelWidth = Math.min(width, ctx.measureText(text).width + 12);
      const labelX = Math.max(0, Math.min(x, width - labelWidth));
      const labelY = Math.max(0, Math.min(y - 24, height - 24));
      ctx.fillStyle = "#d1f878";
      ctx.fillRect(labelX, labelY, labelWidth, 24);
      ctx.fillStyle = "#101410";
      ctx.fillText(text, labelX + 6, labelY + 17, labelWidth - 12);
    }
    this.summary.textContent = counts.size
      ? "Detected: " + [...counts].map(([label, count]) => `${label} × ${count}`).join(" · ")
      : "No objects detected above 50% confidence. Try a person, cup, bottle, or chair.";
  }
}

if (typeof module !== "undefined") module.exports = {DetectionOverlay, detectionBox};
