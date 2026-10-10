"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const state = { database: null, schema: null, result: null, reading: null, sort: null };

// ---------------------------------------------------------------- helpers
async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body && !(options.body instanceof Blob) ? { "Content-Type": "application/json" } : {},
    ...options,
  });
  let data = null;
  try { data = await response.json(); } catch { /* empty body */ }
  if (!response.ok) {
    const detail = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : response.statusText;
    throw new Error(detail);
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key));
    localStorage.setItem(key, JSON.stringify(value));
  } catch { return null; }
  return value;
}

let toastTimer = null;
function toast(text) {
  const box = $("#toast");
  box.textContent = text; box.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { box.hidden = true; }, 1800);
}

async function copy(text, label) {
  try { await navigator.clipboard.writeText(text); toast(`${label} copied`); }
  catch { toast("Copying is not available here"); }
}

function plural(n, word) { return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`; }

function formatValue(value) {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "number") {
    if (Number.isInteger(value)) return value.toLocaleString();
    return value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }
  return String(value);
}

// ---------------------------------------------------------------- theme & preferences
function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme; else delete document.documentElement.dataset.theme;
}
applyTheme(store("intentsql.theme"));
const developer = () => Boolean(store("intentsql.developer"));

// ---------------------------------------------------------------- connection & settings
async function refreshConnection() {
  try {
    const conn = await api("/api/connection");
    state.connection = conn;
    const ready = conn.has_key || conn.provider === "mock";
    $("#conn-dot").className = "dot " + (ready ? "ok" : "bad");
    $("#open-settings").title = `Settings · ${conn.label}`;
    const picker = $("#conn-provider");
    picker.replaceChildren(el("option", { value: "auto", text: "Automatic (first provider with a key)" }),
      ...conn.providers.map((p) => el("option", { value: p.name, text: `${p.label}${p.has_key ? "" : " · no key"}` })));
    picker.value = conn.requested === "auto" ? "auto" : conn.provider;
    $("#conn-url").value = conn.url; $("#conn-model").value = conn.model;
    $("#conn-advanced").hidden = conn.provider !== "typesafe";
    $("#conn-key").closest("label").hidden = conn.provider === "mock";
    $("#conn-provider-note").textContent = `Active: ${conn.label} · model ${conn.model}`;
    $("#conn-status").textContent = ready ? "Ready." : "No API key saved for this provider yet.";
    $("#welcome").hidden = ready;
    return conn;
  } catch {
    $("#conn-dot").className = "dot bad";
    return null;
  }
}

function openSettings() {
  refreshConnection();
  $("#theme-select").value = store("intentsql.theme") || "";
  $("#developer").checked = developer();
  $("#settings-dialog").showModal();
  $("#conn-key").focus();
}
$("#open-settings").addEventListener("click", openSettings);
$("#welcome-settings").addEventListener("click", openSettings);
$("#theme-select").addEventListener("change", (e) => { store("intentsql.theme", e.target.value || null); applyTheme(e.target.value); });
$("#developer").addEventListener("change", (e) => { store("intentsql.developer", e.target.checked); if (state.result) renderHow(); });
$("#conn-provider").addEventListener("change", () => {
  $("#conn-advanced").hidden = $("#conn-provider").value !== "typesafe";
  $("#conn-key").closest("label").hidden = $("#conn-provider").value === "mock";
  $("#conn-key").value = "";
});
$("#conn-save").addEventListener("click", async (event) => {
  event.preventDefault();
  try {
    const provider = $("#conn-provider").value;
    const typesafe = provider === "typesafe";
    await api("/api/connection", { method: "POST", body: JSON.stringify({
      provider, api_key: $("#conn-key").value || null,
      url: typesafe ? $("#conn-url").value || null : null, model: typesafe ? $("#conn-model").value || null : null }) });
    $("#conn-key").value = "";
    await refreshConnection();
    $("#settings-dialog").close();
    toast("Settings saved");
  } catch (err) { $("#conn-status").textContent = err.message; }
});

// ---------------------------------------------------------------- sidebar
function selectTab(name) {
  for (const tab of ["schema", "history"]) {
    $(`#tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    $(`#panel-${tab}`).hidden = tab !== name;
  }
}
$("#tab-schema").addEventListener("click", () => selectTab("schema"));
$("#tab-history").addEventListener("click", () => selectTab("history"));
$("#toggle-sidebar").addEventListener("click", () => {
  const open = !$("#sidebar").classList.contains("open");
  $("#sidebar").classList.toggle("open", open);
  $("#toggle-sidebar").setAttribute("aria-expanded", String(open));
});

