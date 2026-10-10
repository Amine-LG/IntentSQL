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

// ---------------------------------------------------------------- model (provider) setup
const STATE_TONE = { connected: "ok", demo: "demo", untested: "idle", not_configured: "idle",
  auth_failed: "bad", no_credits: "bad", unavailable: "warn", budget_reached: "warn", error: "bad", incompatible: "bad" };
const PROVIDER_BLURB = {
  typesafe: "TypeSafe's Jev, the model IntentSQL was developed and benchmarked with.",
  liquid: "Liquid AI's d1, a decision model with the same question types.",
  demo: "No model: fixed default choices. Shows how the app works; it does not understand questions.",
};

async function refreshConnection() {
  try {
    const conn = await api("/api/connection");
    state.connection = conn;
    renderModelChip(conn);
    const active = conn.active;
    const usable = active && (active.state !== "not_configured");
    $("#welcome").hidden = Boolean(usable);
    if (!$("#model-dialog").open) state.modelChoice = conn.provider || (conn.providers[0] || {}).name;
    if ($("#model-dialog").open) renderModelDialog();
    return conn;
  } catch (err) {
    $("#model-name").textContent = "Model unavailable";
    return null;
  }
}

function renderModelChip(conn) {
  const active = conn.active;
  const dot = $("#model-dot");
  if (!active) {
    dot.className = "state-dot idle"; $("#model-name").replaceChildren("Choose", el("span", { class: "hide-narrow", text: " a model" })); $("#model-state").textContent = "";
    return;
  }
  dot.className = `state-dot ${STATE_TONE[active.state] || "idle"}`;
  $("#model-name").replaceChildren(...(active.name === "demo" ? ["Demo", el("span", { class: "hide-narrow", text: " mode" })]
    : active.custom ? [active.label, el("span", { class: "hide-narrow", text: " · custom" })]
    : [active.model_label, el("span", { class: "hide-narrow", text: ` · ${active.label}` })]));
  $("#model-state").textContent = active.name === "demo" ? "" : active.state_text;
  $("#model-chip").title = `${active.label} ${active.model_label}: ${active.state_text}${active.message ? ` — ${active.message}` : ""}`;
}

function openModelDialog() {
  state.modelChoice = state.connection?.provider || "typesafe";
  renderModelDialog();
  $("#model-dialog").showModal();
  $(".model-dialog .provider-card.selected")?.focus();      // not the first card's focus ring
}

function renderModelDialog() {
  const conn = state.connection;
  if (!conn) return;
  $("#model-lock").hidden = !conn.locked_by_environment;
  $("#model-lock").textContent = conn.locked_by_environment
    ? "The model is set by the INTENTSQL_PROVIDER environment variable, so it cannot be changed here." : "";
  $("#model-notice").hidden = !conn.notice;
  $("#model-notice").textContent = conn.notice || "";
  const card = (p) => {
    const selected = p.name === state.modelChoice;
    const active = p.name === conn.provider;
    return el("button", {
      type: "button", role: "radio", "aria-checked": String(selected), class: `provider-card${selected ? " selected" : ""}`,
      onclick: () => { state.modelChoice = p.name; state.confirmDelete = false; renderModelDialog(); },
    },
    el("span", { class: "provider-name" }, p.custom ? p.label : p.name === "demo" ? "Demo mode" : `${p.model_label}`,
      p.custom || p.name === "demo" ? null : el("span", { class: "provider-by", text: ` by ${p.label}` })),
    el("span", { class: "provider-blurb", text: p.custom ? `${p.model} · ${hostOf(p.url)}` : PROVIDER_BLURB[p.name] || "" }),
    el("span", { class: `badge-state ${STATE_TONE[p.state] || "idle"}` }, el("span", { class: "state-dot" }),
      p.state_text, active ? " · in use" : ""));
  };
  const builtIn = conn.providers.filter((p) => !p.custom);
  const custom = conn.providers.filter((p) => p.custom);
  const adding = state.modelChoice === "new";
  $("#provider-cards").replaceChildren(...builtIn.map(card));
  $("#custom-cards").replaceChildren(...custom.map(card),
    el("button", { type: "button", role: "radio", "aria-checked": String(adding), class: `provider-card add-card${adding ? " selected" : ""}`,
      onclick: () => { state.modelChoice = "new"; renderModelDialog(); } },
    el("span", { class: "provider-name", text: "+ Custom model" }),
    el("span", { class: "provider-blurb", text: "Connect your own endpoint that speaks the System One decision protocol." })));
  renderProviderDetail();
}

