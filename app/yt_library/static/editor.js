/* yt-library crop & boost editor (ES module). Opened at /edit?id=<song id>.
 * All user/YouTube-provided text is inserted with textContent only. */

const WS_URL = "https://cdn.jsdelivr.net/npm/wavesurfer.js@7/dist/wavesurfer.esm.js";
const REGIONS_URL = "https://cdn.jsdelivr.net/npm/wavesurfer.js@7/dist/plugins/regions.esm.js";

const $ = (id) => document.getElementById(id);

// ---------- helpers ----------
async function api(method, path, body) {
  const opts = { method, headers: { Accept: "application/json" }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new Error("Network error: could not reach the server.");
  }
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (e) { data = null; }
  }
  if (!res.ok) {
    let msg = "";
    if (data && data.detail !== undefined) {
      if (typeof data.detail === "string") msg = data.detail;
      else if (Array.isArray(data.detail)) msg = data.detail.map((d) => (d && d.msg) || JSON.stringify(d)).join("; ");
      else msg = JSON.stringify(data.detail);
    }
    throw new Error(msg || `Request failed (${res.status} ${res.statusText})`);
  }
  return data;
}

function setMsg(node, text, kind) {
  node.textContent = text || "";
  node.className = "msg" + (kind ? " " + kind : "");
}

