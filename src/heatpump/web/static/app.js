// The heat pump page: a graph of sensor readings and the coil image for the time
// under the pointer.
//
// All times inside this file are milliseconds since 1970 (UTC), as the API
// returns them. Plotly shows a time given as a number in the browser's local
// time, so times go into Plotly unchanged. What Plotly hands back (axis ranges,
// the time under the pointer) is local time as text, "2026-11-14 05:32:10.5".

"use strict";

const MAX_POINTS = 2000;       // per channel, for the visible range
const MARGIN = 0.15;           // also fetch this fraction of the range on each side
const ZOOM_DELAY_MS = 250;     // wait for a zoom or pan to settle before fetching
const SERIES_COLORS = 8;
const HOUR = 3600 * 1000;

const state = {
  sensors: [],                 // the channel catalog from /api/sensors
  selected: [],                // names of the channels shown
  view: null,                  // {start, end}: the time range on screen
  home: null,                  // the range a double-click returns to
  presetHours: null,           // set while the view is "the last N hours"
  images: [],                  // images in the fetched range, oldest first
  imageIndex: -1,              // which of them is shown
  pinned: false,               // true = the image no longer follows the pointer
  cursor: null,                // time under the pointer
  requestId: 0,                // to ignore answers that arrive out of order
  lastData: null,              // last /api/readings answer, for redrawing on theme change
};

const $ = (id) => document.getElementById(id);
const graph = $("graph");

// ---------------------------------------------------------------------------
// Time helpers
// ---------------------------------------------------------------------------

// A time from Plotly: a number is already milliseconds; text is local time.
// Text with a "T" and no zone is read by the browser as local time.
function plotValueToMs(value) {
  if (typeof value === "number") return value;
  let text = String(value).trim().replace(" ", "T");
  if (!text.includes("T")) text += "T00:00";  // a bare date means local midnight
  return Date.parse(text);
}

function toInputValue(ms) {
  // local time as "2026-11-14T05:32:10", for the datetime-local inputs
  const local = ms - new Date(ms).getTimezoneOffset() * 60000;
  return new Date(local).toISOString().slice(0, 19);
}

function formatTime(ms) {
  return new Date(ms).toLocaleString([], {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit",
  });
}

function formatSpan(ms) {
  const s = Math.round(Math.abs(ms) / 1000);
  if (s < 90) return `${s} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 36 * 3600) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86400).toFixed(1)} days`;
}

// ---------------------------------------------------------------------------
// Talking to the server
// ---------------------------------------------------------------------------

async function getJson(path, params) {
  const query = params ? "?" + new URLSearchParams(params) : "";
  const response = await fetch(path + query);
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.json();
}

function showBanner(message) {
  $("banner").textContent = message || "";
  $("banner").hidden = !message;
}

// ---------------------------------------------------------------------------
// The address bar remembers the channels and the time range
// ---------------------------------------------------------------------------

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  const names = (params.get("sensors") || "").split(",").filter(Boolean);
  const hours = Number(params.get("hours"));
  const start = Number(params.get("start"));
  const end = Number(params.get("end"));
  return {
    names,
    hours: hours > 0 ? hours : null,
    range: start > 0 && end > start ? { start, end } : null,
  };
}

function writeHash() {
  const params = new URLSearchParams();
  params.set("sensors", state.selected.join(","));
  if (state.presetHours) {
    params.set("hours", state.presetHours);
  } else {
    params.set("start", Math.round(state.view.start));
    params.set("end", Math.round(state.view.end));
  }
  history.replaceState(null, "", "#" + params.toString().replaceAll("%2C", ","));
}

// ---------------------------------------------------------------------------
// Channels
// ---------------------------------------------------------------------------