function hostOf(url) { try { return new URL(url).host; } catch { return url; } }

function renderProviderDetail() {
  const conn = state.connection;
  const p = conn.providers.find((x) => x.name === state.modelChoice);
  const box = $("#provider-detail");
  box.replaceChildren();
  if (state.modelChoice === "new" || p?.custom) { renderCustomDetail(box, p || null); return; }
  if (!p) return;
  const active = p.name === conn.provider;
  const status = el("p", { class: `provider-status ${STATE_TONE[p.state] || "idle"}`, role: "status" },
    el("strong", { text: p.state_text }), p.message ? ` — ${p.message}` : "");
  box.append(status);
  if (p.needs_key) {
    const keyInput = el("input", { type: "password", id: "provider-key", autocomplete: "off", spellcheck: "false",
      placeholder: p.has_key ? "A key is saved (leave blank to keep it)" : "Paste your API key" });
    box.append(el("label", { class: "field" }, "API key", keyInput));
    const help = (p.key_help || "").split(": ");
    box.append(el("p", { class: "muted small" }, help[0], help[1] ? el("span", {}, ": ", el("a", { href: help[1], target: "_blank", rel: "noopener", text: help[1].replace("https://", "") })) : ""));
    if (p.models.length > 1) {
      const select = el("select", { id: "provider-model" }, p.models.map((m) => el("option", { value: m, text: m, selected: m === p.model })));
      box.append(el("label", { class: "field inline" }, "Model", select));
    }
  }
  const actions = el("div", { class: "detail-actions" });
  const busy = (button, text) => { button.disabled = true; button.textContent = text; };
  const useLabel = active ? (p.needs_key ? "Save" : "In use") : (p.name === "demo" ? "Use demo mode" : "Save and use");
  const use = el("button", { type: "button", class: "button primary", text: useLabel, disabled: conn.locked_by_environment && !active,
    onclick: async () => {
      busy(use, "Saving…");
      try {
        const body = { provider: p.name };
        const key = $("#provider-key")?.value.trim();
        if (key) body.api_key = key;
        const model = $("#provider-model")?.value;
        if (model) body.model = model;
        state.connection = await api("/api/connection", { method: "POST", body: JSON.stringify(body) });
        renderModelChip(state.connection); renderModelDialog();
        $("#welcome").hidden = state.connection.active?.state !== "not_configured";
        toast(p.name === "demo" ? "Demo mode on" : `${p.model_label} selected`);
      } catch (err) { box.prepend(el("p", { class: "provider-status bad", text: err.message })); }
      finally { use.disabled = false; use.textContent = useLabel; }
    } });
  actions.append(use);
  if (p.needs_key && p.has_key) {
    actions.append(testButton(p));
    actions.append(el("button", { type: "button", class: "button ghost danger", text: "Remove key",
      onclick: async () => {
        state.connection = await api("/api/connection", { method: "POST", body: JSON.stringify({ provider: p.name, clear_key: true }) });
        renderModelChip(state.connection); renderModelDialog(); toast("Key removed");
      } }));
  }
  box.append(actions);
  if (p.needs_key && !p.has_key) box.append(el("p", { class: "muted small", text: "After saving, use Test connection: IntentSQL sends two tiny questions, one Choice and one Noul (a fraction of a cent)." }));
}

function testButton(p) {
  const test = el("button", { type: "button", class: "button", text: "Test connection",
    onclick: async () => {
      test.disabled = true; test.textContent = "Testing…";
      try {
        const out = await api("/api/connection/test", { method: "POST", body: JSON.stringify({ provider: p.name }) });
        state.connection = out; renderModelChip(out); renderModelDialog();
        toast(out.ok ? `${p.custom ? p.label : p.model_label}: connected` : "Connection failed");
      } catch (err) { toast(err.message); renderProviderDetail(); }
    } });
  return test;
}

