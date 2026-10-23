"use strict";

const $ = (s) => document.querySelector(s);
let api = null;
let state = { folders: [], job: null, model: {}, ocr: false };
let scope = null;          // path of the folder being searched, or null for all
let current = null;        // { result, hit } shown in the preview
let pollTimer = null;
let dismissedJob = "";     // the finished job whose banner the user closed
const RUNNING = ["loading", "scanning", "indexing"];
const isRunning = (job) => !!job && RUNNING.includes(job.phase);
const jobKey = (job) => job ? `${job.path}|${job.started || ""}|${job.phase}` : "";
let lastQuery = "";

const STOP_WORDS = new Set("a an and are as at be by for from has have in is it of on or that the this to was were will with what when where which who why how did does do i my me we our".split(" "));

// ---------- helpers ----------

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function bytes(n) {
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + " " + u[i];
}

function num(n) { return Number(n || 0).toLocaleString(); }

function duration(sec) {
  if (!isFinite(sec) || sec <= 0) return "";
  if (sec < 90) return Math.round(sec) + " s";
  if (sec < 5400) return Math.round(sec / 60) + " min";
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h + " h " + (m ? m + " min" : "");
}

function queryTerms(q) {
  return [...new Set((q.toLowerCase().match(/[\p{L}\p{N}']+/gu) || []).filter((w) => w.length > 2 && !STOP_WORDS.has(w)))];
}

function highlight(text, terms) {
  let html = esc(text);
  if (!terms.length) return html;
  const re = new RegExp("\\b(" + terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")\\w*", "gi");
  return html.replace(re, "<mark>$&</mark>");
}

function snippet(text, terms, len = 320) {
  const flat = text.replace(/\s+/g, " ").trim();
  if (flat.length <= len) return flat;
  const lower = flat.toLowerCase();
  let at = -1;
  for (const t of terms) { const i = lower.indexOf(t); if (i >= 0 && (at < 0 || i < at)) at = i; }
  let start = Math.max(0, (at < 0 ? 0 : at) - 80);
  if (start > 0) start = flat.indexOf(" ", start) + 1;
  const end = Math.min(flat.length, start + len);
  return (start > 0 ? "… " : "") + flat.slice(start, end) + (end < flat.length ? " …" : "");
}

function pageLabel(h) {
  if (!h.page_start) return "";
  return h.page_end && h.page_end !== h.page_start ? `pp ${h.page_start}–${h.page_end}` : `p ${h.page_start}`;
}

function show(el, on = true) { el.hidden = !on; }

// ---------- state and sidebar ----------

async function refresh() {
  state = await api.state();
  renderFolders();
  renderJob(state.job);
  startPolling();
}

function renderFolders() {
  const nav = $("#folders");
  const items = [];
  if (state.folders.length > 1) {
    items.push(`<div class="folder ${scope === null ? "sel" : ""}" data-path="">
      <div class="folder-name">All folders</div>
      <div class="folder-meta">${state.folders.length} folders</div></div>`);
  }
  for (const f of state.folders) {
    const s = f.stats || {};
    const bits = [];
    if (!f.exists) bits.push('<span class="error">folder missing</span>');
    else if (s.error) bits.push('<span class="error">index damaged</span>');
    else {
      const busy = isRunning(state.job) && state.job.path === f.path;
      bits.push(`${num(s.files)} files`);
      if (busy) bits.push(state.job.auto ? "updating…" : "indexing…");
      else if (f.paused) bits.push("paused");
      else if (s.last_indexed) bits.push(s.last_indexed.slice(0, 10));
      else bits.push("not finished");
    }
    const extra = [];
    if (s.skipped) extra.push(`<a data-act="skipped">${num(s.skipped)} cloud-only skipped</a>`);
    if (s.failures) extra.push(`<a data-act="failures">${num(s.failures)} not readable</a>`);
    items.push(`<div class="folder ${scope === f.path ? "sel" : ""}" data-path="${esc(f.path)}" title="${esc(f.path)}">
      <div class="folder-name">${esc(f.name)}</div>
      <div class="folder-meta">${bits.join(" · ")}</div>
      ${extra.length ? `<div class="folder-meta">${extra.join(" · ")}</div>` : ""}
      <button class="folder-menu-btn" data-act="menu" title="More">⋯</button>
    </div>`);
  }
  if (!state.folders.length) {
    items.push(`<div class="muted" style="padding:4px 8px 10px;font-size:12.5px">No folders yet. Add one to get started.</div>`);
  }
  nav.innerHTML = items.join("");
  if (scope !== null && !state.folders.some((f) => f.path === scope)) scope = null;
  if (scope === null && state.folders.length === 1) scope = state.folders[0].path;
  renderSubfolders();
  renderFoot();
}

function renderSubfolders() {
  const sel = $("#sub");
  const f = state.folders.find((x) => x.path === scope);
  const subs = f ? f.subfolders : [];
  const prev = sel.value;
  sel.innerHTML = `<option value="">All subfolders</option>` + subs.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("");
  if (subs.includes(prev)) sel.value = prev;
  show(sel, subs.length > 0);
}

$("#folders").addEventListener("click", async (ev) => {
  const row = ev.target.closest(".folder");
  if (!row) return;
  const path = row.dataset.path || null;
  const act = ev.target.dataset.act;
  if (act === "menu") { ev.stopPropagation(); openMenu(row, path); return; }
  if (act === "skipped") return showList(path, "skipped");
  if (act === "failures") return showList(path, "failures");
  scope = path;
  renderFolders();
  if (lastQuery) runSearch();
});

function closeMenus() { document.querySelectorAll(".menu").forEach((m) => m.remove()); }
document.addEventListener("click", closeMenus);

function openMenu(row, path) {
  closeMenus();
  const f = state.folders.find((x) => x.path === path);
  const busy = isRunning(state.job) && state.job.path === path;
  const userBusy = busy && !state.job.auto;
  const m = document.createElement("div");
  m.className = "menu";
  m.innerHTML = `
    <button data-m="reindex" ${userBusy ? "disabled" : ""}>${f.paused ? "Resume indexing" : "Update index"}</button>
    <button data-m="online">${f.include_online ? "Stop downloading cloud files" : "Include cloud-only files…"}</button>
    <button data-m="reveal">Show in Finder</button>
    <button data-m="remove" class="danger" ${busy ? "disabled" : ""}>Remove from Folder Search</button>`;
  m.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    const what = ev.target.dataset.m;
    closeMenus();
    if (what === "reindex") startIndex(path, f.include_online);
    else if (what === "online") {
      if (f.include_online) { await api.start_index(path, false); refresh(); }
      else addFolderFlow(path);
    } else if (what === "reveal") api.reveal(path);
    else if (what === "remove") {
      if (confirm(`Remove "${f.name}" from Folder Search?\n\nThis deletes the search index only. Your files are not touched.`)) {
        await api.remove_folder(path);
        if (scope === path) scope = null;
        refresh();
      }
    }
  });
  row.appendChild(m);
}

async function showList(path, kind) {
  const f = state.folders.find((x) => x.path === path);
  const rows = kind === "failures" ? await api.failures(path) : await api.skipped(path);
  $("#list-title").textContent = kind === "failures"
    ? `Files in "${f.name}" that could not be read (${num(rows.length)})`
    : `Cloud-only files left out of "${f.name}" (${num(rows.length)})`;
  const shown = rows.slice(0, 2000);
  $("#list-body").innerHTML = shown.map((r) =>
    `<div class="list-row">${esc(r.path)}<div class="muted">${kind === "failures" ? esc(r.reason) : bytes(r.size)}</div></div>`
  ).join("") + (rows.length > shown.length ? `<div class="list-row muted">…and ${num(rows.length - shown.length)} more</div>` : "");
  show($("#list-screen"));
}
$("#list-close").onclick = () => show($("#list-screen"), false);

// ---------- model ----------

async function checkModel() {
  const m = await api.model_status();
  if (m.phase === "ready") { api.warm_up(); api.start_background(); return true; }
  show($("#model-screen"));
  if (m.phase === "downloading" || m.phase === "loading") pollModel();
  return false;
}

$("#model-download").onclick = async () => {
  $("#model-download").disabled = true;
  await api.download_model();
  pollModel();
};

async function pollModel() {
  show($("#model-progress"));
  const m = await api.model_status();
  const pct = m.total ? (100 * m.done) / m.total : 0;
  $("#model-bar").style.width = pct.toFixed(1) + "%";
  $("#model-text").textContent = m.phase === "loading" ? "Loading the model…"
    : m.total ? `${bytes(m.done)} of ${bytes(m.total)}` : "Starting download…";
  if (m.phase === "ready") { show($("#model-screen"), false); return; }
  if (m.phase === "error") {
    $("#model-error").textContent = "Download failed: " + m.error + ". Check the internet connection and try again.";
    show($("#model-error"));
    $("#model-download").disabled = false;
    return;
  }
  setTimeout(pollModel, 500);
}

// ---------- adding a folder ----------

$("#add-folder").onclick = async () => {
  const path = await api.choose_folder();
  if (path) addFolderFlow(path);
};

async function addFolderFlow(path) {
  $("#scan-title").textContent = "Checking folder…";
  $("#scan-body").innerHTML = `<p class="muted">${esc(path)}</p><div class="spinner"></div>`;
  $("#scan-actions").innerHTML = "";
  show($("#scan-screen"));
  const r = await api.scan_folder(path);
  $("#scan-title").textContent = r.name;

  if (!r.total_files) {
    $("#scan-body").innerHTML = `<p>No documents Folder Search can read were found here.</p>
      <p class="muted">It reads PDF, Word (.docx, .doc), RTF, text, Markdown and email (.eml, .msg) files.</p>`;
    $("#scan-actions").innerHTML = `<button class="btn" id="scan-cancel">Close</button>`;
    $("#scan-cancel").onclick = () => show($("#scan-screen"), false);
    return;
  }

  const types = Object.entries(r.by_type).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${num(v)} ${k.slice(1)}`).join(", ");
  let html = `<div class="stats">
      <span>Documents on this Mac</span><b>${num(r.local_files)}</b>
      ${r.online_files ? `<span>Cloud-only (not downloaded)</span><b>${num(r.online_files)}</b>` : ""}
    </div>
    <p class="muted" style="font-size:12.5px">${types}</p>`;

  let choice = "local";
  if (r.online_files) {
    html += `<p>${num(r.online_files)} files are stored only in the cloud (for example Dropbox "online-only" files). Indexing them means downloading <strong>${bytes(r.online_bytes)}</strong>.</p>
      <button class="choice sel" data-c="local"><strong>Index the ${num(r.local_files)} files on this Mac</strong><span>Nothing is downloaded. Cloud-only files are listed so you can add them later.</span></button>
      <button class="choice" data-c="all"><strong>Download and index everything</strong><span>Downloads ${bytes(r.online_bytes)}. Needs that much free disk space, and takes longer.</span></button>`;
  }
  const est = estimate(r.local_files);
  if (est) html += `<p class="muted" style="font-size:12.5px;margin-top:10px">${est} You can keep using the app, stop at any time, and carry on later.</p>`;
  $("#scan-body").innerHTML = html;
  $("#scan-body").querySelectorAll(".choice").forEach((b) => (b.onclick = () => {
    choice = b.dataset.c;
    $("#scan-body").querySelectorAll(".choice").forEach((x) => x.classList.toggle("sel", x === b));
  }));
  $("#scan-actions").innerHTML = `<button class="btn" id="scan-cancel">Cancel</button><button class="btn btn-primary" id="scan-go">Start indexing</button>`;
  $("#scan-cancel").onclick = () => show($("#scan-screen"), false);
  $("#scan-go").onclick = () => {
    show($("#scan-screen"), false);
    startIndex(path, choice === "all");
    scope = r.already ? scope : path;
  };
}

function estimate(files) {
  // Measured on an M-series Mac: ~30 passages/s, ~30 passages per document.
  const sec = (files * 30) / 30;
  if (sec < 120) return "";
  return `First indexing will take roughly ${duration(sec)}.`;
}

// ---------- indexing ----------

async function startIndex(path, includeOnline) {
  const res = await api.start_index(path, includeOnline);
  if (!res.ok) { alert(res.error); return; }
  await refresh();
}

// One heartbeat for the whole app: quick while something is indexing, slow
// otherwise, so automatic updates started by the folder watcher show up too.
function startPolling() {
  if (pollTimer) return;
  let ticks = 0;
  let wasRunning = isRunning(state.job);
  const beat = async () => {
    let job = null;
    try { job = await api.progress(); } catch (e) { /* window closing */ }
    const running = isRunning(job);
    state.job = job;
    renderJob(job);
    if (running && ++ticks % 8 === 0) { state = await api.state(); renderFolders(); }
    if (running !== wasRunning) { await refresh(); }
    wasRunning = running;
    pollTimer = setTimeout(beat, running ? 700 : 2500);
  };
  pollTimer = setTimeout(beat, 300);
}

function renderFoot() {
  const lines = [];
  const job = state.job;
  if (isRunning(job) && job.auto) {
    lines.push(job.phase === "indexing" && job.total
      ? `Updating ${esc(job.name)}: ${num(job.done)} of ${num(job.total)}`
      : `Checking ${esc(job.name)} for changes…`);
  } else if (state.auto_update) {
    lines.push("Folders update automatically when files change.");
  }
  lines.push(state.ocr ? "Scanned PDFs: OCR on" : "Scanned PDFs are skipped (install ocrmypdf to read them)");
  const bg = state.background || {};
  let html = "";
  if (bg.lock_problem) html += `<div class="foot-warn">${esc(bg.lock_problem)}</div>`;
  if (bg.enabled && bg.needs_access && bg.needs_access.length) {
    html += `<div class="foot-warn">
      <strong>Updates while closed can't read ${bg.needs_access.length === 1 ? "a folder" : bg.needs_access.length + " folders"}.</strong>
      macOS needs to give the background updater Full Disk Access.
      <div class="foot-actions">
        <button class="btn btn-small" id="fda-open">Open Privacy settings</button>
        <button class="btn btn-small" id="fda-show">Show the program to add</button>
      </div>
      <div>In Full Disk Access, click +, then drag in the file shown in Finder.</div>
    </div>`;
  }
  html += `<label class="foot-toggle"><input type="checkbox" id="bg-toggle" ${bg.enabled ? "checked" : ""}>
    Update when the app is closed</label>`;
  if (bg.enabled && bg.last_run) html += `<div class="foot-sub">Last background update: ${esc(bg.last_run.slice(0, 16))}</div>`;
  html += lines.map((l) => `<div>${l}</div>`).join("");
  const foot = $("#sidebar-foot");
  if (foot.dataset.html === html) return;  // unchanged: keep clicks and focus intact
  foot.dataset.html = html;
  foot.innerHTML = html;
  $("#bg-toggle").onchange = async (ev) => { state.background = await api.set_background(ev.target.checked); renderFoot(); };
  const o = $("#fda-open"); if (o) o.onclick = () => api.open_privacy_settings();
  const r = $("#fda-show"); if (r) r.onclick = () => api.reveal_python();
}

function renderJob(job) {
  const banner = $("#job-banner");
  renderFoot();
  // Automatic updates run quietly (status in the sidebar); the banner is for
  // jobs the user started.
  if (!job || job.auto || jobKey(job) === dismissedJob) { show(banner, false); return; }
  show(banner);
  const running = ["loading", "scanning", "indexing"].includes(job.phase);
  show($("#job-stop"), running);
  show($("#job-dismiss"), !running);
  let title = "", detail = "", pct = 0;
  if (job.phase === "loading") { title = `Preparing to index ${job.name}…`; }
  else if (job.phase === "scanning") { title = `Looking through ${job.name}…`; }
  else if (job.phase === "indexing") {
    pct = job.total_bytes ? (100 * job.done_bytes) / job.total_bytes : (job.total ? (100 * job.done) / job.total : 0);
    const rate = job.done_bytes / Math.max(job.elapsed, 1);
    const left = rate > 0 && job.done > 3 ? (job.total_bytes - job.done_bytes) / rate : 0;
    title = `Indexing ${job.name}: ${num(job.done)} of ${num(job.total)}`;
    detail = (left ? `about ${duration(left)} left · ` : "") + (job.current || "");
  } else if (job.phase === "done") {
    pct = 100;
    title = `${job.name} is up to date`;
    const parts = [];
    if (job.added) parts.push(`${num(job.added)} added`);
    if (job.updated) parts.push(`${num(job.updated)} updated`);
    if (job.removed) parts.push(`${num(job.removed)} removed`);
    if (job.failed) parts.push(`${num(job.failed)} could not be read`);
    if (job.skipped_online) parts.push(`${num(job.skipped_online)} cloud-only skipped`);
    detail = parts.join(" · ") || "no changes";
  } else if (job.phase === "stopped") {
    pct = job.total ? (100 * job.done) / job.total : 0;
    title = `Stopped indexing ${job.name}`;
    detail = `${num(job.done)} of ${num(job.total)} done. Paused: choose "Resume indexing" from the ⋯ menu to carry on.`;
  } else if (job.phase === "error") {
    title = `Indexing ${job.name} failed`;
    detail = job.error || "";
  }
  $("#job-title").textContent = title;
  $("#job-detail").textContent = detail;
  $("#job-bar").style.width = pct.toFixed(1) + "%";
}

$("#job-stop").onclick = () => { api.stop_index(); $("#job-title").textContent = "Stopping after this file…"; };
$("#job-dismiss").onclick = () => { dismissedJob = jobKey(state.job); show($("#job-banner"), false); };

// ---------- search ----------

$("#search-form").addEventListener("submit", (ev) => { ev.preventDefault(); runSearch(); });
$("#sub").addEventListener("change", () => { if (lastQuery) runSearch(); });

async function runSearch() {
  const q = $("#q").value.trim();
  lastQuery = q;
  if (!q) return;
  if (!state.folders.length) {
    $("#results").innerHTML = `<div class="empty"><h3>Nothing to search yet</h3>Add a folder to index it first.</div>`;
    return;
  }
  $("#search-meta").textContent = "Searching…";
  const sub = $("#sub").hidden ? null : $("#sub").value || null;
  const res = await api.search(q, scope, sub, 30);
  if (q !== lastQuery) return;
  renderResults(res, q);
}

function renderResults(res, q) {
  const terms = queryTerms(q);
  const n = res.results.length;
  $("#search-meta").textContent = n ? `${n} document${n === 1 ? "" : "s"} · ${res.ms} ms` : "";
  if (!n) {
    $("#results").innerHTML = `<div class="empty"><h3>No matches</h3>Try describing it differently, or search all folders.</div>`;
    return;
  }
  const multi = scope === null && state.folders.length > 1;
  $("#results").innerHTML = res.results.map((r, i) => {
    const dir = r.path.includes("/") ? r.path.slice(0, r.path.lastIndexOf("/")) : "";
    const where = [multi ? r.root_name : "", dir].filter(Boolean).join(" › ");
    return `<div class="result" data-i="${i}">
      <div class="result-name">${esc(r.filename)}</div>
      ${where ? `<div class="result-where">${esc(where)}</div>` : ""}
      ${r.hits.map((h, j) => `<button class="passage" data-i="${i}" data-j="${j}">
          ${pageLabel(h) ? `<span class="pg">${pageLabel(h)}</span>` : ""}${highlight(snippet(h.text, terms), terms)}
        </button>`).join("")}
    </div>`;
  }).join("");
  $("#results").scrollTop = 0;
  $("#results").querySelectorAll(".passage").forEach((b) => (b.onclick = () => {
    $("#results").querySelectorAll(".passage.sel").forEach((x) => x.classList.remove("sel"));
    b.classList.add("sel");
    const r = res.results[+b.dataset.i];
    openPreview(r, r.hits[+b.dataset.j], terms);
  }));
  const first = $("#results .passage");
  if (first) first.click();
}

// ---------- preview ----------

async function openPreview(result, hit, terms, page) {
  current = { result, hit, terms };
  show($("#preview-head"));
  $("#preview-title").textContent = result.filename;
  $("#preview-title").title = result.abs;
  const body = $("#preview-body");
  const pg = page || hit.page_start || 1;
  const p = await api.preview(result.abs, pg, page && page !== hit.page_start ? "" : hit.text);
  if (current.hit !== hit) return;
  if (p.kind === "pdf") {
    body.innerHTML = `<div class="page-nav">
        <button class="btn btn-small" id="pg-prev" ${p.page <= 1 ? "disabled" : ""}>‹</button>
        <span>Page ${p.page} of ${p.pages}</span>
        <button class="btn btn-small" id="pg-next" ${p.page >= p.pages ? "disabled" : ""}>›</button>
      </div>
      <div class="page-wrap"><img src="${p.image}" alt="Page ${p.page}">
        ${p.boxes.map((b) => `<div class="hl" style="left:${b[0] * 100}%;top:${b[1] * 100}%;width:${b[2] * 100}%;height:${b[3] * 100}%"></div>`).join("")}
      </div>`;
    $("#pg-prev").onclick = () => openPreview(result, hit, terms, p.page - 1);
    $("#pg-next").onclick = () => openPreview(result, hit, terms, p.page + 1);
    const img = body.querySelector("img");
    img.onload = () => {
      if (p.boxes.length) body.scrollTop = Math.max(0, img.offsetTop + img.clientHeight * p.scroll_to - body.clientHeight / 3);
      else body.scrollTop = 0;
    };
  } else if (p.kind === "text") {
    body.innerHTML = `<div class="text-preview">${highlight(p.text, terms)}</div>`;
    body.scrollTop = 0;
  } else {
    body.innerHTML = `<div class="empty">${esc(p.error)}</div>`;
  }
}

$("#open-file").onclick = () => current && api.open_file(current.result.abs);
$("#reveal-file").onclick = () => current && api.reveal(current.result.abs);

// ---------- start ----------

window.addEventListener("pywebviewready", async () => {
  api = window.pywebview.api;
  await refresh();
  await checkModel();
  $("#q").focus();
});