// ---------------------------------------------------------------- databases & schema
async function loadDatabases(select) {
  const list = await api("/api/databases");
  const picker = $("#database");
  picker.replaceChildren(...list.map((d) => el("option", { value: d.name, text: d.bundled ? `${d.name} · example` : d.name })));
  const wanted = select || store("intentsql.database");
  if (list.some((d) => d.name === wanted)) picker.value = wanted;
  state.databases = list;
  await selectDatabase(picker.value);
}

async function selectDatabase(name) {
  if (!name) return;
  state.database = name; store("intentsql.database", name);
  $("#db-name").textContent = name;
  const entry = (state.databases || []).find((d) => d.name === name);
  $("#delete-db").hidden = !entry || entry.bundled;
  renderHistory();
  $("#schema-list").replaceChildren(el("p", { class: "muted", text: "Loading…" }));
  $("#examples").replaceChildren();
  try {
    state.schema = await api(`/api/databases/${encodeURIComponent(name)}/schema`);
    renderSchema();
  } catch (err) {
    $("#schema-list").replaceChildren(el("p", { class: "error", text: err.message }));
  }
  api(`/api/databases/${encodeURIComponent(name)}/examples`).then(renderExamples).catch(() => {});
}
$("#database").addEventListener("change", (event) => selectDatabase(event.target.value));

function renderSchema() {
  const filter = $("#schema-filter").value.trim().toLowerCase();
  const refs = new Map();
  for (const fk of state.schema.foreign_keys || []) {
    fk.columns.forEach((c, i) => refs.set(`${fk.table}.${c}`, `${fk.ref_table}.${fk.ref_columns[i]}`));
  }
  const tables = state.schema.tables.filter((t) => !filter || t.name.toLowerCase().includes(filter)
    || t.columns.some((c) => c.name.toLowerCase().includes(filter)));
  const rows = state.schema.tables.reduce((n, t) => n + t.rows, 0);
  $("#schema-summary").textContent = `${plural(state.schema.tables.length, "table")} · ${plural(rows, "row")}`;
  $("#schema-list").replaceChildren(...tables.map((t) => el("details", { open: Boolean(filter) || tables.length <= 2 },
    el("summary", {}, el("span", { text: t.name }), el("span", { class: "rows", text: t.rows.toLocaleString() })),
    el("ul", {}, t.columns.map((c) => {
      const target = refs.get(`${t.name}.${c.name}`);
      return el("li", {},
        el("span", {}, c.name,
          c.primary_key ? el("span", { class: "tag", text: "KEY", title: "Primary key" }) : null,
          target ? el("span", { class: "tag", text: "→", title: `References ${target}` }) : null),
        el("span", { class: "kind", text: c.kind }));
    })),
    el("button", { class: "button ghost small preview", type: "button", text: "Preview rows",
      onclick: () => openConsole(`SELECT * FROM "${t.name.replaceAll('"', '""')}" LIMIT 50`, true) }),
  )));
  if (!tables.length) $("#schema-list").append(el("p", { class: "muted small", text: "Nothing matches." }));
}
$("#schema-filter").addEventListener("input", () => state.schema && renderSchema());

function renderExamples(examples) {
  question.placeholder = examples && examples.length ? `e.g. ${examples[examples.length > 2 ? 2 : 0]}` : "Ask a question about this database";
  $("#examples").replaceChildren(...(examples || []).map((q) => el("button", { type: "button", text: q,
    onclick: () => { $("#question").value = q; autosize(); ask(); } })));
}

// ---------------------------------------------------------------- history
const historyKey = () => `intentsql.history.${state.database}`;
function remember(question, status) {
  const items = loadHistory().filter((h) => h.q !== question);
  items.unshift({ q: question, status, t: Date.now() });
  store(historyKey(), items.slice(0, 30));
  renderHistory();
}
function loadHistory() {
  // Entries are {q, status, t}; earlier versions stored plain strings.
  return (store(historyKey()) || []).map((h) => (typeof h === "string" ? { q: h, status: "" } : h))
    .filter((h) => h && typeof h.q === "string" && h.q.trim());
}
function renderHistory() {
  const items = loadHistory();
  $("#history-empty").hidden = items.length > 0;
  $("#clear-history").hidden = !items.length;
  $("#history-list").replaceChildren(...items.map((h) => el("li", {}, el("button", {
    type: "button", title: h.q,
    onclick: () => { $("#question").value = h.q; autosize(); ask(); $("#sidebar").classList.remove("open"); },
  }, el("span", { class: `status-dot ${h.status}`, "aria-hidden": "true" }), el("span", { text: h.q })))));
}
$("#clear-history").addEventListener("click", () => { store(historyKey(), []); renderHistory(); });