function renderCustomDetail(box, p) {
  const conn = state.connection;
  const locked = conn.locked_by_environment;
  if (p) {
    box.append(el("p", { class: `provider-status ${STATE_TONE[p.state] || "idle"}`, role: "status" },
      el("strong", { text: p.state_text }), p.message ? ` — ${p.message}` : ""));
  }
  box.append(el("p", { class: "custom-note" },
    el("span", { class: "chip custom-chip", text: "Custom" }),
    " Not verified by IntentSQL. The endpoint must implement the System One decision protocol; a chat API (such as an OpenAI-style /chat/completions URL) will not work. ",
    "Test connection checks that answers come back in the right format, not how good the model is."));
  const field = (label, input, hint) => el("label", { class: "field" }, label, input, hint ? el("span", { class: "muted small", text: hint }) : null);
  const name = el("input", { id: "custom-label", type: "text", value: p?.label || "", maxlength: "40", placeholder: "My decision model", autocomplete: "off" });
  const url = el("input", { id: "custom-url", type: "url", value: p?.url || "", placeholder: "https://example.com/v1/systemone", autocomplete: "off", spellcheck: "false" });
  const model = el("input", { id: "custom-model", type: "text", value: p?.model || "", placeholder: "model-id", autocomplete: "off", spellcheck: "false" });
  const key = el("input", { id: "custom-key", type: "password", autocomplete: "off", spellcheck: "false",
    placeholder: p?.has_key ? "A key is saved (leave blank to keep it)" : "Optional: leave empty if the endpoint needs none" });
  box.append(field("Name", name), field("Endpoint URL", url, "https:// (plain http:// only for localhost)."), field("Model ID", model),
    field("API key", key, "Stored on this computer only; sent only to this endpoint, and dropped if you change the endpoint's address."));
  const kinds = new Set(p?.kinds || ["choice", "noul"]);
  const kind = (k, label, hint) => el("label", { class: "check" },
    el("input", { type: "checkbox", value: k, class: "custom-kind", checked: kinds.has(k) }), el("span", {}, el("strong", { text: label }), ` — ${hint}`));
  const maxChars = el("input", { id: "custom-max", type: "number", min: "2000", max: "1000000", step: "1000", value: String(p?.max_chars || 60000) });
  box.append(el("details", { class: "advanced" }, el("summary", { text: "Advanced" }),
    el("p", { class: "muted small", text: "Decision types the endpoint supports. IntentSQL asks Choice and Noul questions; Test connection sends one of each type you tick." }),
    kind("choice", "Choice", "pick one of several options (required)"),
    kind("noul", "Noul", "a yes/no probability (required)"),
    kind("score", "Score", "a level on a scale (optional; not used by IntentSQL yet)"),
    field("Largest request (characters)", maxChars, "Questions are split into several requests above this size.")));
  box.append(el("details", { class: "protocol" }, el("summary", { text: "What must the endpoint do?" }),
    el("p", { class: "small", text: "Accept a POST with JSON {model, state, questions} and reply with {answers}: one entry per question key." }),
    el("pre", { class: "protocol-sample" }, el("code", { text:
`request:  {"model": "model-id", "state": {...},
           "questions": {"team": {"type": "choice", "instructions": "...",
                                  "criteria": {"billing": "...", "shipping": "..."}},
                         "refund": {"type": "noul", "instructions": "..."}}}
response: {"answers": {"team": {"choice": "billing",
                                "probabilities": {"billing": 0.9, "shipping": 0.1}},
                       "refund": {"noul": 0.85}}}` })),
    el("p", { class: "muted small", text: "With an API key, requests carry Authorization: Bearer <key>. Full details: docs/CUSTOM_MODELS.md." })));
  const actions = el("div", { class: "detail-actions" });
  const error = el("p", { class: "provider-status bad", hidden: true, role: "alert" });
  const active = p && p.name === conn.provider;
  const save = async (select, button, label) => {
    button.disabled = true; button.textContent = "Saving…"; error.hidden = true;
    try {
      const body = { id: p?.id || null, label: name.value.trim(), url: url.value.trim(), model: model.value.trim(),
        kinds: $$(".custom-kind").filter((c) => c.checked).map((c) => c.value), max_chars: Number(maxChars.value) || null, select };
      if (key.value.trim()) body.api_key = key.value.trim();
      const out = await api("/api/custom-models", { method: "POST", body: JSON.stringify(body) });
      state.connection = out; state.modelChoice = out.saved;
      renderModelChip(out); renderModelDialog();
      $("#welcome").hidden = Boolean(out.active);
      toast(select ? `${body.label} selected — test the connection next` : `${body.label} saved`);
    } catch (err) { error.textContent = err.message; error.hidden = false; button.disabled = false; button.textContent = label; }
  };
  const useLabel = active ? "Save" : "Save and use";
  const use = el("button", { type: "button", class: "button primary", text: useLabel, disabled: locked && !active,
    onclick: () => save(true, use, useLabel) });
  actions.append(use);
  if (!active) {
    const only = el("button", { type: "button", class: "button", text: "Save only", onclick: () => save(false, only, "Save only") });
    actions.append(only);
  }
  if (p) {
    actions.append(testButton(p));
    if (p.has_key) actions.append(el("button", { type: "button", class: "button ghost danger", text: "Remove key",
      onclick: async () => {
        try {
          const out = await api("/api/custom-models", { method: "POST", body: JSON.stringify({ id: p.id, label: p.label,
            url: p.url, model: p.model, kinds: p.kinds, max_chars: p.max_chars, clear_key: true }) });
          state.connection = out; renderModelChip(out); renderModelDialog(); toast("Key removed");
        } catch (err) { error.textContent = err.message; error.hidden = false; }
      } }));
    const del = el("button", { type: "button", class: "button ghost danger", text: state.confirmDelete ? "Click again to delete" : "Delete model",
      disabled: locked && active,
      onclick: async () => {
        if (!state.confirmDelete) { state.confirmDelete = true; del.textContent = "Click again to delete"; return; }
        state.confirmDelete = false;
        try {
          const out = await api(`/api/custom-models/${encodeURIComponent(p.id)}`, { method: "DELETE" });
          state.connection = out; state.modelChoice = out.provider || "typesafe";
          renderModelChip(out); renderModelDialog(); $("#welcome").hidden = Boolean(out.active);
          toast(`${p.label} deleted`);
        } catch (err) { error.textContent = err.message; error.hidden = false; }
      } });
    actions.append(del);
  }
  box.append(error, actions);
}

