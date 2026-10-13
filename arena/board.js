// KINTSUGI-Omega Arena board (Stage 11). Reads GET /api/state, posts POST /api/sabotage.
// No dependencies and no external requests. Every value from the API is inserted as text,
// never as HTML.
"use strict";

const STATE_URL = "/api/state";
const SABOTAGE_URL = "/api/sabotage";
const REFRESH_MS = 15000;
const SVG_NS = "http://www.w3.org/2000/svg";

let current = null;

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, String(value));
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function svg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
  return node;
}

function chip(text, kind) {
  return el("span", { class: `chip ${kind || ""}`.trim(), text });
}

function pct(x) {
  return typeof x === "number" ? `${(100 * x).toFixed(1)}%` : "—";
}

function short(hash) {
  return hash ? `${String(hash).slice(0, 12)}…` : "—";
}

function setText(id, text) {
  document.getElementById(id).textContent = text;
}

function empty(text) {
  return el("li", { class: "empty", text });
}

function renderHead(state) {
  const ledger = state.ledger || {};
  setText("window", state.window || "—");
  setText("ledger-seq", String(ledger.seq ?? 0));
  setText("ledger-head", short(ledger.h));
  setText("updated", new Date().toLocaleTimeString());
}

function renderFleet(state) {
  const root = document.getElementById("fleet");
  root.replaceChildren();
  for (const host of state.fleet || []) {
    const miss = host.meets_slo === false;
    const bar = el("div", { class: `bar${miss ? " miss" : ""}`, role: "img",
      "aria-label": `pass rate ${pct(host.pass_rate)}, SLO ${pct(host.slo_pass_rate)}` });
    const [lo, hi] = Array.isArray(host.wilson95) ? host.wilson95 : [null, null];
    if (typeof lo === "number" && typeof hi === "number") {
      const ci = el("div", { class: "ci" });
      ci.style.left = `${100 * lo}%`;
      ci.style.width = `${Math.max(0.5, 100 * (hi - lo))}%`;
      bar.append(ci);
    }
    if (typeof host.pass_rate === "number") {
      const pt = el("div", { class: "pt" });
      pt.style.left = `calc(${100 * host.pass_rate}% - 2px)`;
      bar.append(pt);
    }
    if (typeof host.slo_pass_rate === "number") {
      const slo = el("div", { class: "slo", title: "SLO" });
      slo.style.left = `${100 * host.slo_pass_rate}%`;
      bar.append(slo);
    }
    const status = host.meets_slo === undefined || host.meets_slo === null
      ? chip("no baseline", "warn") : chip(miss ? "below SLO" : "meets SLO", miss ? "bad" : "ok");
    root.append(el("article", { class: "card" },
      el("h3", { text: host.host }),
      el("p", { text: host.description || "" }),
      el("div", { class: "rate", text: pct(host.pass_rate) }),
      bar,
      el("p", {}, status, " ", chip(host.tier || "—", "info"), " ",
        chip(`model: ${host.model_mode || "—"}`)),
      el("p", { text: `SLO ${pct(host.slo_pass_rate)} · ${host.tasks ?? "—"} tasks · p95 ` +
        `${host.p95_latency_s ?? "—"} s` }),
      el("p", { text: `external tools: ${(host.external_tools || []).join(", ") || "—"}` })));
  }
  if (!root.childElementCount) root.append(el("p", { class: "empty", text: "No fleet hosts." }));
}

function renderIncidents(state) {
  const root = document.getElementById("incidents");
  root.replaceChildren();
  for (const item of state.incidents || []) {
    root.append(el("li", {},
      chip(item.type, item.type), " ", el("strong", { text: item.incident || "—" }),
      item.host ? ` on ${item.host}` : "", el("br"),
      item.summary || "",
      el("div", { class: "meta", text: `#${item.seq} · ${item.at || ""}` })));
  }
  if (!root.childElementCount) {
    root.append(empty(state.window === "before T0"
      ? "No incidents yet: the ledger starts at T0." : "No incidents recorded."));
  }
}

function renderImmunity(state) {
  const table = document.getElementById("immunity");
  table.replaceChildren();
  const immunity = state.immunity || { hosts: [], rows: {} };
  const hosts = [...new Set([...(state.fleet || []).map((h) => h.host), ...immunity.hosts])];
  const rows = Object.entries(immunity.rows || {});
  if (!rows.length) {
    table.append(el("caption", { class: "empty", text: "No antibodies yet." }));
    return;
  }
  table.append(el("thead", {}, el("tr", {}, el("th", { text: "Antibody" }),
    ...hosts.map((h) => el("th", { scope: "col", text: h })))));
  const body = el("tbody");
  for (const [ref, cells] of rows) {
    const state_ = (state.antibodies || {})[ref] || {};
    body.append(el("tr", {},
      el("th", { scope: "row" }, ref, " ", chip(String(state_.state || "").toLowerCase(),
        String(state_.state || "").toLowerCase())),
      ...hosts.map((h) => el("td", {}, cells[h] ? chip(cells[h], cells[h]) : "—"))));
  }
  table.append(body);
}

