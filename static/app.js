"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { database: null, schema: null, last: null, tab: "decisions" };

const EXAMPLES = {
  "cyberchase.db": [
    "How many episodes are in season 2?",
    "List the titles of episodes that aired in 2004, earliest first.",
    "Which topics appear in more than 5 episodes?",
  ],
  "dese.db": [
    "Which five districts have the highest per-pupil expenditure?",
    "How many public schools are in Cambridge?",
    "Show each district name with its number of schools, most schools first.",
  ],
  "moneyball.db": [
    "Who were the ten highest paid players in 2001?",
    "What is the average salary for each year?",
    "Which players never had a salary record?",
  ],
};

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
    else if (value !== undefined && value !== null) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function store(key, value) {
  try { if (value === undefined) return JSON.parse(localStorage.getItem(key)); localStorage.setItem(key, JSON.stringify(value)); }
  catch { return null; }
}

function renderTable(container, columns, rows) {
  container.replaceChildren();
  if (!columns.length) { container.append(el("p", { class: "muted", style: "padding:10px", text: "No columns." })); return; }
  const table = el("table");
  table.append(el("thead", {}, el("tr", {}, columns.map((c) => el("th", { scope: "col", text: c })))));
  const body = el("tbody");
  for (const row of rows) {
    body.append(el("tr", {}, row.map((v) => {
      if (v === null) return el("td", { class: "null", text: "NULL" });
      return el("td", { class: typeof v === "number" ? "num" : "", text: String(v) });
    })));
  }
  table.append(body);
  container.append(table);
  if (!rows.length) container.append(el("p", { class: "muted", style: "padding:10px", text: "The query returned no rows." }));
}

// ---------------------------------------------------------------- theme
function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme; else delete document.documentElement.dataset.theme;
}
$("#theme").addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  applyTheme(next); store("intentsql.theme", next);
});
applyTheme(store("intentsql.theme"));

// ---------------------------------------------------------------- connection
async function refreshConnection() {
  try {
    const conn = await api("/api/connection");
    $("#conn-dot").className = "dot " + (conn.has_key ? "ok" : "bad");
    $("#conn-url").value = conn.url; $("#conn-model").value = conn.model;
    $("#conn-status").textContent = conn.has_key ? "A key is configured." : "No API key configured yet.";
    return conn;
  } catch (err) { $("#conn-dot").className = "dot bad"; return null; }
}
$("#open-connection").addEventListener("click", async () => { await refreshConnection(); $("#connection-dialog").showModal(); });
$("#conn-save").addEventListener("click", async (event) => {
  event.preventDefault();
  try {
    await api("/api/connection", { method: "POST", body: JSON.stringify({
      url: $("#conn-url").value, model: $("#conn-model").value, api_key: $("#conn-key").value || null }) });
    $("#conn-key").value = "";
    await refreshConnection();
    $("#conn-status").textContent = "Saved.";
  } catch (err) { $("#conn-status").textContent = err.message; }
});

// ---------------------------------------------------------------- databases & schema
async function loadDatabases(select) {
  const list = await api("/api/databases");
  const picker = $("#database");
  picker.replaceChildren(...list.map((d) => el("option", { value: d.name, text: d.name + (d.bundled ? " (example)" : "") })));
  const wanted = select || store("intentsql.database");
  if (list.some((d) => d.name === wanted)) picker.value = wanted;
  await selectDatabase(picker.value, list);
}

async function selectDatabase(name, list) {
  if (!name) return;
  state.database = name; store("intentsql.database", name);
  $("#db-name").textContent = name;
  const entry = (list || []).find((d) => d.name === name);
  $("#delete-db").hidden = !entry || entry.bundled;
  renderExamples(name);
  renderHistory();
  $("#schema-list").replaceChildren(el("p", { class: "muted", text: "Loading…" }));
  try {
    state.schema = await api(`/api/databases/${encodeURIComponent(name)}/schema`);
    renderSchema();
  } catch (err) {
    $("#schema-list").replaceChildren(el("p", { class: "error", text: err.message }));
  }
}

function renderSchema() {
  const filter = $("#schema-filter").value.trim().toLowerCase();
  const fks = new Set((state.schema.foreign_keys || []).flatMap((fk) => fk.columns.map((c) => `${fk.table}.${c}`)));
  const tables = state.schema.tables.filter((t) => !filter || t.name.toLowerCase().includes(filter)
    || t.columns.some((c) => c.name.toLowerCase().includes(filter)));
  $("#schema-count").textContent = `· ${state.schema.tables.length} tables`;
  $("#schema-list").replaceChildren(...tables.map((t) => el("details", { open: tables.length <= 3 ? "" : null },
    el("summary", {}, el("span", { text: t.name }), el("span", { class: "muted", text: `${t.rows} rows` })),
    el("ul", {}, t.columns.map((c) => el("li", {},
      el("span", {}, c.name, c.primary_key ? el("span", { class: "key", text: "PK", title: "primary key" }) : null,
        fks.has(`${t.name}.${c.name}`) ? el("span", { class: "key", text: "FK", title: "foreign key" }) : null),
      el("span", { class: "kind", text: c.type || c.kind })))),
    el("button", { class: "ghost small preview", type: "button", text: "Preview rows",
      onclick: () => openConsole(`SELECT * FROM "${t.name.replaceAll('"', '""')}" LIMIT 50`, true) }),
  )));
  if (!tables.length) $("#schema-list").append(el("p", { class: "muted", text: "No matches." }));
}
$("#schema-filter").addEventListener("input", () => state.schema && renderSchema());
$("#database").addEventListener("change", (event) => selectDatabase(event.target.value));