$("#model-chip").addEventListener("click", openModelDialog);
// Enter in a field must not submit the dialog's form (which would close it).
$("#provider-detail").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.tagName === "INPUT") e.preventDefault(); });
$("#welcome-setup").addEventListener("click", openModelDialog);
$("#welcome-demo").addEventListener("click", async () => {
  try {
    state.connection = await api("/api/connection", { method: "POST", body: JSON.stringify({ provider: "demo" }) });
    renderModelChip(state.connection); $("#welcome").hidden = true; toast("Demo mode on");
  } catch (err) { toast(err.message); }
});

// ---------------------------------------------------------------- display settings
function openSettings() {
  $("#theme-select").value = store("intentsql.theme") || "";
  $("#developer").checked = developer();
  $("#settings-dialog").showModal();
}
$("#open-settings").addEventListener("click", openSettings);
$("#theme-select").addEventListener("change", (e) => { store("intentsql.theme", e.target.value || null); applyTheme(e.target.value); });
$("#developer").addEventListener("change", (e) => { store("intentsql.developer", e.target.checked); if (state.result) renderInspector(); });

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
    refreshConnection();          // a real answer (or failure) updates the model's status
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
  if (result.provider === "demo" && result.sql) [tone, label] = ["warn", "Demo answer"];
  const pill = $("#confidence");
  pill.className = `pill ${tone}`; pill.textContent = label;
  pill.title = result.provider === "demo" ? "Demo mode: no model was asked; the choices are fixed defaults"
    : result.confidence !== null && result.confidence !== undefined
    ? `In testing, about ${Math.round(result.confidence * 100)}% of answers with this level of confidence were correct`
    : result.verification !== null && result.verification !== undefined
      ? `Verification score ${Math.round(result.verification * 100)}% (confidence is not calibrated for this provider yet)` : "";
  renderReading();
  renderAlternatives();
  const notice = $("#notice");
  let message = result.message || "";
  if (result.provider === "demo") message = ["Demo mode: the choices are fixed defaults, not a reading of your question.", message].filter(Boolean).join(" ");
  if (result.status === "refused" && !message) message = "IntentSQL could not answer this question.";
  notice.hidden = !message;
  notice.className = "notice" + (result.status === "ambiguous" ? " warn" : ["refused", "error"].includes(result.status) ? " bad" : "");
  notice.textContent = message;
  state.stage = state.stage || null;
  renderInspector();
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

