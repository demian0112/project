"use strict";

const root = document.body;
const deviceName = root.dataset.deviceName || "";
const requestedFrames = Number(root.dataset.frames || 180);
const pollIntervalMs = 1000;

const elements = {
  manualRefresh: document.querySelector("#manual-refresh"),
  lastPolledAt: document.querySelector("#last-polled-at"),
  sidebarLiveState: document.querySelector("#sidebar-live-state"),
  sidebarLiveCopy: document.querySelector("#sidebar-live-copy"),
  deviceSessionText: document.querySelector("#device-session-text"),
  deviceStatePill: document.querySelector("#device-state-pill"),
  deviceDetectionPill: document.querySelector("#device-detection-pill"),
  deviceQualityPill: document.querySelector("#device-quality-pill"),
  heatmapShape: document.querySelector("#heatmap-shape"),
  heatmapCanvas: document.querySelector("#csi-heatmap-canvas"),
  emptyState: document.querySelector("#csi-empty-state"),
  liveStatus: document.querySelector("#live-status"),
  metricRssi: document.querySelector("#metric-rssi"),
  metricFps: document.querySelector("#metric-fps"),
  metricFrames: document.querySelector("#metric-frames"),
  metricSubcarriers: document.querySelector("#metric-subcarriers"),
  metricLost: document.querySelector("#metric-lost"),
  metricLossRate: document.querySelector("#metric-loss-rate"),
  metricSeqGaps: document.querySelector("#metric-seq-gaps"),
  metricLastSeq: document.querySelector("#metric-last-seq"),
  metricLastUpdate: document.querySelector("#metric-last-update"),
};

const stateLabels = {
  online: "online",
  offline: "offline",
  error: "fault",
};

const detectionLabels = {
  idle: "idle",
  starting: "starting",
  running: "running",
  stopping: "stopping",
};

let pollTimer = null;
let inFlight = false;

function text(element, value) {
  if (element) {
    element.textContent = value;
  }
}

function formatNumber(value, digits = 0) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  return value.toFixed(digits);
}

function formatPercent(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  return `${(value * 100).toFixed(1)}%`;
}

function formatRssi(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  return `${value} dBm`;
}

function formatAge(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms)) {
    return "--";
  }
  if (ms < 1000) {
    return `${Math.max(0, Math.round(ms))} ms ago`;
  }
  return `${(ms / 1000).toFixed(1)} s ago`;
}

function setLiveStatus(kind, label, copy) {
  text(elements.liveStatus, label);
  text(elements.sidebarLiveState, label);
  text(elements.sidebarLiveCopy, copy);
  if (elements.liveStatus) {
    elements.liveStatus.className = `live-status is-${kind}`;
  }
}

function setPill(element, baseClass, modifierPrefix, value, labels) {
  if (!element) {
    return;
  }
  element.className = `${baseClass} ${modifierPrefix}-${value || "unknown"}`;
  element.textContent = labels[value] || value || "unknown";
}

function updateDevice(device) {
  if (!device) {
    return;
  }
  setPill(
    elements.deviceStatePill,
    "status-pill",
    "state",
    device.state,
    stateLabels,
  );
  setPill(
    elements.deviceDetectionPill,
    "detection-pill",
    "detection",
    device.detection_state,
    detectionLabels,
  );
  setPill(
    elements.deviceQualityPill,
    "quality-pill",
    "quality",
    device.network_quality,
    {},
  );
  text(
    elements.deviceSessionText,
    `Session: ${device.current_session || "not running"}`,
  );
}

function decodeBase64(value) {
  const binary = window.atob(value || "");
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function mix(start, end, amount) {
  return Math.round(start + (end - start) * amount);
}

function heatColor(value) {
  const t = Math.min(Math.max(value / 255, 0), 1);
  if (t < 0.33) {
    const amount = t / 0.33;
    return [mix(13, 0, amount), mix(71, 170, amount), mix(161, 180, amount)];
  }
  if (t < 0.66) {
    const amount = (t - 0.33) / 0.33;
    return [mix(0, 250, amount), mix(170, 204, amount), mix(180, 21, amount)];
  }
  const amount = (t - 0.66) / 0.34;
  return [mix(250, 220, amount), mix(204, 38, amount), mix(21, 38, amount)];
}

function clearCanvas() {
  const canvas = elements.heatmapCanvas;
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
}

function drawHeatmap(heatmap) {
  const columns = Number(heatmap.frames || 0);
  const rows = Number(heatmap.subcarriers || 0);
  const bytes = decodeBase64(heatmap.matrix_b64);
  if (!columns || !rows || bytes.length < columns * rows) {
    clearCanvas();
    return false;
  }

  const canvas = elements.heatmapCanvas;
  canvas.width = columns;
  canvas.height = rows;
  const context = canvas.getContext("2d");
  const image = context.createImageData(columns, rows);

  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      const sourceIndex = row * columns + column;
      const targetIndex = sourceIndex * 4;
      const color = heatColor(bytes[sourceIndex]);
      image.data[targetIndex] = color[0];
      image.data[targetIndex + 1] = color[1];
      image.data[targetIndex + 2] = color[2];
      image.data[targetIndex + 3] = 255;
    }
  }

  context.putImageData(image, 0, 0);
  return true;
}