/** Format seconds as m:ss.mmm (minutes may exceed 59). */
function fmtTime(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const totalMs = Math.round(sec * 1000);
  const m = Math.floor(totalMs / 60000);
  const s = Math.floor((totalMs % 60000) / 1000);
  const ms = totalMs % 1000;
  return `${m}:${String(s).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
}

function fmtDuration(sec) {
  if (!isFinite(sec)) return "–";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = String(sec % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

/** Parse "ss(.fff)", "m:ss(.fff)" or "h:mm:ss(.fff)". Returns seconds or throws. */
function parseTime(str) {
  const t = String(str).trim().replace(",", ".");
  if (!t) throw new Error("is empty");
  const parts = t.split(":");
  if (parts.length > 3) throw new Error("has too many ':' parts");
  const last = parts.pop();
  if (!/^\d+(\.\d+)?$/.test(last) && !/^\.\d+$/.test(last)) throw new Error("is not a valid time (use m:ss.mmm)");
  let sec = parseFloat(last);
  if (parts.length && sec >= 60) throw new Error("has seconds of 60 or more");
  let mult = 60;
  for (let i = parts.length - 1; i >= 0; i--) {
    const p = parts[i];
    if (!/^\d+$/.test(p)) throw new Error("is not a valid time (use m:ss.mmm)");
    const v = parseInt(p, 10);
    if (parts.length === 2 && i === 1 && v >= 60) throw new Error("has minutes of 60 or more");
    sec += v * mult;
    mult *= 60;
  }
  return sec;
}

// ---------- state ----------
const params = new URLSearchParams(location.search);
const songId = params.get("id");
let song = null;
let config = null;
let duration = 0;         // seconds; from song metadata, replaced by decoded duration
let selStart = 0;
let selEnd = 0;
let ws = null;            // WaveSurfer instance (may stay null if the CDN/decoding fails)
let region = null;
let playingSelection = false;
let previewToken = 0;
let saveToken = 0;

const EPS = 0.01;

// ---------- selection sync ----------
function updateLength() {
  $("sel-len").textContent = selEnd > selStart ? fmtTime(selEnd - selStart) : "–";
}

function writeInputs() {
  $("start").value = fmtTime(selStart);
  $("end").value = fmtTime(selEnd);
  $("start").classList.remove("invalid");
  $("end").classList.remove("invalid");
  updateLength();
  validate();
}

function setSelection(start, end, { fromRegion = false } = {}) {
  selStart = Math.max(0, start);
  selEnd = Math.min(duration || end, end);
  if (region && !fromRegion) {
    try { region.setOptions({ start: selStart, end: selEnd }); } catch (e) { /* ignore */ }
  }
  writeInputs();
}

function onTimeInput(which) {
  const input = $(which);
  let v;
  try {
    v = parseTime(input.value);
  } catch (e) {
    input.classList.add("invalid");
    setMsg($("time-msg"), `${which === "start" ? "Start" : "End"} ${e.message}.`, "error");
    return;
  }
  const s = which === "start" ? v : selStart;
  const en = which === "end" ? v : selEnd;
  if (s < 0 || en > duration + EPS) {
    input.classList.add("invalid");
    setMsg($("time-msg"), `Times must be between 0:00.000 and ${fmtTime(duration)}.`, "error");
    return;
  }
  if (s >= en) {
    input.classList.add("invalid");
    setMsg($("time-msg"), "Start must be before end.", "error");
    return;
  }
  setSelection(s, Math.min(en, duration));
}

// ---------- validation ----------
function readControls() {
  return {
    gain: parseFloat($("gain").value) || 0,
    normalize: $("normalize").checked,
    target: parseFloat($("target").value),
    fade_in: parseFloat($("fade-in").value) || 0,
    fade_out: parseFloat($("fade-out").value) || 0,
  };
}

/** Returns an error string, or "" when valid. Also updates the on-page messages. */
function validate() {
  const errs = [];
  const timeErr = $("start").classList.contains("invalid") || $("end").classList.contains("invalid");
  if (!timeErr) setMsg($("time-msg"), "", "error");
  if (timeErr) errs.push($("time-msg").textContent || "Fix the start/end times.");
  if (!(selStart < selEnd)) errs.push("Start must be before end.");

  const c = readControls();
  const e = config.edit;
  const ctrlErrs = [];
  if (c.fade_in + c.fade_out > selEnd - selStart + 1e-6) {
    ctrlErrs.push(`Fade in + fade out (${(c.fade_in + c.fade_out).toFixed(1)} s) is longer than the selection (${(selEnd - selStart).toFixed(1)} s).`);
  }
  if (c.gain < e.gain_min || c.gain > e.gain_max) ctrlErrs.push(`Gain must be between ${e.gain_min} and ${e.gain_max} dB.`);
  if (c.normalize) {
    if (!isFinite(c.target)) ctrlErrs.push("Target loudness must be a number.");
    else if (c.target < e.target_min || c.target > e.target_max) ctrlErrs.push(`Target loudness must be between ${e.target_min} and ${e.target_max} LUFS.`);
  }
  $("target").classList.toggle("invalid", c.normalize && (!isFinite(c.target) || c.target < e.target_min || c.target > e.target_max));
  setMsg($("ctrl-msg"), ctrlErrs.join(" "), "error");
  errs.push(...ctrlErrs);
  const bad = errs.length > 0;
  $("preview-btn").disabled = bad || previewBusy;
  $("save-btn").disabled = bad || saveBusy;
  return errs.join(" ");
}

function buildBody() {
  const c = readControls();
  const atEnd = selEnd >= duration - EPS;
  return {
    start: Math.round(selStart * 1000) / 1000,
    end: atEnd ? null : Math.round(selEnd * 1000) / 1000,
    gain: c.gain,
    normalize: c.normalize,
    target: isFinite(c.target) ? c.target : config.edit.target_default,
    fade_in: c.fade_in,
    fade_out: c.fade_out,
  };
}

// ---------- controls ----------
function setupControls() {
  const e = config.edit;
  const gain = $("gain");
  gain.min = e.gain_min; gain.max = e.gain_max; gain.step = 0.5; gain.value = 0;
  for (const id of ["fade-in", "fade-out"]) {
    const r = $(id);
    r.min = e.fade_min; r.max = e.fade_max; r.step = 0.1; r.value = e.fade_min;
  }
  const target = $("target");
  target.min = e.target_min; target.max = e.target_max; target.value = e.target_default;
  $("target-note").textContent = `Only used when normalize is on. Range ${e.target_min} to ${e.target_max}, default ${e.target_default}.`;
  if (e.preview_seconds) $("preview-label").textContent = `Preview: first ${e.preview_seconds} seconds of the processed result`;

  const showOut = () => {
    const g = parseFloat(gain.value);
    $("gain-out").textContent = (g > 0 ? "+" : "") + g.toFixed(1) + " dB";
    $("fade-in-out").textContent = parseFloat($("fade-in").value).toFixed(1) + " s";
    $("fade-out-out").textContent = parseFloat($("fade-out").value).toFixed(1) + " s";
    target.disabled = !$("normalize").checked;
  };
  for (const id of ["gain", "fade-in", "fade-out", "normalize", "target"]) {
    $(id).addEventListener("input", () => { showOut(); validate(); });
    $(id).addEventListener("change", () => { showOut(); validate(); });
  }
  showOut();

  for (const which of ["start", "end"]) {
    const input = $(which);
    input.addEventListener("change", () => { onTimeInput(which); validate(); });
    input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); onTimeInput(which); validate(); } });
    input.addEventListener("input", () => input.classList.remove("invalid"));
  }
  $("reset-btn").addEventListener("click", () => {
    $("start").classList.remove("invalid");
    $("end").classList.remove("invalid");
    setSelection(0, duration);
  });
  $("preview-btn").addEventListener("click", runPreview);
  $("save-btn").addEventListener("click", runSave);
}

// ---------- waveform ----------
function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

async function setupWaveform() {
  let WaveSurfer, RegionsPlugin;
  try {
    [{ default: WaveSurfer }, { default: RegionsPlugin }] = await Promise.all([import(WS_URL), import(REGIONS_URL)]);
  } catch (e) {
    setMsg($("wave-msg"), "Could not load the waveform library; you can still type exact start/end times below.", "error");
    $("waveform").classList.add("hidden");
    return;
  }
  setMsg($("wave-msg"), "Loading waveform…", "info");
  const regions = RegionsPlugin.create();
  const accent = cssVar("--accent", "#2f6fdb");
  ws = WaveSurfer.create({
    container: "#waveform",
    url: song.file_url,
    height: 128,
    waveColor: cssVar("--muted", "#888"),
    progressColor: accent,
    cursorColor: cssVar("--text", "#000"),
    normalize: true,
    dragToSeek: true,
    plugins: [regions],
  });

  ws.on("error", (err) => {
    setMsg($("wave-msg"), "Could not decode the audio in this browser (" + String(err && err.message || err) + "). You can still type exact times.", "error");
  });

  ws.on("decode", () => {
    const d = ws.getDuration();
    if (d && isFinite(d)) {
      // If the decoded duration differs, trust it; keep the current selection clamped.
      const wasWhole = selEnd >= duration - EPS;
      duration = d;
      $("song-duration").textContent = fmtDuration(duration);
      if (wasWhole || selEnd > duration) selEnd = duration;
      if (selStart >= selEnd) selStart = 0;
    }
    setMsg($("wave-msg"), "");
    regions.clearRegions();
    region = regions.addRegion({
      start: selStart,
      end: selEnd,
      color: "color-mix(in srgb, " + accent + " 22%, transparent)",
      drag: true,
      resize: true,
    });
    const sync = () => {
      $("start").classList.remove("invalid");
      $("end").classList.remove("invalid");
      setSelection(region.start, region.end, { fromRegion: true });
    };
    region.on("update", sync);
    region.on("update-end", sync);
    writeInputs();
    $("play-btn").disabled = false;
    $("play-sel-btn").disabled = false;
  });

  const updatePlayLabel = () => { $("play-btn").textContent = ws.isPlaying() ? "Pause" : "Play"; };
  ws.on("play", updatePlayLabel);
  ws.on("pause", () => { playingSelection = false; updatePlayLabel(); });
  ws.on("finish", () => { playingSelection = false; updatePlayLabel(); });
  ws.on("timeupdate", (t) => {
    if (playingSelection && t >= selEnd) {
      playingSelection = false;
      ws.pause();
      ws.setTime(selEnd);
    }
  });
  ws.on("interaction", () => { playingSelection = false; });

  $("play-btn").addEventListener("click", () => { playingSelection = false; ws.playPause(); });
  $("play-sel-btn").addEventListener("click", () => {
    const pa = $("preview-audio");
    if (!pa.paused) pa.pause();
    ws.setTime(selStart);
    playingSelection = true;
    ws.play();
  });
}

// ---------- job polling (preview / save) ----------
let previewBusy = false;
let saveBusy = false;

async function pollJob(jobId, isCurrent, onUpdate) {
  for (;;) {
    await new Promise((r) => setTimeout(r, 1000));
    if (!isCurrent()) return null;
    let job;
    try {
      job = await api("GET", "/api/jobs/" + encodeURIComponent(jobId));
    } catch (e) {
      onUpdate(null, e);
      continue; // transient error: keep polling
    }
    if (!isCurrent()) return null;
    onUpdate(job, null);
    if (job.status === "done" || job.status === "failed" || job.status === "partial") return job;
  }
}

function jobStatusText(job, what) {
  if (!job) return what + "…";
  if (job.status === "queued") return `${what}: queued` + (job.queue_position ? ` (position ${job.queue_position})` : "") + "…";
  if (job.status === "running") return `${what}: processing…`;
  return `${what}: ${job.status}`;
}

async function runPreview() {
  const err = validate();
  if (err) { setMsg($("preview-msg"), err, "error"); return; }
  const token = ++previewToken;
  previewBusy = true;
  validate();
  if (ws && ws.isPlaying()) ws.pause();
  setMsg($("preview-msg"), "Preview: submitting…", "info");
  try {
    const job = await api("POST", "/api/songs/" + encodeURIComponent(songId) + "/preview", buildBody());
    setMsg($("preview-msg"), jobStatusText(job, "Preview"), "info");
    const fin = (job.status === "done" || job.status === "failed") ? job : await pollJob(job.id, () => token === previewToken, (j, e) => {
      if (e) setMsg($("preview-msg"), "Preview: " + e.message + " (retrying)", "error");
      else setMsg($("preview-msg"), jobStatusText(j, "Preview"), "info");
    });
    if (!fin || token !== previewToken) return;
    if (fin.status === "done" && fin.preview_url) {
      setMsg($("preview-msg"), "Preview ready.", "ok");
      const pa = $("preview-audio");
      pa.src = fin.preview_url;
      $("preview-box").classList.remove("hidden");
      pa.play().catch(() => { /* autoplay blocked: user can press play */ });
    } else {
      setMsg($("preview-msg"), "Preview failed: " + (fin.error || "unknown error"), "error");
    }
  } catch (e) {
    setMsg($("preview-msg"), "Preview failed: " + e.message, "error");
  } finally {
    if (token === previewToken) { previewBusy = false; validate(); }
  }
}

async function runSave() {
  const err = validate();
  if (err) { setMsg($("save-msg"), err, "error"); return; }
  const token = ++saveToken;
  saveBusy = true;
  validate();
  $("save-done").classList.add("hidden");
  setMsg($("save-msg"), "Saving: submitting…", "info");
  try {
    const job = await api("POST", "/api/songs/" + encodeURIComponent(songId) + "/edit", buildBody());
    setMsg($("save-msg"), jobStatusText(job, "Saving"), "info");
    const fin = (job.status === "done" || job.status === "failed") ? job : await pollJob(job.id, () => token === saveToken, (j, e) => {
      if (e) setMsg($("save-msg"), "Saving: " + e.message + " (retrying)", "error");
      else setMsg($("save-msg"), jobStatusText(j, "Saving"), "info");
    });
    if (!fin || token !== saveToken) return;
    if (fin.status === "done" && fin.result_song_id !== null && fin.result_song_id !== undefined) {
      setMsg($("save-msg"), "Saved as a new song. The original is unchanged.", "ok");
      $("new-song-link").href = "/#song-" + encodeURIComponent(fin.result_song_id);
      $("save-done").classList.remove("hidden");
    } else {
      setMsg($("save-msg"), "Saving failed: " + (fin.error || "unknown error"), "error");
    }
  } catch (e) {
    setMsg($("save-msg"), "Saving failed: " + e.message, "error");
  } finally {
    if (token === saveToken) { saveBusy = false; validate(); }
  }
}

// ---------- boot ----------
async function boot() {
  if (!songId) {
    setMsg($("load-msg"), "No song selected. Open the editor from the library.", "error");
    return;
  }
  try {
    [song, config] = await Promise.all([
      api("GET", "/api/songs/" + encodeURIComponent(songId)),
      api("GET", "/api/config"),
    ]);
  } catch (e) {
    setMsg($("load-msg"), "Could not load song: " + e.message, "error");
    return;
  }
  config.edit = Object.assign({
    gain_min: -20, gain_max: 20, target_min: -30, target_max: -5, target_default: -14,
    fade_min: 0, fade_max: 10, preview_seconds: 15,
  }, config.edit || {});

  document.title = (song.title || "Song") + " · Edit · yt-library";
  $("song-title").textContent = song.title || "(untitled)";
  $("song-format").textContent = song.format || song.ext || "–";
  duration = Number(song.duration) || 0;
  $("song-duration").textContent = fmtDuration(duration);
  if (song.parent_id !== null && song.parent_id !== undefined) {
    $("song-parent").textContent = song.parent_title || "another song";
    $("song-from").classList.remove("hidden");
  }
  $("back-link").href = "/#song-" + encodeURIComponent(song.id);

  $("load-msg").classList.add("hidden");
  $("editor").classList.remove("hidden");

  setupControls();
  selStart = 0;
  selEnd = duration;
  writeInputs();
  setupWaveform();
}

boot();