// ---------------------------------------------------------------- decision inspector
// Shows what the pipeline recorded -- nothing here is invented.  Per-decision probabilities
// are the model's own; the overall confidence is a separate, calibrated estimate.
const PURPOSE = {
  "request scope": "Kind of request", output: "What to show", "literal role": "What a value is for",
  "literal column": "Which column a value belongs to", "literal operator": "How a value is compared",
  "literal count": "What is counted", "implicit value": "Implied filter", grouping: "Grouping",
  ordering: "Ordering", limit: "How many results", relationship: "Relationship", "relationship role": "Relationship",
  "aggregate comparison": "Comparison with an aggregate", "missing values": "Missing values", verification: "Check",
  exclusion: "Exclusion", "universal condition": "Every record must match", percentage: "Percentage",
  "join route": "Join route", "column comparison": "Column against column", "output conflict": "Count or total",
  "shared attribute": "Shared attribute",
};
const STAGE_OF = {
  output: "understand", "output conflict": "understand", percentage: "understand", "request scope": "understand",
  "literal role": "values", "literal column": "values", "literal operator": "values", "literal count": "values",
  "implicit value": "values", exclusion: "values", "universal condition": "values", "missing values": "values",
  "shared attribute": "values",
};
const GROUP_TITLE = { understand: "Understanding the question", values: "Values and filters", structure: "Shape of the answer" };

function decisionsOf(result) {
  return (result.decisions || []).filter((d) => d.purpose !== "verification" && !(d.key === "scope" && d.selected === "read"));
}

function stagesOf(result) {
  const g = result.grounding || {};
  const plan = planTables(result.plan);
  const rows = (state.reading?.rows || []).length;
  return [
    { id: "found", title: "Found", metric: g.literals ? `${plural(g.literals.length, "value")} · ${plural((g.tables || []).length, "table")}` : "—" },
    { id: "decided", title: "Decided", metric: plural(decisionsOf(result).length, "decision") },
    { id: "planned", title: "Planned", metric: plan.tables.length ? plural(plan.tables.length, "table") : "—" },
    { id: "checked", title: "Checked", metric: result.provider === "demo" ? "not checked (demo)" : result.verification !== null && result.verification !== undefined ? `${Math.round(result.verification * 100)}% match` : "—" },
    { id: "sql", title: "SQL & data", metric: result.sql ? plural(rows, "row") : "—" },
  ];
}

function renderInspector() {
  const result = state.result;
  const section = $("#inspector");
  section.hidden = !result || (!result.decisions && !result.grounding);
  if (section.hidden) return;
  const u = result.usage || {};
  const t = result.timings_ms || {};
  const source = (state.connection?.providers || []).find((p) => p.name === result.provider);
  const by = result.provider === "demo" ? "demo mode" : source ? (source.custom ? `${source.label} (custom)` : `${source.model_label} by ${source.label}`) : result.provider;
  $("#inspector-meta").textContent = [t.total !== undefined ? `${t.total} ms` : "",
    result.provider === "demo" ? "no model called (demo mode)" : u.calls ? `${plural(u.calls, "model call")}${by ? ` to ${by}` : ""}` : ""].filter(Boolean).join(" · ");
  const stages = stagesOf(result);
  $("#stages").replaceChildren(...stages.map((stage, i) => el("li", {},
    el("button", { type: "button", role: "tab", "aria-selected": String(state.stage === stage.id), class: "stage",
      onclick: () => { state.stage = state.stage === stage.id ? null : stage.id; renderInspector(); } },
    el("span", { class: "stage-n", text: String(i + 1) }),
    el("span", { class: "stage-title", text: stage.title }),
    el("span", { class: "stage-metric", text: stage.metric })))));
  const panel = $("#stage-panel");
  panel.hidden = !state.stage;
  panel.replaceChildren();
  if (!state.stage) return;
  ({ found: panelFound, decided: panelDecided, planned: panelPlanned, checked: panelChecked, sql: panelSql })[state.stage](panel, result);
}