function updateMetrics(heatmap) {
  const stats = heatmap.stats || {};
  text(elements.heatmapShape, `${heatmap.frames} x ${heatmap.subcarriers}`);
  text(elements.metricRssi, formatRssi(stats.last_rssi));
  text(elements.metricFps, formatNumber(stats.fps, 1));
  text(
    elements.metricFrames,
    `${heatmap.available_frames}/${heatmap.frames}`,
  );
  text(elements.metricSubcarriers, String(heatmap.subcarriers || "--"));
  text(elements.metricLost, String(stats.lost_frames || 0));
  text(elements.metricLossRate, formatPercent(stats.loss_rate));
  text(elements.metricSeqGaps, String(stats.seq_gap_events || 0));
  text(elements.metricLastSeq, String(stats.last_sequence ?? "--"));
  text(elements.metricLastUpdate, formatAge(stats.last_update_age_ms));
}

function resetMetrics() {
  text(elements.metricRssi, "--");
  text(elements.metricFps, "--");
  text(elements.metricFrames, "--");
  text(elements.metricSubcarriers, "--");
  text(elements.metricLost, "--");
  text(elements.metricLossRate, "--");
  text(elements.metricSeqGaps, "--");
  text(elements.metricLastSeq, "--");
  text(elements.metricLastUpdate, "--");
}

function showEmpty(message) {
  clearCanvas();
  if (elements.emptyState) {
    elements.emptyState.hidden = false;
    elements.emptyState.textContent = message;
  }
  resetMetrics();
  text(elements.heatmapShape, `${requestedFrames} x --`);
}

function showHeatmap() {
  if (elements.emptyState) {
    elements.emptyState.hidden = true;
  }
}

async function pollHeatmap() {
  if (inFlight || !deviceName) {
    return;
  }
  inFlight = true;
  setLiveStatus("waiting", "Polling", "Fetching latest CSI snapshot");

  try {
    const url =
      `/api/devices/${encodeURIComponent(deviceName)}/csi-heatmap` +
      `?frames=${encodeURIComponent(requestedFrames)}`;
    const response = await fetch(url, {
      headers: {
        Accept: "application/json",
      },
    });
    if (response.status === 401) {
      window.location.assign("/admin/login");
      return;
    }
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Failed to load CSI snapshot");
    }

    text(elements.lastPolledAt, new Date().toLocaleTimeString());
    updateDevice(payload.device);
    if (!payload.ok || !payload.heatmap || !payload.heatmap.ok) {
      showEmpty("No live CSI frames for this device yet");
      setLiveStatus("waiting", "Waiting", "Start detection to receive CSI");
      return;
    }

    if (drawHeatmap(payload.heatmap)) {
      showHeatmap();
      updateMetrics(payload.heatmap);
      setLiveStatus("ok", "Live", "Receiving CSI frames");
    } else {
      showEmpty("CSI matrix is incomplete");
      setLiveStatus("error", "Invalid", "Snapshot matrix is incomplete");
    }
  } catch (error) {
    showEmpty(error.message || "CSI polling failed");
    setLiveStatus("error", "Error", "Polling failed");
  } finally {
    inFlight = false;
  }
}

function startPolling() {
  if (pollTimer) {
    window.clearInterval(pollTimer);
  }
  pollHeatmap();
  pollTimer = window.setInterval(pollHeatmap, pollIntervalMs);
}

function stopPolling() {
  if (pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

if (elements.manualRefresh) {
  elements.manualRefresh.addEventListener("click", pollHeatmap);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopPolling();
  } else {
    startPolling();
  }
});

window.addEventListener("beforeunload", stopPolling);
startPolling();