// Channels are grouped into one panel per unit; on/off channels share a panel
function groupOf(sensor) {
  return sensor.kind === "binary" ? "on / off" : (sensor.unit || "no unit");
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// A channel's color depends on its place in the catalog, not on what else is
// selected, so it keeps its color when other channels are added or removed.
function colorOf(sensor) {
  const peers = state.sensors.filter((s) => groupOf(s) === groupOf(sensor));
  return cssVar(`--series-${(peers.indexOf(sensor) % SERIES_COLORS) + 1}`);
}

function dashOf(sensor) {
  const peers = state.sensors.filter((s) => groupOf(s) === groupOf(sensor));
  return peers.indexOf(sensor) >= SERIES_COLORS ? "dash" : "solid";
}

function buildPicker() {
  const list = $("picker-list");
  list.replaceChildren();
  const groups = [...new Set(state.sensors.map(groupOf))];
  for (const group of groups) {
    const heading = document.createElement("h3");
    heading.textContent = group;
    list.append(heading);
    for (const sensor of state.sensors.filter((s) => groupOf(s) === group)) {
      const label = document.createElement("label");
      if (sensor.description) label.title = sensor.description;
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = sensor.name;
      box.checked = state.selected.includes(sensor.name);
      const swatch = document.createElement("span");
      swatch.className = "swatch";
      swatch.style.background = colorOf(sensor);
      const text = document.createElement("span");
      text.textContent = sensor.name;
      label.append(box, swatch, text);
      list.append(label);
    }
  }
  updatePickerCount();
}

function updatePickerCount() {
  $("picker-count").textContent = `(${state.selected.length} of ${state.sensors.length})`;
}

let pickerTimer = null;
$("picker-list").addEventListener("change", () => {
  const checked = [...$("picker-list").querySelectorAll("input:checked")].map((b) => b.value);
  // Keep catalog order
  state.selected = state.sensors.map((s) => s.name).filter((n) => checked.includes(n));
  updatePickerCount();
  clearTimeout(pickerTimer);
  pickerTimer = setTimeout(load, 300);
});

document.addEventListener("click", (event) => {
  if (!$("picker").contains(event.target)) $("picker").open = false;
});

// ---------------------------------------------------------------------------
// Time range controls
// ---------------------------------------------------------------------------

function setView(start, end, { presetHours = null, home = true } = {}) {
  state.view = { start, end };
  state.presetHours = presetHours;
  if (home) state.home = { start, end, presetHours };
  for (const button of $("presets").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.hours) === presetHours));
  }
  $("from").value = toInputValue(start);
  $("to").value = toInputValue(end);
  load();
}

function showLastHours(hours) {
  const now = Date.now();
  setView(now - hours * HOUR, now, { presetHours: hours });
}

$("presets").addEventListener("click", (event) => {
  const hours = Number(event.target.dataset.hours);
  if (hours) showLastHours(hours);
});

function shiftView(direction) {
  const span = state.view.end - state.view.start;
  setView(state.view.start + direction * span, state.view.end + direction * span);
}
function goHome() {
  const { start, end, presetHours } = state.home;
  setView(start, end, { presetHours });
}
$("zoom-out").addEventListener("click", goHome);
$("earlier").addEventListener("click", () => shiftView(-1));
$("later").addEventListener("click", () => shiftView(1));

$("custom").addEventListener("submit", (event) => {
  event.preventDefault();
  const start = new Date($("from").value).getTime();
  const end = new Date($("to").value).getTime();
  if (!(end > start)) {
    showBanner("The 'to' time must be later than the 'from' time.");
    return;
  }
  setView(start, end);
});

// ---------------------------------------------------------------------------
// Loading data for the current view
// ---------------------------------------------------------------------------

async function load() {
  const { start, end } = state.view;
  const requestId = ++state.requestId;
  writeHash();
  graph.classList.add("loading");
  // A little more than is on screen, so a small pan does not show an empty edge
  const margin = (end - start) * MARGIN;
  const range = {
    start: new Date(start - margin).toISOString(),
    end: new Date(end + margin).toISOString(),
  };
  try {
    const [data, images] = await Promise.all([
      state.selected.length
        ? getJson("api/readings", {
            sensors: state.selected.join(","),
            ...range,
            max_points: Math.round(MAX_POINTS * (1 + 2 * MARGIN)),
          })
        : { series: [] },
      getJson("api/images", range),
    ]);
    if (requestId !== state.requestId) return;  // a newer request has been made
    showBanner("");
    state.lastData = data;
    draw(data);
    setImages(images);
  } catch (error) {
    if (requestId !== state.requestId) return;
    showBanner(`Could not load the data: ${error.message}`);
  } finally {
    if (requestId === state.requestId) graph.classList.remove("loading");
  }
}