function note(text) { return el("p", { class: "panel-note", text }); }
function chip(text, cls = "") { return el("span", { class: `chip ${cls}`, text }); }
function bar(p, cls = "") {
  const pct = Math.max(0, Math.min(100, Math.round(p * 100)));
  return el("span", { class: `bar ${cls}`, role: "img", "aria-label": `${pct} percent` }, el("span", { style: `width:${pct}%` }));
}
function colLabel(key) { const [t, c] = String(key).split("."); return `${t.replaceAll("_", " ")} · ${(c || "").replaceAll("_", " ")}`; }

function panelFound(panel, result) {
  const g = result.grounding || {};
  panel.append(note("Found by code in your question and the database, before any model call."));
  const lits = g.literals || [];
  panel.append(el("h4", { text: "Values in your question" }));
  panel.append(lits.length ? el("ul", { class: "evidence" }, lits.map((l) => el("li", {},
    chip(l.kind === "text" ? `“${l.text}”` : l.text, "value"),
    el("span", { class: "arrow", "aria-hidden": "true", text: "→" }),
    l.columns.length ? el("span", { class: "chips" }, l.columns.map((c) => chip(colLabel(c), "col")))
      : el("span", { class: "muted small", text: l.kind === "number" ? "a number (its role is decided next)" : l.kind === "period" ? `a period: ${l.value}` : "not stored in the database" })))) : el("p", { class: "muted small", text: "No values (names, numbers or dates) in the question." }));
  if ((g.tables || []).length || (g.columns || []).length) {
    panel.append(el("h4", { text: "Words that point to the schema" }));
    panel.append(el("div", { class: "chips" }, [...(g.tables || []).map((t) => chip(t.label, "table")),
      ...(g.columns || []).map((c) => chip(colLabel(c.key), "col"))]));
  }
  if ((g.measures || []).length) {
    panel.append(el("h4", { text: "Calculations offered" }));
    panel.append(el("div", { class: "chips" }, g.measures.map((m) => chip(m, "measure"))));
  }
  panel.append(el("p", { class: "muted small", text: `${g.schema_tables || "All"} tables were available to choose from.` }));
}

function decisionCard(d, demo = false) {
  const probs = d.probabilities || null;
  const options = d.options || {};
  let chosenKey = null; let chosenP = Number(d.probability) || 0;
  if (probs && Object.keys(probs).length) {
    chosenKey = Object.entries(probs).sort((a, b) => b[1] - a[1])[0][0];
    chosenP = probs[chosenKey];
  }
  const isNoul = !probs;
  const chosenText = isNoul ? (chosenP >= 0.5 ? "Yes" : "No") : (options[chosenKey] || (typeof d.selected === "string" ? d.selected : chosenKey));
  const question = String(d.asked || d.question || "");
  const marked = (text) => String(text).split(/(\[[^\]]*\])/).map((part) => part.startsWith("[") ? el("mark", { text: part.slice(1, -1) }) : part);
  const others = probs ? Object.entries(probs).filter(([k]) => k !== chosenKey).sort((a, b) => b[1] - a[1]).slice(0, 4) : [];
  const card = el("li", { class: "decision-card" },
    el("div", { class: "decision-top" },
      el("span", { class: "decision-kind", text: PURPOSE[d.purpose] || d.purpose }),
      el("span", { class: "decision-p", text: demo ? "fixed default" : `${Math.round((isNoul ? Math.max(chosenP, 1 - chosenP) : chosenP) * 100)}%` })),
    el("p", { class: "decision-q" }, marked(question)),
    d.context ? el("p", { class: "decision-context" }, "“", ...marked(d.context), "”") : null,
    el("div", { class: "decision-choice" }, el("span", { class: "chosen", text: chosenText }), demo ? null : bar(isNoul ? Math.max(chosenP, 1 - chosenP) : chosenP, "chosen-bar")));
  if (demo && others.length) {
    const rest = Object.keys(probs).filter((k) => k !== chosenKey);
    card.append(el("details", { class: "others" }, el("summary", { text: rest.length > 6 ? `6 of ${rest.length} other options` : `Other options (${rest.length})` }),
      el("ul", {}, rest.slice(0, 6).map((k) => el("li", {}, el("span", { class: "other-text", text: options[k] || k }))))));
  } else if (others.length && others.some(([, p]) => p > 0.005)) {
    card.append(el("details", { class: "others" }, el("summary", { text: Object.keys(probs).length - 1 > others.length ? `Top ${others.length} of ${Object.keys(probs).length - 1} other options` : `Other options (${others.length})` }),
      el("ul", {}, others.map(([k, p]) => el("li", {}, el("span", { class: "other-text", text: options[k] || k }), bar(p), el("span", { class: "other-p", text: `${Math.round(p * 100)}%` }))))));
  }
  return card;
}