function renderExamples(name) {
  const examples = EXAMPLES[name] || [];
  $("#examples").replaceChildren(...examples.map((q) => el("button", { type: "button", text: q,
    onclick: () => { $("#question").value = q; ask(); } })));
}

// ---------------------------------------------------------------- history
function historyKey() { return `intentsql.history.${state.database}`; }
function remember(question) {
  const items = (store(historyKey()) || []).filter((q) => q !== question);
  items.unshift(question); store(historyKey(), items.slice(0, 12)); renderHistory();
}
function renderHistory() {
  const items = store(historyKey()) || [];
  $("#history").hidden = !items.length;
  $("#history-list").replaceChildren(...items.map((q) => el("li", {}, el("button", { type: "button", text: q,
    onclick: () => { $("#question").value = q; ask(); } }))));
}

// ---------------------------------------------------------------- asking
const STATUS_TEXT = { answered: "Answered", ambiguous: "Ambiguous", refused: "Not answered", error: "Error" };

async function ask() {
  const question = $("#question").value.trim();
  if (!question || !state.database) return;
  const button = $("#ask");
  button.disabled = true; button.replaceChildren(el("span", { class: "spinner", "aria-hidden": "true" }), " Asking");
  try {
    const result = await api("/api/ask", { method: "POST", body: JSON.stringify({ database: state.database, question }) });
    state.last = result; remember(question); renderResult(result);
  } catch (err) {
    state.last = null;
    renderResult({ status: "error", message: err.message, rows: [], columns: [] });
  } finally {
    button.disabled = false; button.textContent = "Ask";
  }
}
$("#ask-form").addEventListener("submit", (event) => { event.preventDefault(); ask(); });
$("#question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); ask(); }
});

function renderResult(result) {
  $("#result").hidden = false;
  const badge = $("#status");
  badge.className = "badge " + result.status; badge.textContent = STATUS_TEXT[result.status] || result.status;
  $("#explanation").textContent = result.explanation || "";
  const message = $("#message");
  message.hidden = !result.message; message.textContent = result.message || "";
  message.className = "message" + (result.status === "error" ? " error" : "");
  const alternatives = $("#alternatives");
  alternatives.replaceChildren();
  alternatives.hidden = !(result.alternatives && result.alternatives.length);
  if (!alternatives.hidden) {
    alternatives.append(el("p", { class: "muted", text: "Other readings of the request:" }));
    for (const alt of result.alternatives) {
      alternatives.append(el("button", { type: "button", class: "ghost", text: alt.explanation,
        onclick: () => renderResult({ ...result, ...alt, status: "answered", alternatives: [], message: null }) }));
    }
  }
  const hasSql = Boolean(result.display_sql);
  $(".sql-block").hidden = !hasSql;
  $("#sql code").textContent = result.display_sql || "";
  $("#sql-params").textContent = hasSql ? `${result.sql}\n\n-- parameters: ${JSON.stringify(result.params)}` : "";
  const answered = result.status === "answered" || result.status === "ambiguous";
  $(".table-head").hidden = !answered;
  $("#table").hidden = !answered;
  if (answered) {
    renderTable($("#table"), result.columns || [], result.rows || []);
    const n = (result.rows || []).length;
    $("#row-count").textContent = `${n} row${n === 1 ? "" : "s"}${result.truncated ? " (first rows only)" : ""}`;
  }
  renderDebug();
}

$("#copy-sql").addEventListener("click", () => navigator.clipboard?.writeText($("#sql code").textContent));
$("#export").addEventListener("click", () => {
  if (!state.last) return;
  const quote = (v) => v === null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replaceAll('"', '""')}"` : String(v);
  const lines = [state.last.columns.map(quote).join(","), ...state.last.rows.map((r) => r.map(quote).join(","))];
  const link = el("a", { href: URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" })), download: "intentsql-result.csv" });
  link.click(); URL.revokeObjectURL(link.href);
});

// ---------------------------------------------------------------- debug
$("#debug").checked = Boolean(store("intentsql.debug"));
$("#debug").addEventListener("change", () => { store("intentsql.debug", $("#debug").checked); renderDebug(); });
document.querySelectorAll(".tabs button").forEach((button) => button.addEventListener("click", () => {
  state.tab = button.dataset.tab;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b === button)));
  renderDebug();
}));