// ---------------------------------------------------------------------------
// The graph
// ---------------------------------------------------------------------------

function withAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function draw(data) {
  const byName = new Map(state.sensors.map((s) => [s.name, s]));
  const groups = [...new Set(data.series.map((s) => groupOf(byName.get(s.name))))];
  const traces = [];

  for (const series of data.series) {
    const sensor = byName.get(series.name);
    const group = groupOf(sensor);
    const axis = groups.indexOf(group) + 1;
    const yaxis = axis === 1 ? "y" : `y${axis}`;
    const color = colorOf(sensor);
    const x = series.ts;
    const binary = sensor.kind === "binary";
    const unit = binary || !sensor.unit ? "" : ` ${sensor.unit}`;

    if (series.bucket_seconds) {
      // A band from the lowest to the highest reading in each bucket, so short
      // spikes stay visible when the line shows an average
      const band = {
        x, yaxis, type: "scatter", mode: "lines", line: { width: 0 },
        legendgroup: series.name, showlegend: false, hoverinfo: "skip", connectgaps: false,
      };
      traces.push({ ...band, y: series.low });
      traces.push({ ...band, y: series.high, fill: "tonexty", fillcolor: withAlpha(color, 0.22) });
    }
    traces.push({
      x, y: series.value, yaxis, type: "scatter", mode: "lines",
      name: series.name, legendgroup: series.name,
      // An on/off value holds until the next reading; so does an average bucket's
      // neighbor, but those are close enough together to join with straight lines
      line: { color, width: 2, dash: dashOf(sensor), shape: binary && !series.bucket_seconds ? "hv" : "linear" },
      connectgaps: false,
      hovertemplate: `%{y:,.2f}${unit}<extra>${series.name}</extra>`,
    });
  }

  const text = cssVar("--text");
  const muted = cssVar("--muted");
  const grid = cssVar("--grid");
  const axisLine = cssVar("--axis");
  const surface = cssVar("--surface");

  // Stack the panels: on/off panels are a third the height of the others
  const weights = groups.map((g) => (g === "on / off" ? 1 : 3));
  const total = weights.reduce((a, b) => a + b, 0) || 1;
  const gap = groups.length > 1 ? 0.05 : 0;
  const usable = 1 - gap * (groups.length - 1);
  const layout = {
    height: Math.max(420, 150 + 80 * total),
    margin: { l: 64, r: 16, t: 8, b: 40 },
    paper_bgcolor: surface,
    plot_bgcolor: surface,
    font: { family: 'system-ui, -apple-system, "Segoe UI", sans-serif', size: 12, color: text },
    showlegend: data.series.length > 1,
    legend: { orientation: "h", x: 0, y: 1.0, yanchor: "bottom", font: { color: text } },
    hovermode: "x unified",
    hoversubplots: "axis",
    hoverlabel: { bgcolor: surface, bordercolor: axisLine, font: { color: text } },
    dragmode: "zoom",
    uirevision: "keep",          // keep legend choices when new data arrives
    xaxis: {
      type: "date",
      range: [state.view.start, state.view.end],
      uirevision: `${state.view.start}-${state.view.end}`,
      anchor: groups.length > 1 ? `y${groups.length}` : "y",
      gridcolor: grid, linecolor: axisLine, tickcolor: axisLine, tickfont: { color: muted },
      showspikes: true, spikemode: "across", spikesnap: "cursor",
      spikethickness: 1, spikecolor: muted, spikedash: "solid",
    },
    shapes: imageMarker(),
    annotations: [],
  };
  if (state.selected.length === 0) {
    layout.annotations.push(message("Choose one or more channels above."));
  } else if (data.series.every((s) => s.ts.length === 0)) {
    layout.annotations.push(message("No readings in this time range."));
  }

  // The first group is the top panel
  let top = 1;
  groups.forEach((group, i) => {
    const height = usable * weights[i] / total;
    const axis = {
      domain: [Math.max(0, top - height), top],
      title: { text: group, font: { color: muted, size: 12 } },
      fixedrange: true,          // zooming is along the time axis only
      gridcolor: grid, zeroline: false, linecolor: axisLine, tickfont: { color: muted },
    };
    if (group === "on / off") {
      Object.assign(axis, { range: [-0.15, 1.15], tickvals: [0, 1], ticktext: ["off", "on"] });
    }
    layout[i === 0 ? "yaxis" : `yaxis${i + 1}`] = axis;
    top -= height + gap;
  });
  if (groups.length === 0) layout.yaxis = { visible: false, fixedrange: true };

  Plotly.react(graph, traces, layout, {
    displaylogo: false,
    responsive: true,
    scrollZoom: true,
    doubleClick: false,          // handled in wireGraph
    modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d", "zoomIn2d", "zoomOut2d"],
  });
  if (!graph.dataset.wired) wireGraph();
}

