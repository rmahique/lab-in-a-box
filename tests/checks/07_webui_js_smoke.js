// DOM-free functional smoke test for webui/htdocs/app.js's pure logic
// (validateFieldValue, applyFieldDefault) — loaded via node's vm module with
// minimal document/window stubs, no jsdom/npm dependency. Run from
// 07_webui_js.sh, in its own container — see tests/run_tests.sh.
"use strict";
const fs = require("fs");
const vm = require("vm");

const src = fs.readFileSync("webui/htdocs/app.js", "utf8");
const sandbox = {
  document: { querySelectorAll: () => [] },
  window: { addEventListener: () => {} },
  console,
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "app.js" });

let failures = 0;
function check(desc, cond) {
  if (!cond) { failures++; console.error("FAIL: " + desc); }
}

// -- validateFieldValue -------------------------------------------------
check("empty value never flagged",
  sandbox.validateFieldValue({ type: "integer" }, "") === null);
check("non-numeric integer rejected",
  sandbox.validateFieldValue({ type: "integer" }, "abc") !== null);
check("integer below min rejected",
  sandbox.validateFieldValue({ type: "integer", min: 256 }, "10") !== null);
check("integer within range accepted",
  sandbox.validateFieldValue({ type: "integer", min: 1, max: 64 }, "6") === null);
check("port above 65535 rejected",
  sandbox.validateFieldValue({ type: "port" }, "70000") !== null);
check("pattern mismatch rejected",
  sandbox.validateFieldValue({ type: "string", pattern: "^\\d+\\.\\d+\\.\\d+\\.\\d+$" }, "not-an-ip") !== null);
check("pattern match accepted",
  sandbox.validateFieldValue({ type: "string", pattern: "^\\d+\\.\\d+\\.\\d+\\.\\d+$" }, "10.0.0.1") === null);

// -- applyFieldDefault: plain input placeholder --------------------------
{
  const field = { name: "VM_MEM", type: "integer", default: 24576 };
  const input = { _field: field, tagName: "INPUT", dataset: {} };
  sandbox.applyFieldDefault(input, undefined);
  check("plain input: schema default used as placeholder when no live value",
    input.placeholder === "24576");
  sandbox.applyFieldDefault(input, "8192");
  check("plain input: live (common) value overrides schema default",
    input.placeholder === "8192");
}

// -- applyFieldDefault: empty-string default must not read as "no default" --
{
  const field = { name: "config_method", type: "string", default: "" };
  const input = {
    _field: field, tagName: "SELECT", dataset: {}, options: [
      { value: "", dataset: { label: "(default) Ignition+Combustion", fixedLabel: "" }, className: "" },
      { value: "cloud-init", dataset: { label: "cloud-init", fixedLabel: "" }, className: "" },
    ],
  };
  sandbox.applyFieldDefault(input, undefined);
  check("select: empty-string schema default still gets marked as the default option",
    input.options[0].className === "opt-default" && input.options[1].className === "");
}

// -- applyFieldDefault: select value only synced before user interaction --
{
  const field = { name: "backend", type: "string", default: "libvirt" };
  const opts = [
    { value: "", dataset: { blank: "1" }, className: "" },
    { value: "libvirt", dataset: { label: "libvirt", fixedLabel: "" }, className: "" },
  ];
  const input = { _field: field, tagName: "SELECT", dataset: {}, options: opts, value: "" };
  sandbox.applyFieldDefault(input, undefined);
  check("select: value synced to default before user touches it",
    input.value === "libvirt");
  input.dataset.userTouched = "1";
  input.value = "";
  sandbox.applyFieldDefault(input, undefined);
  check("select: value left alone once user has touched it",
    input.value === "");
}

// -- applyFieldDefault: repeat-group select never auto-fills its value ------
// Regression test for a real bug reported live 2026-09-01: INSTALL_RKE2_TYPE
// (a per-node/repeatable field with a schema default) always showed up in
// the saved lab.json for every node, even ones whose role was never
// actually chosen — because the select's own .value, not just its
// placeholder-equivalent labeling, was being set to the default.
{
  const field = { name: "INSTALL_RKE2_TYPE", type: "string", default: "server" };
  const opts = [
    { value: "", dataset: { blank: "1" }, className: "" },
    { value: "server", dataset: { label: "server", fixedLabel: "" }, className: "" },
    { value: "agent", dataset: { label: "agent", fixedLabel: "" }, className: "" },
  ];
  const input = { _field: field, tagName: "SELECT", dataset: { repeat: "1" }, options: opts, value: "" };
  sandbox.applyFieldDefault(input, undefined);
  check("select: repeat-group field's value stays unset even though it has a schema default",
    input.value === "");
  check("select: repeat-group field still gets its default option labeled/marked",
    opts[1].className === "opt-default");
  // Same field shape but NOT in a repeat group (dataset.repeat unset) —
  // existing common/flat-field behavior must be unchanged.
  const flatInput = { _field: field, tagName: "SELECT", dataset: {}, options: opts.map((o) => ({ ...o })), value: "" };
  sandbox.applyFieldDefault(flatInput, undefined);
  check("select: a flat (non-repeat) field with the same default still auto-fills its value",
    flatInput.value === "server");
}

if (failures) {
  console.error(failures + " check(s) failed");
  process.exit(1);
}
console.log("all app.js smoke checks passed");