function panelDecided(panel, result) {
  const decisions = decisionsOf(result);
  panel.append(note(result.provider === "demo"
    ? "Demo mode: no model is asked, so each decision takes a fixed default option. With a model, each card shows the model's probability for its choice."
    : "The model chose among options that IntentSQL built from your database. Percentages are the model's probability for each choice — not the chance that the whole answer is right."));
  if (!decisions.length) { panel.append(el("p", { class: "muted small", text: "No decisions were recorded for this answer." })); return; }
  const groups = { understand: [], values: [], structure: [] };
  for (const d of decisions) groups[STAGE_OF[d.purpose] || "structure"].push(d);
  for (const [id, list] of Object.entries(groups)) {
    if (!list.length) continue;
    panel.append(el("h4", { text: GROUP_TITLE[id] }), el("ol", { class: "decision-list" }, list.map((d) => decisionCard(d, result.provider === "demo"))));
  }
}

function planTables(plan) {
  const tables = []; const joins = [];
  const visit = (node) => {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) { node.forEach(visit); return; }
    if (node.node === "From") {
      if (node.base?.node === "TableRef" && !tables.includes(node.base.table)) tables.push(node.base.table);
      for (const j of node.joins || []) {
        const right = j.right?.table;
        if (right && !tables.includes(right)) tables.push(right);
        const on = (j.on || []).map((pair) => `${pair[0]?.column} = ${pair[1]?.column}`).join(", ");
        joins.push({ to: right, on, kind: j.kind || "INNER" });
      }
      if (node.base?.node === "Derived") visit(node.base.query);
      return;
    }
    for (const value of Object.values(node)) if (value && typeof value === "object") visit(value);
  };
  visit(plan);
  return { tables, joins };
}

function describeIntent(intent) {
  if (!intent) return [];
  const col = (k) => (k ? colLabel(k) : "");
  const rows = [];
  const outputs = (intent.outputs || []).map((o) => o.star ? `all of ${o.table}` : o.func === "COUNT" && !o.column ? `number of ${o.table}` : o.func && o.func !== "EXPR" ? `${o.func.toLowerCase()}(${col(o.column)})` : col(o.column));
  if (outputs.length) rows.push(["Show", outputs]);
  const conds = (intent.conditions || []).map((c) => {
    if (c.kind === "absent") return `no related ${c.table}`;
    if (c.kind === "related") return `has related ${c.table}`;
    const values = (c.values || []).map((v) => (typeof v === "string" ? `'${v}'` : v)).join(", ");
    return `${col(c.column)} ${c.kind === "compare" || c.kind === "text" ? c.op || "=" : c.kind.replaceAll("_", " ")} ${values}`.trim();
  });
  if (conds.length) rows.push([intent.connector === "OR" ? "Any of" : "Where", conds]);
  if ((intent.excluded || []).length) rows.push(["Except", intent.excluded.map((c) => `${col(c.column)} ${c.op || "="} ${(c.values || []).join(", ")}`)]);
  if ((intent.group_by || []).length) rows.push(["Per", intent.group_by.map(col)]);
  if ((intent.order || []).length) rows.push(["Order", intent.order.map((o) => `${o.func ? `${o.func.toLowerCase()} ` : ""}${o.column ? col(o.column) : `number of ${o.table}`} ${o.desc ? "↓" : "↑"}`)]);
  if (intent.limit) rows.push(["Keep", [`top ${intent.limit}${intent.offset ? ` after ${intent.offset}` : ""}`]]);
  return rows;
}