// ---------------------------------------------------------------- asking
const question = $("#question");
function autosize() { question.style.height = "auto"; question.style.height = `${Math.min(question.scrollHeight + 2, 240)}px`; }
question.addEventListener("input", autosize);
question.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); ask(); }
});
$("#ask-form").addEventListener("submit", (event) => { event.preventDefault(); ask(); });
document.addEventListener("keydown", (event) => {
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  if (event.key === "/" && !typing && !document.querySelector("dialog[open]")) { event.preventDefault(); question.focus(); }
});

const PHASES = ["ground", "interpret", "compile", "verify", "execute"];
function setProgress(done, timings = {}) {
  const box = $("#progress");
  box.hidden = false;
  $$("#progress li").forEach((li) => {
    const phase = li.dataset.phase;
    const index = PHASES.indexOf(phase);
    const finished = done.includes(phase);
    const nextIndex = Math.max(-1, ...done.map((p) => PHASES.indexOf(p))) + 1;
    li.className = finished ? "done" : index === nextIndex ? "active" : "";
    li.querySelector(".ms")?.remove();
    if (finished && timings[phase] !== undefined) li.append(el("span", { class: "ms", text: `${timings[phase]} ms` }));
  });
}

async function streamAnswer(body, onPhase) {
  const response = await fetch("/api/ask/stream", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = response.statusText;
    try { detail = (await response.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(detail);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut); buffer = buffer.slice(cut + 2);
      const kind = (block.match(/^event: (.*)$/m) || [])[1];
      const data = JSON.parse((block.match(/^data: (.*)$/m) || [])[1] || "null");
      if (kind === "phase") onPhase(data);
      else if (kind === "result") return data;
      else if (kind === "error") throw new Error(data.message);
    }
  }
  throw new Error("The connection closed before an answer arrived.");
}

async function ask() {
  const text = question.value.trim();
  if (!text || !state.database) return;
  const button = $("#ask");
  button.disabled = true; button.textContent = "Asking…";
  const done = []; const timings = {};
  setProgress(done);
  try {
    const result = await streamAnswer({ database: state.database, question: text }, (event) => {
      done.push(event.phase); timings[event.phase] = event.ms; setProgress(done, timings);
    });
    remember(text, result.status);
    showAnswer(result);
  } catch (err) {
    showAnswer({ status: "error", question: text, message: err.message, rows: [], columns: [] });
  } finally {
    button.disabled = false; button.textContent = "Ask";
    $("#progress").hidden = true;
  }
}

// ---------------------------------------------------------------- answer
const CONFIDENCE = {
  answered: (c, v) => (c !== null && c !== undefined ? (c >= 0.8 ? ["ok", "Confident"] : ["warn", "Likely right"])
    : v === null || v === undefined || v >= 0.6 ? ["ok", "Checked"] : ["warn", "Likely right"]),
  ambiguous: () => ["bad", "Uncertain"],
  refused: () => ["bad", "Not answered"],
  error: () => ["bad", "Error"],
};

function readings(result) {
  const main = { explanation: result.explanation, sql: result.sql, display_sql: result.display_sql,
                 params: result.params, columns: result.columns, rows: result.rows, truncated: result.truncated,
                 main: true };
  return [main, ...(result.alternatives || [])];
}

