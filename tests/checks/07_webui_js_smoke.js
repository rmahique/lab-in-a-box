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

// -- buildMermaidDiagram: architecture preview diagram --------------------
check("no nodes -> nothing to draw", sandbox.buildMermaidDiagram({}) === null);

{
  // A clustered node, a standalone node, cluster-level and node-level
  // addons, and an `existing` (Phase 5 pre-provisioned host) node — one
  // definition exercising every branch at once.
  const lab = {
    nodes: {
      "node1.mydemo.lab": { myip: "192.168.88.101", kcluster: "clu1" },
      "node2.mydemo.lab": { myip: "192.168.88.102", existing: true, addons: ["mariadb"] },
    },
    kclusters: {
      clu1: { clu_type: "rke2", addons: ["rancher", "longhorn"] },
    },
  };
  const def = sandbox.buildMermaidDiagram(lab);
  check("diagram starts with a valid mermaid graph declaration", def.startsWith("graph TB"));
  check("clustered node renders inside its kcluster's subgraph",
    /subgraph n_clu_clu1\["clu1 \(rke2\)"\][\s\S]*n_node1_mydemo_lab\["node1\.mydemo\.lab<br\/>192\.168\.88\.101"\][\s\S]*end/.test(def));
  check("standalone node (no kcluster) renders outside any subgraph",
    def.includes('n_node2_mydemo_lab["node2.mydemo.lab<br/>192.168.88.102"]')
    && !new RegExp("subgraph[\\s\\S]*n_node2_mydemo_lab[\\s\\S]*end").test(def.split("subgraph")[1] || ""));
  check("cluster-level addons render as linked boxes off the cluster",
    def.includes('(["rancher"])') && def.includes('(["longhorn"])'));
  check("node-level addons render as linked boxes off that node",
    def.includes('(["mariadb"])') && / -\.-> n_node2_mydemo_lab_addon_0_mariadb/.test(def));
  check("an `existing` node gets the dashed-border class applied",
    def.includes("classDef existingNode") && / class n_node2_mydemo_lab existingNode;/.test(def));
  check("a node that is NOT `existing` is never included in the existingNode class list",
    !new RegExp("class [^;]*n_node1_mydemo_lab[^;]* existingNode;").test(def));
}

{
  // Quotes/newlines in a hostname would break mermaid's own quoted-label
  // syntax outright — confirm they're neutralized rather than passed through.
  const def = sandbox.buildMermaidDiagram({ nodes: { 'weird"host\nname': { myip: "10.0.0.1" } } });
  check("quotes and newlines in a node name are neutralized, not passed through raw",
    !def.includes('"weird"host') && !/\n.*name/.test(def.split("\n").find((l) => l.includes("weird"))));
}

if (failures) {
  console.error(failures + " check(s) failed");
  process.exit(1);
}
console.log("all app.js smoke checks passed");