function panelPlanned(panel, result) {
  panel.append(note("Deterministic code turned the decisions into a typed plan, then chose the joins from the database's keys."));
  const { tables, joins } = planTables(result.plan);
  if (tables.length) {
    panel.append(el("h4", { text: "Tables and how they are joined" }));
    const diagram = el("div", { class: "join-diagram" }, el("span", { class: "node", text: tables[0] }));
    for (const j of joins) diagram.append(el("span", { class: "edge", title: j.kind, text: j.on || "" }), el("span", { class: "node", text: j.to }));
    panel.append(diagram);
  }
  const rows = describeIntent(result.intent);
  if (rows.length) {
    panel.append(el("h4", { text: "Structured interpretation" }));
    panel.append(el("dl", { class: "plan-rows" }, rows.flatMap(([k, items]) => [el("dt", { text: k }), el("dd", {}, el("div", { class: "chips" }, items.map((i) => chip(i, "plan"))))])));
  }
  if (!tables.length && !rows.length) panel.append(el("p", { class: "muted small", text: "No plan was built for this request." }));
}

function panelChecked(panel, result) {
  panel.append(note("Before showing the answer, IntentSQL asks the model whether the plain-English reading matches your question, and compares it with other readings that would return different data."));
  const demo = result.provider === "demo";
  if (demo) {
    panel.append(el("p", { class: "panel-note warn", text: "Demo mode: no model checked this answer, so there is no check score." }));
  }
  if (demo || result.verification === null || result.verification === undefined) {
    if (!demo) panel.append(el("p", { class: "muted small", text: "No check was run for this answer." }));
    return;
  }
  panel.append(el("div", { class: "check-row" }, el("span", { text: "Reading matches the question" }), bar(result.verification), el("strong", { text: `${Math.round(result.verification * 100)}%` })));
  if (result.confidence !== null && result.confidence !== undefined) {
    panel.append(el("p", { class: "small", text: `Overall: in testing with this model, about ${Math.round(result.confidence * 100)}% of answers with this level of evidence were correct.` }));
  } else {
    panel.append(el("p", { class: "muted small", text: "No calibrated overall confidence for this model yet; the check score above is the model's own." }));
  }
  const pick = (result.decisions || []).find((d) => d.key === "pick");
  if (pick && pick.options) {
    panel.append(el("h4", { text: "Readings compared" }));
    panel.append(el("ul", { class: "readings" }, Object.entries(pick.options).map(([k, text]) => el("li", { class: k === pick.selected ? "picked" : "" },
      el("span", { class: "reading-letter", text: k }), el("span", { class: "reading-text", text }), bar(pick.probabilities?.[k] || 0), el("span", { class: "other-p", text: `${Math.round((pick.probabilities?.[k] || 0) * 100)}%` })))));
  }
}

function panelSql(panel, result) {
  panel.append(note("The SQL runs on a read-only connection. Values are passed as bound parameters, never pasted into the SQL."));
  if (result.sql) {
    panel.append(el("pre", { class: "sql small-sql" }, el("code", {}, highlight(result.sql))));
    if ((result.params || []).length) panel.append(el("p", { class: "small" }, "Bound values: ", el("span", { class: "chips" }, result.params.map((v) => chip(String(v), "value")))));
  }
  const u = result.usage || {};
  const providerLabel = (state.connection?.providers || []).find((p) => p.name === (result.provider || u.provider));
  const facts = [["Model", providerLabel ? (providerLabel.name === "demo" ? "Demo mode (no model)" : `${providerLabel.model_label} · ${providerLabel.label}${(u.models || []).length ? ` (${u.models.join(", ")})` : ""}`) : "—"],
    ["Model calls", u.calls ? `${u.calls} · ${(u.input_tokens || 0).toLocaleString()} input tokens${u.cost_usd ? ` · $${Number(u.cost_usd).toFixed(5)}` : ""}` : "—"],
    ["Time", Object.entries(result.timings_ms || {}).map(([k, v]) => `${k} ${v} ms`).join(" · ") || "—"]];
  panel.append(el("dl", { class: "steps" }, facts.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })])));
  if (developer()) {
    const tech = (title, value) => el("details", { class: "tech" }, el("summary", { text: title }), el("pre", { text: JSON.stringify(value, null, 2) }));
    panel.append(tech("Semantic plan (JSON)", result.intent), tech("SQL plan (JSON)", result.plan),
      tech("Grounding (JSON)", result.grounding), tech(`Model exchanges (${(result.exchanges || []).length})`, result.exchanges));
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
api("/api/health").then((h) => { $("#app-version").textContent = String(h.version || "").replace(/b(\d+)$/, "-beta.$1"); }).catch(() => {});
loadDatabases().catch((err) => $("#schema-list").replaceChildren(el("p", { class: "error", text: err.message })));
