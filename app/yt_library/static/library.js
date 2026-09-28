/* yt-library main page. Vanilla JS, no build step.
 * All user/YouTube-provided text is inserted with textContent only. */
(function () {
  "use strict";

  // ---------- helpers ----------
  const $ = (id) => document.getElementById(id);

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") node.className = v;
        else if (k === "text") node.textContent = v;
        else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
        else if (k in node && typeof v !== "string") node[k] = v;
        else node.setAttribute(k, v === true ? "" : String(v));
      }
    }
    for (const c of children) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

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
    let data = null;
    const text = await res.text();
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

  // Clock skew: offset = server clock - local clock (seconds).
  let clockOffset = 0;
  function syncClock(serverTime) {
    if (typeof serverTime === "number" && isFinite(serverTime)) {
      clockOffset = serverTime - Date.now() / 1000;
    }
  }
  const serverNow = () => Date.now() / 1000 + clockOffset;

  function fmtDuration(sec) {
    if (sec === null || sec === undefined || !isFinite(sec)) return "–";
    sec = Math.round(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const ss = String(s).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
  }

  function fmtSize(bytes) {
    if (bytes === null || bytes === undefined || !isFinite(bytes)) return "–";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = bytes;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return (i === 0 ? v : v.toFixed(v < 10 ? 1 : 0)) + " " + units[i];
  }

  function fmtAgo(ts) {
    if (!ts) return "–";
    const d = serverNow() - ts;
    if (d < 60) return "just now";
    if (d < 3600) return Math.floor(d / 60) + " min ago";
    if (d < 86400) return Math.floor(d / 3600) + " h ago";
    if (d < 7 * 86400) return Math.floor(d / 86400) + " d ago";
    return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  }

  function fmtRemaining(sec) {
    if (sec <= 0) return "expired";
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m`;
    return "<1m";
  }

  const fmtDateTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "");

  function setMsg(node, text, kind) {
    node.textContent = text || "";
    node.className = "msg" + (kind ? " " + kind : "");
  }

  // ---------- generic dialog ----------
  const dlg = $("confirm");
  function confirmDialog({ title, text, items, okLabel, danger, extra }) {
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text || "";
    const list = $("confirm-list");
    list.replaceChildren();
    const MAX = 30;
    (items || []).slice(0, MAX).forEach((t) => list.append(el("li", { text: t })));
    if (items && items.length > MAX) list.append(el("li", { class: "muted", text: `…and ${items.length - MAX} more` }));
    list.classList.toggle("hidden", !items || !items.length);
    const ok = $("confirm-ok");
    ok.textContent = okLabel || "OK";
    ok.className = danger ? "danger solid" : "primary";
    // optional extra controls (e.g. lifetime select) go before the buttons
    const old = dlg.querySelector(".dlg-extra");
    if (old) old.remove();
    if (extra) {
      const wrap = el("div", { class: "dlg-extra" }, extra);
      list.after(wrap);
    }
    dlg.returnValue = "";
    return new Promise((resolve) => {
      dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
      if (typeof dlg.showModal === "function") dlg.showModal();
      else resolve(window.confirm(title + "\n" + (text || "")));
    });
  }

  // ---------- config ----------
  let config = null;

  function lifetimeOptions(select, selected) {
    select.replaceChildren();
    for (const lt of config.lifetimes || []) {
      select.append(el("option", { value: lt.key, text: lt.label || lt.key, selected: lt.key === selected }));
    }
  }

  async function loadConfig() {
    config = await api("GET", "/api/config");
    syncClock(config.server_time);
    const fmt = $("format");
    fmt.replaceChildren();
    for (const f of config.formats || []) {
      fmt.append(el("option", { value: f, text: f, selected: f === config.default_format }));
    }
    lifetimeOptions($("lifetime"), config.default_lifetime);
    lifetimeOptions($("bulk-lifetime"), config.default_lifetime);
  }

  // ---------- add music (probe + submit) ----------
  let probeResult = null;

  function resetProbe() {
    probeResult = null;
    $("probe").classList.add("hidden");
    $("probe-list").replaceChildren();
    $("probe-summary").replaceChildren();
  }

  function selectedScopeSingle() {
    if (!probeResult) return false;
    if (probeResult.video_in_playlist) {
      const r = document.querySelector('input[name="scope"]:checked');
      return !r || r.value === "single";
    }
    return !probeResult.is_playlist;
  }

  function renderProbeList() {
    const list = $("probe-list");
    list.replaceChildren();
    if (!probeResult) return;
    let entries = probeResult.entries || [];
    const single = selectedScopeSingle();
    if (single && probeResult.video_in_playlist) {
      const vid = probeResult.video_in_playlist.video_id;
      const match = entries.filter((e) => e.video_id === vid);
      entries = match.length ? match : [{ index: 1, video_id: vid, title: probeResult.video_in_playlist.title, existing: [] }];
    }
    for (const e of entries) {
      const li = el("li", { value: e.index || null });
      li.append(el("span", { text: e.title || e.video_id || "(untitled)" }));
      if (e.uploader) li.append(el("span", { class: "muted small", text: " · " + e.uploader }));
      li.append(el("span", { class: "muted small", text: " · " + fmtDuration(e.duration) }));
      if (e.too_long) li.append(el("span", { class: "tag bad", text: "too long, will be skipped" }));
      for (const ex of e.existing || []) {
        const a = el("a", { href: "#song-" + ex.song_id, class: "tag ok", text: "already in library (" + ex.format + ")" });
        li.append(a);
      }
      list.append(li);
    }
    $("download-btn").textContent = single ? "Download" : `Download ${probeResult.track_count ?? entries.length} tracks`;
  }

  function renderProbe(p) {
    probeResult = p;
    const sum = $("probe-summary");
    sum.replaceChildren();
    const count = p.track_count ?? (p.entries || []).length;
    const tooLong = (p.entries || []).filter((e) => e.too_long).length;
    const existing = (p.entries || []).filter((e) => (e.existing || []).length).length;
    sum.append(el("strong", { text: p.title || p.url }));
    sum.append(el("div", { class: "muted small", text:
      (p.is_playlist ? `Playlist · ${count} track${count === 1 ? "" : "s"}` : "Single video") +
      (tooLong ? ` · ${tooLong} too long` : "") +
      (existing ? ` · ${existing} already in library` : "") }));
    const maxT = config && config.limits && config.limits.max_tracks_per_job;
    if (p.is_playlist && maxT && count > maxT) {
      sum.append(el("div", { class: "msg error", text: `This playlist has more than ${maxT} tracks, the maximum per job.` }));
    }
    const choice = $("probe-choice");
    if (p.video_in_playlist) {
      choice.classList.remove("hidden");
      $("choice-single").textContent = "Just this video: " + (p.video_in_playlist.title || p.video_in_playlist.video_id);
      $("choice-all").textContent = `Whole playlist (${count} track${count === 1 ? "" : "s"})`;
      document.querySelector('input[name="scope"][value="single"]').checked = true;
    } else {
      choice.classList.add("hidden");
    }
    renderProbeList();
    $("probe").classList.remove("hidden");
  }

  $("add-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const url = $("url").value.trim();
    if (!url) return;
    resetProbe();
    const btn = $("check-btn");
    btn.disabled = true;
    setMsg($("add-msg"), "Checking link…", "info");
    try {
      const p = await api("POST", "/api/probe", { url });
      setMsg($("add-msg"), "");
      renderProbe(p);
    } catch (e) {
      setMsg($("add-msg"), e.message, "error");
    } finally {
      btn.disabled = false;
    }
  });

  $("url").addEventListener("input", () => { if (probeResult) resetProbe(); setMsg($("add-msg"), ""); });
  document.querySelectorAll('input[name="scope"]').forEach((r) => r.addEventListener("change", renderProbeList));
  $("probe-cancel").addEventListener("click", () => { resetProbe(); setMsg($("add-msg"), ""); });

  $("download-btn").addEventListener("click", async () => {
    if (!probeResult) return;
    const btn = $("download-btn");
    btn.disabled = true;
    try {
      const body = {
        url: probeResult.url || $("url").value.trim(),
        format: $("format").value,
        lifetime: $("lifetime").value,
        single: selectedScopeSingle(),
      };
      const job = await api("POST", "/api/jobs", body);
      resetProbe();
      $("url").value = "";
      const pos = job && job.queue_position ? ` (position ${job.queue_position} in queue)` : "";
      setMsg($("add-msg"), "Download started" + pos + ". Progress is shown under Jobs; you can close this page and come back.", "ok");
      if (job && job.id) openJobs.add(job.id);
      refreshJobsNow();
    } catch (e) {
      setMsg($("add-msg"), e.message, "error");
    } finally {
      btn.disabled = false;
    }
  });

  // ---------- jobs ----------
  const ACTIVE = new Set(["queued", "running"]);
  const openJobs = new Set();    // job ids the user (or we) expanded
  const closedJobs = new Set();  // job ids the user explicitly collapsed
  let lastJobStatus = null;      // Map id -> status from previous poll
  let jobsTimer = null;

  function songLink(id, label) {
    return el("a", { href: "#song-" + id, text: label });
  }

  function renderJob(job) {
    const active = ACTIVE.has(job.status);
    const isOpen = !closedJobs.has(job.id) && (active || openJobs.has(job.id));
    const det = el("details", { class: "job", open: isOpen });
    det.dataset.id = job.id;
    let state = isOpen; // the initial `open` assignment also fires a toggle event; ignore it
    det.addEventListener("toggle", () => {
      if (det.open === state) return;
      state = det.open;
      if (det.open) { openJobs.add(job.id); closedJobs.delete(job.id); }
      else { openJobs.delete(job.id); closedJobs.add(job.id); }
    });

    const tracks = job.tracks || [];
    const doneN = tracks.filter((t) => t.status === "done" || t.status === "duplicate").length;
    const summary = el("summary", null,
      el("span", { class: "tag", text: job.kind }),
      el("span", { class: "jtitle", text: job.title || job.url || "(untitled)" }),
      el("span", { class: "status " + job.status, text: job.status }),
    );
    if (job.status === "queued" && job.queue_position) {
      summary.append(el("span", { class: "muted small", text: `#${job.queue_position} in queue` }));
    }
    if (tracks.length > 1) {
      summary.append(el("span", { class: "muted small", text: `${doneN}/${tracks.length} tracks` }));
    }
    det.append(summary);

    const body = el("div", { class: "jbody" });
    const metaBits = [];
    if (job.format) metaBits.push(job.format);
    if (job.lifetime) metaBits.push("keep " + job.lifetime);
    if (job.created_at) metaBits.push("created " + fmtDateTime(job.created_at));
    if (job.finished_at) metaBits.push("finished " + fmtDateTime(job.finished_at));
    body.append(el("div", { class: "muted small", text: metaBits.join(" · ") }));
    if (job.url && /^https?:\/\//i.test(job.url)) {
      body.append(el("div", { class: "small" },
        el("a", { href: job.url, target: "_blank", rel: "noopener noreferrer", text: job.url })));
    }
    if (job.error) body.append(el("div", { class: "err", text: job.error }));
    if (job.kind === "edit" && job.result_song_id) {
      body.append(el("div", null, "Result: ", songLink(job.result_song_id, "open new song")));
    }
    const failedN = tracks.filter((t) => t.status === "failed").length;
    const canRetry = job.kind === "download" && !active && failedN > 0;
    if (canRetry) {
      const btn = el("button", { type: "button", class: "small", text: `Retry ${failedN} failed track${failedN === 1 ? "" : "s"}` });
      btn.addEventListener("click", () => retryJob(job.id, null, btn));
      body.append(el("div", { class: "jactions" }, btn));
    }
    if (tracks.length) {
      const ul = el("ul", { class: "tracks" });
      for (const t of tracks) {
        const li = el("li", null,
          el("span", { class: "muted small", text: (t.index ?? "") + ". " }),
          el("span", { text: t.title || t.video_id || "(untitled)" }), " ",
          el("span", { class: "status " + t.status, text: t.status }),
        );
        if (t.song_id && (t.status === "done" || t.status === "duplicate")) {
          li.append(" ", songLink(t.song_id, t.status === "duplicate" ? "existing song" : "song"));
        }
        if (canRetry && t.status === "failed" && failedN > 1) {
          const rb = el("button", { type: "button", class: "small link", text: "Retry" });
          rb.addEventListener("click", () => retryJob(job.id, [t.index], rb));
          li.append(" ", rb);
        }
        if (t.error) li.append(el("div", { class: "err", text: t.error }));
        ul.append(li);
      }
      body.append(ul);
    }
    det.append(body);
    return det;
  }

  async function retryJob(jobId, trackIndexes, btn) {
    btn.disabled = true;
    try {
      await api("POST", `/api/jobs/${encodeURIComponent(jobId)}/retry`, trackIndexes ? { tracks: trackIndexes } : {});
      openJobs.add(jobId);
      closedJobs.delete(jobId);
      setMsg($("jobs-msg"), "");
    } catch (e) {
      setMsg($("jobs-msg"), "Retry failed: " + e.message, "error");
      btn.disabled = false;
    }
    refreshJobsNow();
  }

  async function fetchJobs() {
    let anyActive = false;
    try {
      const data = await api("GET", "/api/jobs");
      setMsg($("jobs-msg"), "", "error");
      const jobs = ((data && data.jobs) || []).filter((j) => j.kind !== "preview");
      anyActive = jobs.some((j) => ACTIVE.has(j.status));

      // Detect jobs that finished since the last poll -> refresh library.
      let finished = false;
      const nowStatus = new Map();
      for (const j of jobs) {
        nowStatus.set(j.id, j.status);
        if (lastJobStatus) {
          const prev = lastJobStatus.get(j.id);
          if (prev && ACTIVE.has(prev) && !ACTIVE.has(j.status)) finished = true;
          if (!prev && !ACTIVE.has(j.status)) finished = true; // appeared and finished between polls
        }
      }
      lastJobStatus = nowStatus;
      if (finished) loadSongs();

      const box = $("jobs");
      box.replaceChildren();
      if (!jobs.length) box.append(el("p", { class: "muted small", text: "No jobs yet." }));
      for (const j of jobs) box.append(renderJob(j));
      const q = jobs.filter((j) => j.status === "queued").length;
      const r = jobs.filter((j) => j.status === "running").length;
      $("jobs-note").textContent = anyActive ? `${r} running, ${q} queued` : "";
    } catch (e) {
      setMsg($("jobs-msg"), "Could not load jobs: " + e.message, "error");
    }
    return anyActive;
  }

  async function pollJobs() {
    clearTimeout(jobsTimer);
    const active = await fetchJobs();
    clearTimeout(jobsTimer);
    jobsTimer = setTimeout(pollJobs, active ? 2000 : 15000);
  }
  function refreshJobsNow() { pollJobs(); }

  // ---------- library ----------
  let songs = [];
  const selected = new Set();
  let sortKey = "created_at";
  let sortDir = "desc";
  let filterText = "";
  let playingId = null;
  let pendingHash = null;

  function matchesFilter(s) {
    if (!filterText) return true;
    const hay = [s.title, s.uploader, s.format, s.ext, s.parent_title].filter(Boolean).join(" ").toLowerCase();
    return filterText.split(/\s+/).every((w) => hay.includes(w));
  }

  function sortedFiltered() {
    const list = songs.filter(matchesFilter);
    const dir = sortDir === "asc" ? 1 : -1;
    list.sort((a, b) => {
      let va = a[sortKey], vb = b[sortKey];
      const na = va === null || va === undefined || va === "";
      const nb = vb === null || vb === undefined || vb === "";
      if (na && nb) return 0;
      if (na) return 1;
      if (nb) return -1;
      let c;
      if (typeof va === "number" && typeof vb === "number") c = va - vb;
      else c = String(va).localeCompare(String(vb), undefined, { sensitivity: "base", numeric: true });
      if (c === 0) c = (a.id > b.id ? 1 : a.id < b.id ? -1 : 0);
      return c * dir;
    });
    return list;
  }

  function td(label, cls, ...children) {
    return el("td", { "data-label": label, class: cls }, ...children);
  }

  function renderSongs() {
    const body = $("lib-body");
    const list = sortedFiltered();
    const now = serverNow();
    body.replaceChildren();
    for (const s of list) {
      const remaining = (s.expires_at || 0) - now;
      const tr = el("tr", { id: "song-" + s.id });
      if (remaining < 86400) tr.classList.add("expiring");
      if (String(s.id) === String(playingId)) tr.classList.add("playing");

      const cb = el("input", { type: "checkbox", "aria-label": "Select " + (s.title || "song"), checked: selected.has(s.id) });
      cb.addEventListener("change", () => {
        if (cb.checked) selected.add(s.id); else selected.delete(s.id);
        updateSelectionUI();
      });

      const titleCell = td("Title", "title", el("span", { text: s.title || "(untitled)" }));
      if (s.parent_id) {
        const from = el("span", { class: "from" }, "from ");
        if (songs.some((o) => o.id === s.parent_id)) from.append(songLink(s.parent_id, s.parent_title || "original"));
        else from.append(el("span", { text: s.parent_title || "original" }));
        titleCell.append(from);
      }

      let src = "–";
      if (s.source_url && /^https?:\/\//i.test(s.source_url)) {
        src = el("a", { href: s.source_url, target: "_blank", rel: "noopener noreferrer", text: "source" });
      }

      const actions = el("div", { class: "actions" },
        el("button", { type: "button", class: "small", text: String(s.id) === String(playingId) ? "Playing" : "Play", onclick: () => play(s) }),
        el("a", { class: "btn small", href: s.download_url, download: "", text: "Download" }),
        el("a", { class: "btn small", href: "/edit?id=" + encodeURIComponent(s.id), text: "Edit" }),
        el("button", { type: "button", class: "small", text: "Extend", onclick: () => extendSongs([s]) }),
        el("button", { type: "button", class: "small danger", text: "Delete", onclick: () => deleteSongs([s]) }),
      );

      tr.append(
        el("td", { class: "sel" }, cb),
        titleCell,
        td("Uploader", null, s.uploader || "–"),
        td("Duration", "num", fmtDuration(s.duration)),
        td("Format", null, s.format || s.ext || "–"),
        td("Size", "num", fmtSize(s.size)),
        td("Added", "nowrap", el("span", { title: fmtDateTime(s.created_at), text: fmtAgo(s.created_at) })),
        td("Expires in", "exp nowrap", el("span", { title: fmtDateTime(s.expires_at), text: fmtRemaining(remaining) })),
        td("Source", null, src),
        el("td", null, actions),
      );
      body.append(tr);
    }
    const empty = $("lib-empty");
    if (!songs.length) { empty.textContent = "The library is empty. Add some music above."; empty.classList.remove("hidden"); }
    else if (!list.length) { empty.textContent = "No songs match the filter."; empty.classList.remove("hidden"); }
    else empty.classList.add("hidden");

    document.querySelectorAll("#lib th.sortable").forEach((th) => {
      if (th.dataset.key === sortKey) th.setAttribute("aria-sort", sortDir === "asc" ? "ascending" : "descending");
      else th.removeAttribute("aria-sort");
    });
    updateSelectionUI(list);
  }

  function updateSelectionUI(visible) {
    visible = visible || sortedFiltered();
    const n = selected.size;
    $("sel-count").textContent = `${n} selected`;
    for (const id of ["bulk-zip", "bulk-extend", "bulk-delete", "bulk-clear"]) $(id).disabled = n === 0;
    const all = $("check-all");
    const visSel = visible.filter((s) => selected.has(s.id)).length;
    all.checked = visible.length > 0 && visSel === visible.length;
    all.indeterminate = visSel > 0 && visSel < visible.length;
  }

  async function loadSongs() {
    try {
      const data = await api("GET", "/api/songs");
      syncClock(data.server_time);
      songs = data.songs || [];
      const ids = new Set(songs.map((s) => s.id));
      for (const id of [...selected]) if (!ids.has(id)) selected.delete(id);
      const used = data.used_bytes || 0;
      const quota = data.quota_bytes || (config && config.limits && config.limits.quota_bytes) || 0;
      $("quota-text").textContent = quota ? `${fmtSize(used)} of ${fmtSize(quota)} used` : `${fmtSize(used)} used`;
      const m = $("quota-meter");
      m.value = quota ? Math.min(1, used / quota) : 0;
      m.classList.toggle("hidden", !quota);
      if ($("lib-msg").classList.contains("error")) setMsg($("lib-msg"), "");
      renderSongs();
      if (pendingHash) focusHash();
    } catch (e) {
      setMsg($("lib-msg"), "Could not load library: " + e.message, "error");
    }
  }

  // sorting
  document.querySelectorAll("#lib th.sortable").forEach((th) => {
    th.tabIndex = 0;
    const go = () => {
      const k = th.dataset.key;
      if (sortKey === k) sortDir = sortDir === "asc" ? "desc" : "asc";
      else { sortKey = k; sortDir = (k === "created_at" || k === "size" || k === "duration") ? "desc" : "asc"; }
      syncMobileSort();
      renderSongs();
    };
    th.addEventListener("click", go);
    th.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
  });
  function syncMobileSort() {
    const sel = $("mobile-sort");
    const v = sortKey + ":" + sortDir;
    if ([...sel.options].some((o) => o.value === v)) sel.value = v;
  }
  $("mobile-sort").addEventListener("change", (e) => {
    const [k, d] = e.target.value.split(":");
    sortKey = k; sortDir = d;
    renderSongs();
  });

  // filtering
  $("search").addEventListener("input", (e) => {
    filterText = e.target.value.trim().toLowerCase();
    renderSongs();
  });

  // selection
  $("check-all").addEventListener("change", (e) => {
    const visible = sortedFiltered();
    if (e.target.checked) visible.forEach((s) => selected.add(s.id));
    else visible.forEach((s) => selected.delete(s.id));
    renderSongs();
  });
  $("select-matching").addEventListener("click", () => {
    sortedFiltered().forEach((s) => selected.add(s.id));
    renderSongs();
  });
  $("bulk-clear").addEventListener("click", () => { selected.clear(); renderSongs(); });

  const selectedSongs = () => songs.filter((s) => selected.has(s.id));

  // bulk zip: real form post so the browser streams the download
  $("bulk-zip").addEventListener("click", () => {
    const ids = selectedSongs().map((s) => s.id);
    if (!ids.length) return;
    $("zip-ids").value = ids.join(",");
    $("zip-form").submit();
  });

  $("bulk-extend").addEventListener("click", () => extendSongs(selectedSongs(), $("bulk-lifetime").value));
  $("bulk-delete").addEventListener("click", () => deleteSongs(selectedSongs()));

  async function extendSongs(list, lifetime) {
    if (!list.length) return;
    if (!lifetime) {
      // single-row extend: ask for the lifetime
      const sel = el("select", { "aria-label": "Extend by" });
      lifetimeOptions(sel, config.default_lifetime);
      const extra = el("label", { class: "inline" }, "Keep for at least ", sel, " from now");
      const ok = await confirmDialog({
        title: "Extend expiry",
        text: list.length === 1 ? (list[0].title || "(untitled)") : `${list.length} songs`,
        okLabel: "Extend", extra,
      });
      if (!ok) return;
      lifetime = sel.value;
    }
    try {
      const res = await api("POST", "/api/songs/extend", { ids: list.map((s) => s.id), lifetime });
      if (res) syncClock(res.server_time);
      const lt = (config.lifetimes || []).find((l) => l.key === lifetime);
      setMsg($("lib-msg"), `Extended ${list.length} song${list.length === 1 ? "" : "s"} to at least ${lt ? lt.label : lifetime} from now (capped at the maximum lifetime).`, "ok");
      await loadSongs();
    } catch (e) {
      setMsg($("lib-msg"), e.message, "error");
    }
  }

  async function deleteSongs(list) {
    if (!list.length) return;
    const n = list.length;
    const ok = await confirmDialog({
      title: n === 1 ? "Delete this song?" : `Delete ${n} songs?`,
      text: "This removes the files from the shared library for everyone. It cannot be undone.",
      items: list.map((s) => (s.title || "(untitled)") + " [" + (s.format || s.ext || "") + "]"),
      okLabel: n === 1 ? "Delete" : `Delete ${n}`,
      danger: true,
    });
    if (!ok) return;
    try {
      if (n === 1) await api("DELETE", "/api/songs/" + encodeURIComponent(list[0].id));
      else await api("POST", "/api/songs/delete", { ids: list.map((s) => s.id) });
      list.forEach((s) => selected.delete(s.id));
      if (list.some((s) => String(s.id) === String(playingId))) stopPlayer();
      setMsg($("lib-msg"), `Deleted ${n} song${n === 1 ? "" : "s"}.`, "ok");
    } catch (e) {
      setMsg($("lib-msg"), e.message, "error");
    }
    await loadSongs();
  }

  // ---------- player ----------
  const audio = $("audio");
  function play(s) {
    playingId = s.id;
    $("player-title").textContent = s.title || "(untitled)";
    $("player-title").title = s.title || "";
    $("player").classList.remove("hidden");
    if (audio.getAttribute("src") !== s.file_url) audio.src = s.file_url;
    audio.play().catch(() => { /* autoplay may be blocked; controls are visible */ });
    renderSongs();
  }
  function stopPlayer() {
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    playingId = null;
    $("player").classList.add("hidden");
    renderSongs();
  }
  $("player-close").addEventListener("click", stopPlayer);

  // ---------- #song-<id> anchors ----------
  function focusHash() {
    const m = /^#song-(.+)$/.exec(pendingHash || "");
    if (!m) { pendingHash = null; return; }
    const id = decodeURIComponent(m[1]);
    const exists = songs.some((s) => String(s.id) === id);
    if (!exists) {
      setMsg($("lib-msg"), "That song is no longer in the library (it may have expired or been deleted).", "info");
      pendingHash = null;
      return;
    }
    let row = document.getElementById("song-" + id);
    if (!row && filterText) {
      filterText = "";
      $("search").value = "";
      renderSongs();
      row = document.getElementById("song-" + id);
    }
    pendingHash = null;
    if (!row) return;
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.remove("flash");
    void row.offsetWidth; // restart animation
    row.classList.add("flash");
  }
  window.addEventListener("hashchange", () => {
    pendingHash = location.hash;
    if (songs.length) focusHash(); else loadSongs();
  });

  // ---------- boot ----------
  async function boot() {
    try {
      await loadConfig();
    } catch (e) {
      setMsg($("add-msg"), "Could not load configuration: " + e.message, "error");
      config = { lifetimes: [], formats: [], limits: {} };
    }
    pendingHash = location.hash || null;
    await loadSongs();
    pollJobs();
    setInterval(loadSongs, 30000);
  }
  boot();
})();