function showAnswer(result) {
  state.result = result; state.sort = null;
  state.readings = readings(result);
  state.reading = state.readings[0];
  $("#answer").hidden = false;
  $("#answer-title").textContent = result.question || question.value.trim();
  let [tone, label] = (CONFIDENCE[result.status] || CONFIDENCE.error)(result.confidence, result.verification);
  if (result.provider === "mock" && result.sql) [tone, label] = ["warn", "Mock answer"];
  const pill = $("#confidence");
  pill.className = `pill ${tone}`; pill.textContent = label;
  pill.title = result.confidence !== null && result.confidence !== undefined
    ? `In testing, about ${Math.round(result.confidence * 100)}% of answers with this level of confidence were correct`
    : result.verification !== null && result.verification !== undefined
      ? `Verification score ${Math.round(result.verification * 100)}% (confidence is not calibrated for this provider yet)` : "";
  renderReading();
  renderAlternatives();
  const notice = $("#notice");
  let message = result.message || "";
  if (result.provider === "mock") message = ["Offline mock provider: decisions are fixed defaults, not a reading of your question.", message].filter(Boolean).join(" ");
  if (result.status === "refused" && !message) message = "IntentSQL could not answer this question.";
  notice.hidden = !message;
  notice.className = "notice" + (result.status === "ambiguous" ? " warn" : ["refused", "error"].includes(result.status) ? " bad" : "");
  notice.textContent = message;
  renderHow();
  $("#answer").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderReading() {
  const result = state.result;
  const reading = state.reading;
  const answered = Boolean(reading && reading.sql);
  $("#explanation").textContent = (reading && reading.explanation) || (answered ? "" : "—");
  $("#result-section").hidden = !answered;
  $("#sql-section").hidden = !answered;
  if (!answered) return;
  $("#sql code").replaceChildren(...highlight(reading.display_sql || reading.sql));
  renderResult(reading);
  if (result) $("#explanation").textContent = reading.explanation;
}

function renderResult(reading) {
  const box = $("#result");
  const columns = reading.columns || [];
  let rows = reading.rows || [];
  box.className = "result";
  box.replaceChildren();
  const n = rows.length;
  $("#row-count").textContent = `· ${plural(n, "row")}${reading.truncated ? " (first rows only)" : ""}`;
  if (columns.length === 1 && n === 1) {
    box.className = "result single";
    box.append(el("div", { class: "big-value", text: formatValue(rows[0][0]) }), el("div", { class: "big-label", text: columns[0] }));
    return;
  }
  if (state.sort) {
    const { index, desc } = state.sort;
    rows = [...rows].sort((a, b) => {
      const x = a[index]; const y = b[index];
      if (x === y) return 0;
      if (x === null) return 1;
      if (y === null) return -1;
      return (x < y ? -1 : 1) * (desc ? -1 : 1);
    });
  }
  const numeric = columns.map((_, i) => rows.length > 0 && rows.every((r) => r[i] === null || typeof r[i] === "number"));
  const table = el("table");
  table.append(el("thead", {}, el("tr", {}, columns.map((c, i) => {
    const sorted = state.sort && state.sort.index === i;
    return el("th", { scope: "col", class: numeric[i] ? "num" : "", "aria-sort": sorted ? (state.sort.desc ? "descending" : "ascending") : "none" },
      el("button", { type: "button", title: "Sort", onclick: () => {
        state.sort = { index: i, desc: sorted ? !state.sort.desc : false }; renderResult(state.reading);
      } }, c, sorted ? (state.sort.desc ? " ↓" : " ↑") : ""));
  }))));
  const body = el("tbody");
  for (const row of rows.slice(0, 1000)) {
    body.append(el("tr", {}, row.map((v, i) => el("td", {
      class: v === null ? "null" : numeric[i] ? "num" : "", title: v !== null && String(v).length > 40 ? String(v) : null,
      text: formatValue(v) }))));
  }
  table.append(body);
  box.append(table);
  if (!n) box.append(el("p", { class: "empty", text: "No rows." }));
}

function renderAlternatives() {
  const options = state.readings || [];
  const box = $("#alternatives");
  box.hidden = options.length < 2;
  if (box.hidden) return;
  $("#alternative-list").replaceChildren(...options.map((r) => el("li", { class: r === state.reading ? "current" : "" },
    el("span", { text: r.explanation || "—" }),
    r === state.reading
      ? el("span", { class: "muted small", text: "shown" })
      : el("button", { class: "button ghost small", type: "button", text: "Show this", onclick: () => switchReading(r) }))));
  if (options.length >= 2) $("#alternatives .eyebrow").textContent = "Possible readings of the question";
}

async function switchReading(reading) {
  state.reading = reading; state.sort = null;
  renderReading(); renderAlternatives();
  if (reading.main || !reading.sql) return;
  try {
    // Alternatives arrive with a preview; fetch every row for the chosen reading.
    const full = await api("/api/sql", { method: "POST", body: JSON.stringify({ database: state.database, sql: reading.sql, params: reading.params || [] }) });
    reading.rows = full.rows; reading.columns = full.columns; reading.truncated = full.truncated;
    if (state.reading === reading) renderResult(reading);
  } catch (err) { toast(err.message); }
}

// ---------------------------------------------------------------- SQL highlighting
const KEYWORDS = new Set(("select distinct from where join left inner on and or not in is null like between group by having order asc desc limit offset as " +
  "union all intersect except case when then else end exists count sum avg min max cast real integer over partition row_number rank dense_rank strftime julianday").split(" "));
function highlight(sql) {
  const out = [];
  const pattern = /('(?:[^']|'')*')|("(?:[^"]|"")*")|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|(\s+|.)/g;
  let match;
  while ((match = pattern.exec(sql || "")) !== null) {
    const [token, str, quoted, num, word] = match;
    if (str) out.push(el("span", { class: "str", text: token }));
    else if (num) out.push(el("span", { class: "num", text: token }));
    else if (word && KEYWORDS.has(word.toLowerCase())) out.push(el("span", { class: "kw", text: token }));
    else out.push(document.createTextNode(quoted || token));
  }
  return out;
}