function message(text) {
  return {
    text, showarrow: false, xref: "paper", yref: "paper", x: 0.5, y: 0.5,
    font: { size: 14, color: cssVar("--muted") },
  };
}

// A vertical line at the time of the image being shown
function imageMarker() {
  const image = state.images[state.imageIndex];
  if (!image) return [];
  return [{
    type: "line", xref: "x", yref: "paper",
    x0: image.ts, x1: image.ts, y0: 0, y1: 1,
    line: { color: cssVar("--text-2"), width: 1, dash: "dot" },
  }];
}

function wireGraph() {
  graph.dataset.wired = "yes";
  let zoomTimer = null;

  // Zoom or pan: fetch the new range, which comes back at a finer resolution
  graph.on("plotly_relayout", (changes) => {
    if (changes["xaxis.autorange"]) {         // "Reset axes" in the graph's toolbar
      goHome();
      return;
    }
    let range = changes["xaxis.range"];
    if (!range && "xaxis.range[0]" in changes) {
      range = [changes["xaxis.range[0]"], changes["xaxis.range[1]"]];
    }
    if (!range) return;
    const start = plotValueToMs(range[0]);
    const end = plotValueToMs(range[1]);
    if (!(end > start)) return;
    clearTimeout(zoomTimer);
    zoomTimer = setTimeout(() => setView(start, end, { home: false }), ZOOM_DELAY_MS);
  });

  // Double-click: back to the range chosen before zooming
  graph.on("plotly_doubleclick", goHome);

  graph.on("plotly_hover", (event) => {
    if (!event.points.length) return;
    state.cursor = plotValueToMs(event.points[0].x);
    if (!state.pinned) showImage(nearestImage(state.cursor));
    else updateImageNote();
  });

  graph.on("plotly_click", (event) => {
    if (!event.points.length) return;
    state.cursor = plotValueToMs(event.points[0].x);
    if (state.pinned) {
      state.pinned = false;
      showImage(nearestImage(state.cursor));
    } else {
      showImage(nearestImage(state.cursor));
      state.pinned = state.imageIndex >= 0;
    }
    updateImageNote();
  });
}

// ---------------------------------------------------------------------------
// The image panel
// ---------------------------------------------------------------------------

function setImages(images) {
  const shown = state.images[state.imageIndex];
  state.images = images;
  let index = shown ? images.findIndex((i) => i.image_id === shown.image_id) : -1;
  if (index < 0) {
    state.pinned = false;
    // Start with the newest image on screen
    index = nearestImage(state.cursor ?? state.view.end);
  }
  state.imageIndex = -2;  // force a redraw
  showImage(index);
}

// Index of the image closest in time to `ms`; -1 if there are none
function nearestImage(ms) {
  const images = state.images;
  if (!images.length) return -1;
  let low = 0;
  let high = images.length - 1;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (images[mid].ts < ms) low = mid + 1;
    else high = mid;
  }
  if (low > 0 && ms - images[low - 1].ts <= images[low].ts - ms) low -= 1;
  return low;
}