function probabilityRows(probabilities, options, limit = 4) {
  return Object.entries(probabilities || {}).sort((a, b) => b[1] - a[1]).slice(0, limit).map(([key, p]) =>
    el("div", { class: "prob" },
      el("span", { class: "label", title: (options && options[key]) || key, text: (options && options[key]) || key }),
      el("span", { class: "bar" }, el("span", { style: `width:${Math.round(p * 100)}%` })),
      el("span", { text: `${Math.round(p * 100)}%` })));
}

function renderDebug() {
  const panel = $("#debug-panel");
  const result = state.last;
  panel.hidden = !$("#debug").checked || !result;
  if (panel.hidden) return;
  const body = $("#tab-body");
  body.replaceChildren();
  if (state.tab === "decisions") {
    const decisions = result.decisions || [];
    if (!decisions.length) body.append(el("p", { class: "muted", text: "No decisions recorded." }));
    for (const d of decisions) {
      const box = el("div", { class: "decision" },
        el("div", { class: "head" }, el("span", { class: "purpose", text: d.purpose }),
          el("span", { class: "muted", text: `p = ${Number(d.probability).toFixed(2)}` })),
        el("div", { text: `${d.question} → ${typeof d.selected === "object" ? JSON.stringify(d.selected) : d.selected}` }));
      if (d.probabilities) box.append(...probabilityRows(d.probabilities, d.options));
      body.append(box);
    }
  } else if (state.tab === "intent") {
    body.append(el("pre", { text: JSON.stringify(result.intent, null, 2) || "—" }));
  } else if (state.tab === "plan") {
    body.append(el("pre", { text: JSON.stringify(result.plan, null, 2) || "—" }));
  } else if (state.tab === "calls") {
    const calls = result.exchanges || [];
    if (!calls.length) body.append(el("p", { class: "muted", text: "No Jev calls." }));
    calls.forEach((c, i) => body.append(el("details", { class: "decision" },
      el("summary", { text: `#${i + 1} ${c.purpose || "call"} · ${Object.keys(c.questions).length} questions · ${c.input_tokens} input tokens · ${c.latency_ms} ms${c.cached ? " · cached" : ""}` }),
      el("pre", { text: JSON.stringify({ state: c.state, questions: c.questions, answers: c.answers, model: c.model }, null, 2) }))));
  } else if (state.tab === "usage") {
    const u = result.usage || {}; const t = result.timings_ms || {};
    const rows = [["Jev calls", u.calls], ["Rounds", u.rounds], ["Input tokens", u.input_tokens], ["Output tokens", u.output_tokens],
      ["Retries", u.retries], ...Object.entries(t).map(([k, v]) => [`${k} (ms)`, v])];
    body.append(el("div", { class: "kv" }, rows.flatMap(([k, v]) => [el("span", { class: "muted", text: k }), el("span", { text: v ?? "—" })])));
  }
}

// ---------------------------------------------------------------- import
$("#open-import").addEventListener("click", () => { $("#import-status").textContent = ""; $("#import-dialog").showModal(); });
$("#import-go").addEventListener("click", async (event) => {
  event.preventDefault();
  const file = $("#import-file").files[0];
  const path = $("#import-path").value.trim();
  $("#import-status").textContent = "Importing…";
  try {
    const entry = file
      ? await api(`/api/databases/upload?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file })
      : path ? await api("/api/databases/import", { method: "POST", body: JSON.stringify({ path }) })
        : (() => { throw new Error("Choose a file or enter a path."); })();
    $("#import-status").textContent = `Imported as ${entry.name}.`;
    $("#import-file").value = ""; $("#import-path").value = "";
    await loadDatabases(entry.name);
  } catch (err) { $("#import-status").textContent = err.message; }
});
$("#delete-db").addEventListener("click", async () => {
  if (!confirm(`Delete the working copy "${state.database}"? The original file is not affected.`)) return;
  try { await api(`/api/databases/${encodeURIComponent(state.database)}`, { method: "DELETE" }); $("#import-dialog").close(); await loadDatabases(); }
  catch (err) { $("#import-status").textContent = err.message; }
});

// ---------------------------------------------------------------- console
function openConsole(sql, run) {
  $("#console-sql").value = sql || $("#console-sql").value;
  $("#console-error").hidden = true; $("#console-table").replaceChildren();
  $("#console-dialog").showModal();
  if (run) runConsole();
}
async function runConsole() {
  $("#console-error").hidden = true;
  try {
    const data = await api("/api/sql", { method: "POST", body: JSON.stringify({ database: state.database, sql: $("#console-sql").value }) });
    renderTable($("#console-table"), data.columns, data.rows);
  } catch (err) { $("#console-error").hidden = false; $("#console-error").textContent = err.message; }
}
$("#open-console").addEventListener("click", () => openConsole());
$("#console-run").addEventListener("click", (event) => { event.preventDefault(); runConsole(); });

// ---------------------------------------------------------------- start
refreshConnection();
loadDatabases().catch((err) => $("#schema-list").replaceChildren(el("p", { class: "error", text: err.message })));
