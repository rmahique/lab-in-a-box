"use strict";
/*
 * lab-builder frontend.
 *
 * The renderer knows nothing about any specific script or field. It walks a
 * schema tree (Option B): any object with `name`+`type` is a FIELD, wherever it
 * lives; `fields`/`sections` are structural wrappers that don't appear in output;
 * any other named object is a GROUP that becomes an output key. So new fields,
 * new components and new nested sections all render with zero code changes.
 */
const API = "api";

// ---- schema vocabulary (the only fixed convention) -------------------------
const STRUCTURAL = new Set(["fields", "sections"]);        // containers, not output keys
const META_SCALAR = new Set(["schema_version", "addon", "section",
  "component", "description", "repeatable", "key_label", "name", "type",
  "required", "default"]);

const isField = (o) => o && typeof o === "object" && !Array.isArray(o) &&
  typeof o.name === "string" && typeof o.type === "string";

// ---- state -----------------------------------------------------------------
const state = { components: [], current: null, lab: {} };

// ---- tiny DOM helpers ------------------------------------------------------
const $ = (s, r = document) => r.querySelector(s);
function el(tag, cls, txt) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 2600);
}

// ---- API -------------------------------------------------------------------
async function apiGet(action, params = {}) {
  const q = new URLSearchParams({ action, ...params });
  const r = await fetch(`${API}?${q}`);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
async function apiPost(action, payload) {
  const r = await fetch(`${API}?action=${action}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

// ---- catalogue -------------------------------------------------------------
async function loadComponents() {
  const data = await apiGet("components");
  state.components = data.components;
  $("#countNum").textContent = data.count;
  $("#srcNote").textContent = `read from ${data.scripts_dir}`;
  renderCatalogue("");
}
function renderCatalogue(filter) {
  const ul = $("#componentList");
  ul.innerHTML = "";
  const f = filter.toLowerCase();
  // pinned base-topology entry (common/nodes/kclusters)
  if (!f || "lab topology common nodes kclusters".includes(f)) {
    const li = el("li", "pinned");
    li.appendChild(el("div", "ci-title", "▚ Lab topology"));
    li.appendChild(el("div", "ci-desc", "common · nodes · kclusters"));
    li.addEventListener("click", () => selectBase(li));
    ul.appendChild(li);
  }
  state.components
    .filter((c) => !f || c.title.toLowerCase().includes(f) || (c.description || "").toLowerCase().includes(f))
    .forEach((c) => {
      const li = el("li");
      li.dataset.name = c.name;
      li.appendChild(el("div", "ci-title", c.title));
      if (c.description) li.appendChild(el("div", "ci-desc", c.description));
      li.appendChild(el("div", "ci-meta", `${c.field_count} option${c.field_count === 1 ? "" : "s"}`));
      li.addEventListener("click", () => selectComponent(c.name, li));
      ul.appendChild(li);
    });
}

// ---- schema form -----------------------------------------------------------
async function selectComponent(name, li) {
  document.querySelectorAll("#componentList li").forEach((x) => x.classList.remove("active"));
  if (li) li.classList.add("active");
  const schema = await apiGet("schema", { name });
  state.current = schema;
  $("#formTitle").textContent = schema.section || schema.component || name;
  $("#formDesc").textContent = schema.description || "";
  $("#formEmpty").hidden = true;
  $("#addBtn").disabled = false;
  const form = $("#schemaForm");
  form.innerHTML = "";
  walk(schema, form, []);
}

async function selectBase(li) {
  document.querySelectorAll("#componentList li").forEach((x) => x.classList.remove("active"));
  if (li) li.classList.add("active");
  const schema = await apiGet("base");
  state.current = schema;
  $("#formTitle").textContent = "Lab topology";
  $("#formDesc").textContent = schema.description || "";
  $("#formEmpty").hidden = true;
  $("#addBtn").disabled = false;
  const form = $("#schemaForm");
  form.innerHTML = "";
  walk(schema, form, []);
}

/* Recursively render `node` into `parent`, tracking the output-key path. */
function walk(node, parent, outPath) {
  if (Array.isArray(node)) { node.forEach((n) => walk(n, parent, outPath)); return; }
  if (!node || typeof node !== "object") return;
  if (isField(node)) { parent.appendChild(fieldRow(node, outPath)); return; }

  for (const [key, val] of Object.entries(node)) {
    if (!val || typeof val !== "object") continue;      // skip scalar meta
    if (STRUCTURAL.has(key)) { walk(val, parent, outPath); continue; }  // wrapper: no output key
    if (val.repeatable === true) {                                      // keyed map of instances
      parent.appendChild(repeatGroup(key, val, outPath.concat(key)));
      continue;
    }
    // named group -> heading + new output segment
    const box = el("div", "group");
    const head = el("div", "group-head", key);
    if (typeof val.description === "string") head.appendChild(el("span", "gh-desc", val.description));
    box.appendChild(head);
    const body = el("div", "group-body");
    box.appendChild(body);
    parent.appendChild(box);
    walk(val, body, outPath.concat(key));
  }
}

function fieldRow(field, outPath, repeatMode) {
  const row = el("div", "field");
  const label = el("label");
  label.appendChild(document.createTextNode(field.name));
  if (field.required) label.appendChild(el("span", "req", "*"));
  label.appendChild(el("span", "type-tag", field.type));
  row.appendChild(label);
  if (field.description) row.appendChild(el("div", "desc", field.description));

  const def = field.default != null ? String(field.default) : "";
  let input;
  switch (field.type) {
    case "boolean":
      input = el("select");
      [["", "— unset —"], ["true", "true"], ["false", "false"]].forEach(([v, t]) => {
        const o = el("option", null, t); o.value = v; input.appendChild(o);
      });
      input.value = ["true", "false"].includes(def) ? def : "";
      break;
    case "integer": case "port":
      input = el("input"); input.type = "number"; input.placeholder = def; break;
    case "password":
      input = el("input"); input.type = "password"; input.placeholder = def ? "default set" : ""; break;
    case "array":
      input = el("input"); input.type = "text";
      input.placeholder = def || "comma,separated,values"; break;
    default:
      input = el("input"); input.type = "text"; input.placeholder = def;
  }
  if (repeatMode) input.dataset.field = field.name;         // serialized per-instance
  else input.dataset.outpath = JSON.stringify(outPath.concat(field.name));
  input.dataset.ftype = field.type;
  row.appendChild(input);
  return row;
}

/* Gather every field under a node, wherever it lives (container-agnostic). */
function collectFields(node) {
  let out = [];
  if (Array.isArray(node)) node.forEach((n) => { out = out.concat(collectFields(n)); });
  else if (node && typeof node === "object") {
    if (isField(node)) return [node];
    for (const v of Object.values(node)) out = out.concat(collectFields(v));
  }
  return out;
}

/* A repeatable group -> a keyed map of instances (e.g. nodes, kclusters). */
function repeatGroup(name, obj, outPath) {
  const box = el("div", "group repeat");
  box.dataset.repeatpath = JSON.stringify(outPath);
  const head = el("div", "group-head", name);
  if (typeof obj.description === "string") head.appendChild(el("span", "gh-desc", obj.description));
  box.appendChild(head);
  const body = el("div", "group-body");
  const list = el("div", "instances");
  body.appendChild(list);
  const fields = collectFields(obj);
  const keyLabel = obj.key_label || "key";
  const add = el("button", "btn add-inst", "+ add " + name);
  add.type = "button";
  add.addEventListener("click", () => list.appendChild(instanceBox(fields, keyLabel)));
  body.appendChild(add);
  box.appendChild(body);
  list.appendChild(instanceBox(fields, keyLabel));   // start with one
  return box;
}

function instanceBox(fields, keyLabel) {
  const inst = el("div", "instance");
  const keyRow = el("div", "field");
  const kl = el("label");
  kl.appendChild(document.createTextNode(keyLabel));
  kl.appendChild(el("span", "req", "*"));
  keyRow.appendChild(kl);
  const ki = el("input", "inst-key");
  ki.type = "text"; ki.placeholder = keyLabel;
  keyRow.appendChild(ki);
  inst.appendChild(keyRow);
  fields.forEach((f) => inst.appendChild(fieldRow(f, [], true)));
  const rm = el("button", "btn rm-inst", "remove");
  rm.type = "button";
  rm.addEventListener("click", () => inst.remove());
  inst.appendChild(rm);
  return inst;
}

// ---- serialize form -> fragment -------------------------------------------
function readWidget(elm) {
  const raw = elm.value.trim();
  if (raw === "") return undefined;
  switch (elm.dataset.ftype) {
    case "boolean": return raw === "true";
    case "integer": case "port": { const n = Number(raw); return Number.isNaN(n) ? raw : n; }
    case "array": return raw.split(",").map((s) => s.trim()).filter(Boolean);
    default: return raw;
  }
}
function setPath(obj, path, val) {
  let o = obj;
  for (let i = 0; i < path.length - 1; i++) o = (o[path[i]] ??= {});
  o[path[path.length - 1]] = val;
}
function serializeForm() {
  const frag = {};
  // plain fields (skip those inside repeatable instances)
  $("#schemaForm").querySelectorAll("[data-outpath]").forEach((elm) => {
    if (elm.closest(".instance")) return;
    const val = readWidget(elm);
    if (val === undefined || (Array.isArray(val) && !val.length)) return;
    setPath(frag, JSON.parse(elm.dataset.outpath), val);
  });
  // repeatable groups -> { groupKey: { instanceKey: {…} } }
  $("#schemaForm").querySelectorAll(".repeat").forEach((rep) => {
    const map = {};
    rep.querySelectorAll(".instance").forEach((inst) => {
      const key = inst.querySelector(".inst-key").value.trim();
      if (!key) return;
      const obj = {};
      inst.querySelectorAll("[data-field]").forEach((elm) => {
        const val = readWidget(elm);
        if (val === undefined || (Array.isArray(val) && !val.length)) return;
        obj[elm.dataset.field] = val;
      });
      map[key] = obj;
    });
    if (Object.keys(map).length) setPath(frag, JSON.parse(rep.dataset.repeatpath), map);
  });
  return frag;
}

// ---- assemble lab ----------------------------------------------------------
function addToLab() {
  const s = state.current;
  if (!s) return;
  const frag = serializeForm();
  if (s.section && Array.isArray(s.fields)) {
    // flat addon schema -> nest under its section name
    state.lab[s.section] = frag;
  } else {
    Object.assign(state.lab, frag);           // structured schema already carries its keys
  }
  refreshLab();
  toast(`Added “${s.section || s.component}” to lab.json`);
}
function refreshLab() {
  $("#labPreview").textContent = JSON.stringify(state.lab, null, 2);
  const n = Object.keys(state.lab).length;
  $("#sectionCount").textContent = `${n} section${n === 1 ? "" : "s"}`;
  $("#validateResult").hidden = true;
}

// ---- lab actions -----------------------------------------------------------
async function validateLab() {
  const box = $("#validateResult");
  try {
    const r = await apiPost("validate", { config: state.lab });
    box.hidden = false;
    box.className = "validate-result " + (r.ok ? "ok" : "err");
    box.textContent = r.ok ? "✓ Valid lab definition." : (r.output || "Validation failed.");
  } catch (e) { toast("Validate error: " + e.message); }
}
function downloadLab() {
  const name = ($("#labName").value.trim() || "lab").replace(/[^A-Za-z0-9._-]/g, "_");
  const blob = new Blob([JSON.stringify(state.lab, null, 2) + "\n"], { type: "application/json" });
  const a = el("a"); a.href = URL.createObjectURL(blob);
  a.download = name.endsWith(".json") ? name : name + ".json";
  a.click(); URL.revokeObjectURL(a.href);
}
async function saveLab() {
  try {
    const r = await apiPost("save", { filename: $("#labName").value.trim() || "lab", config: state.lab });
    toast("Saved on server: " + r.saved);
  } catch (e) { toast("Save error: " + e.message); }
}

// ---- wire up ---------------------------------------------------------------
window.addEventListener("DOMContentLoaded", () => {
  $("#filter").addEventListener("input", (e) => renderCatalogue(e.target.value));
  $("#addBtn").addEventListener("click", addToLab);
  $("#validateBtn").addEventListener("click", validateLab);
  $("#downloadBtn").addEventListener("click", downloadLab);
  $("#saveBtn").addEventListener("click", saveLab);
  loadComponents().catch((e) => { $("#countNum").textContent = "!"; toast("Load error: " + e.message); });
});