// ---------------------------------------------------------------- how it was answered
const PURPOSE = {
  "request scope": "Kind of request", output: "Shown", "literal role": "Value role", "literal column": "Value matched to",
  "literal operator": "Comparison", "literal count": "Counted", "implicit value": "Implied filter", grouping: "Grouping",
  ordering: "Ordering", limit: "Limit", relationship: "Relationship", "relationship role": "Relationship",
  "aggregate comparison": "Comparison", "missing values": "Missing values", verification: "Check",
  exclusion: "Exclusion", "universal condition": "Every record", percentage: "Percentage", "join route": "Route",
  "column comparison": "Comparison", "output conflict": "Count or total",
};
function renderHow() {
  const result = state.result;
  const body = $("#how-body");
  body.replaceChildren();
  if (!result) return;
  const t = result.timings_ms || {}; const u = result.usage || {};
  const providerLabel = (state.connection && state.connection.providers || []).find((p) => p.name === u.provider)?.label || u.provider;
  const cost = u.cost_usd ? ` · $${Number(u.cost_usd).toFixed(5)}` : "";
  const items = [["Total time", t.total !== undefined ? `${t.total} ms` : "—"],
    ["Jev", u.calls ? `${plural(u.calls, "call")} · ${(u.input_tokens || 0).toLocaleString()} input tokens${cost}` : "—"],
    ["Provider", providerLabel ? `${providerLabel}${(u.models || []).length ? ` (${u.models.join(", ")})` : ""}` : "—"]];
  if (result.confidence !== null && result.confidence !== undefined) {
    items.push(["Confidence", `about ${Math.round(result.confidence * 100)}% of answers like this were correct in testing`]);
  } else if (result.verification !== null && result.verification !== undefined) {
    items.push(["Check", `${Math.round(result.verification * 100)}% that the reading matches the question (not calibrated for this provider)`]);
  }
  const dl = el("dl", { class: "steps" });
  for (const [k, v] of items) dl.append(el("dt", { text: k }), el("dd", { text: v }));
  body.append(dl);
  const decisions = (result.decisions || []).filter((d) => (d.purpose !== "verification" || d.key === "verify")
    && !(d.key === "scope" && d.selected === "read") && d.selected !== null && d.selected !== undefined
    && (developer() || d.purpose !== "literal role"));
  if (decisions.length) {
    body.append(el("p", { class: "eyebrow", text: "Decisions" }));
    body.append(el("ul", { class: "decisions" }, decisions.map((d) => {
      const p = Math.round(Number(d.probability) * 100);
      const selected = typeof d.selected === "object" && d.selected !== null ? JSON.stringify(d.selected) : String(d.selected);
      return el("li", {},
        el("span", { class: "what", text: PURPOSE[d.purpose] || d.purpose }),
        el("span", {}, d.purpose === "output" ? `${d.question}${selected === "value" ? "" : ` (${selected.toLowerCase()})`}` : d.question,
          el("div", { class: "meter" }, el("span", { style: `width:${p}%` }))),
        el("span", { class: "p", text: `${p}%` }));
    })));
  }
  if (developer()) {
    const tech = (title, value) => el("details", { class: "tech" }, el("summary", { text: title }), el("pre", { text: JSON.stringify(value, null, 2) }));
    body.append(tech("Semantic plan", result.intent), tech("SQL plan (IR)", result.plan),
      tech("Parameterized SQL", { sql: result.sql, params: result.params }),
      tech(`Jev calls (${(result.exchanges || []).length})`, result.exchanges),
      tech("Timings", result.timings_ms));
  }
}

