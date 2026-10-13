// Drives a card's real <script> (tools/cards.py) in Node with a minimal DOM stub, playing the host side
// of the ext-apps bridge. Used by test_cards.py (skipped when `node` is not installed).
//
// Usage: node card_harness.js <card.html> <tool-result.json>
// Prints one JSON object: the rendered tree, the messages the card posted and what happened when the
// test's scripted interactions ran (see `steps` below).
"use strict";
const fs = require("fs");
const vm = require("vm");

const [htmlPath, resultPath] = process.argv.slice(2);
const html = fs.readFileSync(htmlPath, "utf8");
const toolResult = JSON.parse(fs.readFileSync(resultPath, "utf8"));
const script = html.split("<script>")[1].split("</script>")[0];
const variant = /data-variant="([a-z]+)"/.exec(html)[1];

const tick = () => new Promise((resolve) => setImmediate(resolve));

class Node {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.listeners = {}; this.className = ""; this.textContent = "";
    this.disabled = false; this.value = ""; this.id = ""; this.htmlFor = ""; this.dataset = {};
  }
  get childNodes() { return this.children; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  // Handlers may start work that waits for a host reply that never comes: fire, then let it run.
  async fire(type) { (this.listeners[type] || []).forEach((fn) => fn({ target: this })); await tick(); }
  getBoundingClientRect() { return { width: 300, height: 200 }; }
}

const root = new Node("main");
const body = new Node("body"); body.dataset.variant = variant;
const docEl = new Node("html");
const posted = [];
const windowListeners = [];
const parent = { postMessage: (msg) => posted.push(msg) };
const window = { parent, addEventListener: (type, fn) => { if (type === "message") windowListeners.push(fn); } };
const document = {
  title: "Card", body, documentElement: docEl,
  createElement: (tag) => new Node(tag),
  getElementById: (id) => (id === "card" ? root : null),
};
function deliver(data) { windowListeners.forEach((fn) => fn({ source: parent, data })); }

function find(node, pred, out = []) {
  if (pred(node)) out.push(node);
  node.children.forEach((c) => find(c, pred, out));
  return out;
}
function tree(node) {
  return { tag: node.tagName, cls: node.className || undefined, text: node.textContent || undefined,
           children: node.children.length ? node.children.map(tree) : undefined };
}

(async () => {
  vm.runInNewContext(script, { window, document, Object, String, Map, Promise, console });
  const init = posted.find((m) => m.method === "ui/initialize");
  deliver({ jsonrpc: "2.0", id: init.id, result: {} });
  await tick();
  deliver({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: toolResult });
  await tick();

  const report = { variant, tree: tree(root), steps: {} };
  const fields = find(root, (n) => n.tagName === "textarea");
  const buttons = find(root, (n) => n.tagName === "button");
  report.textareas = fields.map((f) => ({ id: f.id, maxLength: f.maxLength === undefined ? null : f.maxLength }));
  report.labels = find(root, (n) => n.tagName === "label").map((l) => ({ text: l.textContent, htmlFor: l.htmlFor }));
  report.buttons = buttons.map((b) => ({ text: b.textContent, disabled: b.disabled }));

  if (fields.length) {
    const field = fields[0];
    const sendButton = buttons.find((b) => find(root, (n) => n.className === "reply" && n.children.includes(b)).length);
    const before = posted.length;
    await sendButton.fire("click"); // blank: nothing may be sent
    report.steps.blankClickSent = posted.slice(before).filter((m) => m.method === "tools/call");
    field.value = "   ";
    await field.fire("input");
    report.steps.disabledWhenBlank = sendButton.disabled;
    field.value = "  Yes, Sunday is firm.  ";
    await field.fire("input");
    report.steps.disabledWithText = sendButton.disabled;
    await sendButton.fire("click");
    report.steps.sent = posted.slice(before).filter((m) => m.method === "tools/call").map((m) => m.params);
    report.steps.disabledWhileSending = sendButton.disabled && field.disabled;
  }
  const plain = buttons.filter((b) => !find(root, (n) => n.className === "reply" && n.children.includes(b)).length);
  if (plain.length) {
    const before = posted.length;
    await plain[0].fire("click");
    report.steps.plainSent = posted.slice(before).filter((m) => m.method === "tools/call").map((m) => m.params);
  }
  process.stdout.write(JSON.stringify(report));
})().catch((err) => { process.stderr.write(String(err && err.stack || err)); process.exit(1); });