function renderCurve(state) {
  const root = document.getElementById("curve");
  root.replaceChildren();
  const curve = state.learning_curve || {};
  const points = (curve.points || []).filter((p) => typeof p.cost_usd === "number" && p.cost_usd > 0);
  if (!points.length) {
    root.append(el("p", { class: "empty", text: "No responses recorded yet." }));
    return;
  }
  const W = 480, H = 240, L = 52, R = 12, T = 12, B = 34;
  const maxN = Math.max(...points.map((p) => p.n), 2);
  const costs = points.map((p) => p.cost_usd);
  const fit = (n) => curve.c1 * Math.pow(n, -curve.b);
  const lo = Math.log10(Math.min(...costs) / 2), hi = Math.log10(Math.max(...costs) * 2);
  const x = (n) => L + ((n - 1) / (maxN - 1)) * (W - L - R);
  const y = (c) => T + (1 - (Math.log10(c) - lo) / (hi - lo)) * (H - T - B);
  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Response cost falls from $${costs[0].toFixed(2)} to ` +
      `$${costs[costs.length - 1].toFixed(2)} over ${points.length} responses` });
  chart.append(svg("line", { class: "axis", x1: L, y1: H - B, x2: W - R, y2: H - B }));
  chart.append(svg("line", { class: "axis", x1: L, y1: T, x2: L, y2: H - B }));
  for (let e = Math.ceil(lo); e <= Math.floor(hi); e++) {
    const label = svg("text", { class: "label", x: L - 6, y: y(10 ** e) + 4, "text-anchor": "end" });
    label.textContent = `$${10 ** e >= 1 ? 10 ** e : (10 ** e).toFixed(Math.abs(e))}`;
    chart.append(label, svg("line", { class: "axis", x1: L, y1: y(10 ** e), x2: W - R, y2: y(10 ** e) }));
  }
  for (let n = 1; n <= maxN; n++) {
    const label = svg("text", { class: "label", x: x(n), y: H - B + 16, "text-anchor": "middle" });
    label.textContent = String(n);
    chart.append(label);
  }
  const axis = svg("text", { class: "label", x: (L + W - R) / 2, y: H - 4, "text-anchor": "middle" });
  axis.textContent = "response number";
  chart.append(axis);
  if (curve.status === "fitted" && typeof curve.c1 === "number" && typeof curve.b === "number") {
    const steps = [];
    for (let i = 0; i <= 40; i++) {
      const n = 1 + (i / 40) * (maxN - 1);
      const c = Math.min(Math.max(fit(n), 10 ** lo), 10 ** hi);
      steps.push(`${i ? "L" : "M"}${x(n).toFixed(1)},${y(c).toFixed(1)}`);
    }
    chart.append(svg("path", { class: "fit", d: steps.join(" ") }));
  }
  for (const p of points) {
    const dot = svg("circle", { class: "dot", cx: x(p.n), cy: y(p.cost_usd), r: 5 });
    const title = svg("title");
    title.textContent = `#${p.n} ${p.ref || ""}: $${p.cost_usd.toFixed(4)}`;
    dot.append(title);
    chart.append(dot);
  }
  root.append(chart);
  const phases = (curve.phases || []).join(", ") || "—";
  setText("curve-note", `Cost per response (log scale) · Wright fit b = ${curve.b?.toFixed?.(2) ?? "—"}` +
    `, progress ratio ${curve.progress_ratio?.toFixed?.(2) ?? "—"}, R² ${curve.r2?.toFixed?.(2) ?? "—"}` +
    ` · phase: ${phases}`);
}

function renderLedger(state) {
  const ledger = state.ledger || {};
  const kv = document.getElementById("ledger");
  kv.replaceChildren(
    el("dt", { text: "Entries" }), el("dd", { text: String(ledger.seq ?? 0) }),
    el("dt", { text: "Head hash" }), el("dd", { class: "mono", text: ledger.h || "— (before T0)" }),
    el("dt", { text: "Arena" }), el("dd", {}, state.open ? chip("open", "ok") : chip("closed", "warn")));
  const esc = document.getElementById("escalations");
  esc.replaceChildren();
  for (const e of [...(state.escalations || [])].reverse()) {
    esc.append(el("li", {}, el("strong", { text: e.incident || "—" }), ` ${e.reason || ""}`,
      el("div", { class: "meta", text: e.at || "" })));
  }
  if (!esc.childElementCount) esc.append(empty("No escalations."));
  const abs = document.getElementById("antibodies");
  abs.replaceChildren();
  for (const [ref, ab] of Object.entries(state.antibodies || {})) {
    abs.append(el("li", {}, el("strong", { text: ref }), " ",
      chip(String(ab.state).toLowerCase(), String(ab.state).toLowerCase()),
      ` protects ${(ab.hosts_protected || []).join(", ") || "—"}`));
  }
  if (!abs.childElementCount) abs.append(empty("No antibodies."));
}