// ---------------------------------------------------------------- copy & export
function rowsAsText(sep) {
  const reading = state.reading;
  if (!reading) return "";
  const cell = (v) => {
    const text = v === null ? "" : String(v);
    return sep === "," && /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text.replaceAll("\t", " ").replaceAll("\n", " ");
  };
  return [reading.columns.map(cell).join(sep), ...reading.rows.map((r) => r.map(cell).join(sep))].join("\n");
}
$("#copy-result").addEventListener("click", () => copy(rowsAsText("\t"), "Result"));
$("#export").addEventListener("click", () => {
  const link = el("a", { href: URL.createObjectURL(new Blob([rowsAsText(",")], { type: "text/csv" })), download: "intentsql-result.csv" });
  document.body.append(link); link.click(); link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
});
$("#copy-sql").addEventListener("click", () => copy(state.reading?.display_sql || "", "SQL"));
$("#edit-sql").addEventListener("click", () => openConsole(state.reading?.display_sql || "", false));

// ---------------------------------------------------------------- import
$("#open-import").addEventListener("click", () => { $("#import-status").textContent = ""; $("#drop-name").textContent = ""; $("#import-dialog").showModal(); });
const drop = $("#drop-zone");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault(); drop.classList.remove("over");
  if (e.dataTransfer.files.length) { $("#import-file").files = e.dataTransfer.files; $("#drop-name").textContent = e.dataTransfer.files[0].name; }
});
$("#import-file").addEventListener("change", () => { $("#drop-name").textContent = $("#import-file").files[0]?.name || ""; });
$("#import-go").addEventListener("click", async (event) => {
  event.preventDefault();
  const file = $("#import-file").files[0];
  const path = $("#import-path").value.trim();
  if (!file && !path) { $("#import-status").textContent = "Choose a file or enter a path."; return; }
  $("#import-status").textContent = "Opening…";
  try {
    const entry = file
      ? await api(`/api/databases/upload?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file })
      : await api("/api/databases/import", { method: "POST", body: JSON.stringify({ path }) });
    $("#import-file").value = ""; $("#import-path").value = "";
    $("#import-dialog").close();
    await loadDatabases(entry.name);
    toast(`Opened ${entry.name}`);
  } catch (err) { $("#import-status").textContent = err.message; }
});
$("#delete-db").addEventListener("click", async () => {
  if (!confirm(`Remove the working copy "${state.database}"? The original file is not affected.`)) return;
  try {
    await api(`/api/databases/${encodeURIComponent(state.database)}`, { method: "DELETE" });
    $("#import-dialog").close(); await loadDatabases();
  } catch (err) { $("#import-status").textContent = err.message; }
});

// ---------------------------------------------------------------- console
function openConsole(sql, run) {
  if (sql) $("#console-sql").value = sql;
  $("#console-error").hidden = true; $("#console-table").replaceChildren();
  $("#console-dialog").showModal();
  if (run) runConsole(); else $("#console-sql").focus();
}
async function runConsole() {
  $("#console-error").hidden = true;
  try {
    const data = await api("/api/sql", { method: "POST", body: JSON.stringify({ database: state.database, sql: $("#console-sql").value }) });
    const box = $("#console-table");
    box.replaceChildren();
    const table = el("table");
    table.append(el("thead", {}, el("tr", {}, data.columns.map((c) => el("th", { scope: "col", text: c })))));
    table.append(el("tbody", {}, data.rows.map((r) => el("tr", {}, r.map((v) => el("td", { class: v === null ? "null" : typeof v === "number" ? "num" : "", text: formatValue(v) }))))));
    box.append(table);
    if (!data.rows.length) box.append(el("p", { class: "empty", text: "No rows." }));
  } catch (err) { $("#console-error").hidden = false; $("#console-error").textContent = err.message; }
}
$("#open-console").addEventListener("click", () => openConsole());
$("#console-run").addEventListener("click", (event) => { event.preventDefault(); runConsole(); });
$("#console-sql").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); runConsole(); }
});

// ---------------------------------------------------------------- start
autosize();
refreshConnection();
loadDatabases().catch((err) => $("#schema-list").replaceChildren(el("p", { class: "error", text: err.message })));