function showImage(index) {
  if (index === state.imageIndex) {
    updateImageNote();
    return;
  }
  state.imageIndex = index;
  const image = state.images[index];
  $("image").hidden = !image;
  $("image-placeholder").hidden = Boolean(image);
  $("image-link").hidden = !image;
  $("image-prev").disabled = !image || index === 0;
  $("image-next").disabled = !image || index === state.images.length - 1;
  if (image) {
    // A small copy beside the graph; the full image only on "Open full size"
    $("image").src = `thumbs/${image.image_id}`;
    $("image-link").href = `images/${image.image_id}`;
    $("image-time").textContent = formatTime(image.ts);
  } else {
    $("image").removeAttribute("src");
    $("image-time").textContent = " ";
  }
  updateImageNote();
  if (graph.dataset.wired) Plotly.relayout(graph, { shapes: imageMarker() });
}

function updateImageNote() {
  const image = state.images[state.imageIndex];
  const parts = [];
  if (image) {
    if (image.mode) parts.push(`mode: ${image.mode}`);
    if (state.cursor !== null && !state.pinned) {
      const delta = image.ts - state.cursor;
      if (Math.abs(delta) >= 1000) {
        parts.push(`${formatSpan(delta)} ${delta < 0 ? "before" : "after"} the pointer`);
      }
    }
  }
  $("image-note").textContent = parts.join(" · ");
  $("image-pin").hidden = !state.pinned;
}

function stepImage(direction) {
  const index = state.imageIndex + direction;
  if (index < 0 || index >= state.images.length) return;
  state.pinned = true;
  showImage(index);
}
$("image-prev").addEventListener("click", () => stepImage(-1));
$("image-next").addEventListener("click", () => stepImage(1));

document.addEventListener("keydown", (event) => {
  if (event.target.matches("input, select, textarea")) return;
  if (event.key === "ArrowLeft") stepImage(-1);
  else if (event.key === "ArrowRight") stepImage(1);
  else if (event.key === "Escape" && state.pinned) {
    state.pinned = false;
    updateImageNote();
  } else return;
  event.preventDefault();
});

// ---------------------------------------------------------------------------
// The latest-values table
// ---------------------------------------------------------------------------

async function loadLatest() {
  let rows;
  try {
    rows = await getJson("api/latest");
  } catch (error) {
    showBanner(`Could not load the latest values: ${error.message}`);
    return;
  }
  const body = $("latest-body");
  body.replaceChildren();
  const now = Date.now();
  for (const row of rows) {
    const tr = document.createElement("tr");
    const cell = (text, className) => {
      const td = document.createElement("td");
      td.textContent = text;
      if (className) td.className = className;
      tr.append(td);
    };
    let value = "";
    if (row.ts === null) value = "";
    else if (row.value === null || row.quality !== 0) value = "failed read";
    else if (row.kind === "binary") value = row.value ? "on" : "off";
    else value = row.value.toLocaleString([], { maximumFractionDigits: 2 });
    cell(row.name);
    cell(value, "num");
    cell(row.kind === "binary" ? "" : (row.unit || ""));
    cell(row.ts === null ? "none in the last day" : `${formatSpan(now - row.ts)} ago`,
         row.ts === null || now - row.ts > 5 * 60000 ? "stale" : "");
    body.append(tr);
  }
}
$("latest-refresh").addEventListener("click", loadLatest);

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

async function start() {
  try {
    state.sensors = await getJson("api/sensors");
  } catch (error) {
    showBanner(`Could not load the channel list: ${error.message}. Reload the page to try again.`);
    return;
  }
  const saved = readHash();
  const known = new Set(state.sensors.map((s) => s.name));
  state.selected = saved.names.filter((n) => known.has(n));
  if (!state.selected.length) {
    state.selected = state.sensors.slice(0, 6).map((s) => s.name);
  }
  buildPicker();
  loadLatest();
  if (saved.range) setView(saved.range.start, saved.range.end);
  else showLastHours(saved.hours || 6);
}

// Redraw with the other set of colors when the system switches light/dark
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  buildPicker();
  if (state.lastData) draw(state.lastData);
});

start();