// ---- "Sabotage it": the form is generated from the API's own parameter schemas ------------

function fieldFor(name, schema, tools) {
  const label = el("label", { text: schema.title || name });
  let input;
  if (name === "tool") {
    input = el("select", { name, required: true },
      ...tools.map((t) => el("option", { value: t, text: t })));
  } else if (Array.isArray(schema.enum)) {
    input = el("select", { name, required: true },
      ...schema.enum.map((v) => el("option", { value: v, text: v })));
  } else {
    const integer = schema.type === "integer";
    input = el("input", { name, type: "number", required: true, min: schema.minimum,
      max: schema.maximum, step: integer ? 1 : 0.01, value: schema.minimum,
      "data-type": schema.type });
  }
  label.append(input);
  return label;
}

function renderForm(state) {
  const arena = state.arena || {};
  const hostSel = document.getElementById("f-host");
  const classSel = document.getElementById("f-class");
  const tools = arena.tools || {};
  const classes = arena.classes || {};
  if (!hostSel.options.length) {
    for (const host of Object.keys(tools)) hostSel.append(el("option", { value: host, text: host }));
    for (const cls of Object.keys(classes)) classSel.append(el("option", { value: cls, text: cls }));
    hostSel.addEventListener("change", () => renderParams(current));
    classSel.addEventListener("change", () => renderParams(current));
    renderParams(state);
  }
  const left = Math.max(0, (arena.daily_cap ?? 0) - (arena.accepted_today ?? 0));
  setText("quota", state.open
    ? `${left} of ${arena.daily_cap} requests left today · one per ${arena.window_minutes} min`
    : "The Arena is closed until the autonomy window opens; requests answer 503.");
  document.getElementById("f-submit").disabled = !state.open || left === 0;
}

function renderParams(state) {
  const arena = (state && state.arena) || {};
  const host = document.getElementById("f-host").value;
  const cls = document.getElementById("f-class").value;
  const schema = (arena.classes || {})[cls] || { properties: {} };
  const root = document.getElementById("f-params");
  root.replaceChildren(...Object.entries(schema.properties || {})
    .map(([name, prop]) => fieldFor(name, prop, (arena.tools || {})[host] || [])));
}

function readForm() {
  const params = {};
  for (const input of document.querySelectorAll("#f-params select, #f-params input")) {
    const type = input.getAttribute("data-type");
    params[input.name] = type === "integer" ? parseInt(input.value, 10)
      : type === "number" ? parseFloat(input.value) : input.value;
  }
  return { host: document.getElementById("f-host").value,
    class: document.getElementById("f-class").value, params };
}

async function submit(event) {
  event.preventDefault();
  const out = document.getElementById("answer");
  const button = document.getElementById("f-submit");
  button.disabled = true;
  try {
    const response = await fetch(SABOTAGE_URL, { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(readForm()) });
    const body = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
    out.className = `answer ${response.ok ? "ok" : "bad"}`;
    out.textContent = response.ok
      ? `Sealed ${body.id} (${body.class} on ${body.host}).\ncommitment ${body.c}\n` +
        `delivered no sooner than ${body.not_before}`
      : `${response.status}: ${body.error || "refused"}`;
    out.hidden = false;
  } catch (error) {
    out.className = "answer bad";
    out.textContent = `The request did not reach the Arena: ${error}`;
    out.hidden = false;
  } finally {
    await refresh();
  }
}

async function refresh() {
  const banner = document.getElementById("error");
  try {
    const response = await fetch(STATE_URL, { cache: "no-store" });
    if (!response.ok) throw new Error(`GET ${STATE_URL}: HTTP ${response.status}`);
    current = await response.json();
    banner.hidden = true;
    renderHead(current);
    renderFleet(current);
    renderIncidents(current);
    renderImmunity(current);
    renderCurve(current);
    renderLedger(current);
    renderForm(current);
  } catch (error) {
    banner.textContent = `The board could not read the Arena state: ${error.message || error}`;
    banner.hidden = false;
  }
}

document.getElementById("sabotage").addEventListener("submit", submit);
refresh();
setInterval(refresh, REFRESH_MS);
