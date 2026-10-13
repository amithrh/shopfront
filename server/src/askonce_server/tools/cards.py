"""MCP Apps (`ui://`) cards. Optional enhancement: every tool's text and structuredContent stand alone.

Four static, self-contained HTML resources (no external fetches, so the default restrictive CSP works).
Each renders `structuredContent.card` (a `schemas.Card`) when the host delivers
`ui/notifications/tool-result` over postMessage (ext-apps protocol), and shows `speak` as a caption.
Tenant data arrives only through the tool result, never baked into HTML (one URI per card type, shared
by every tenant). The host joins a tool's `_meta.ui.resourceUri` from tools/list with its results.

Register on the `Apps` instance BEFORE constructing `MCPServer(extensions=[apps])`: the SDK reads the
extension once in `__init__` and silently ignores later registrations.

The shell speaks the View side of the ext-apps bridge (JSON-RPC 2.0 over `postMessage`):
  * `ui/initialize` request, then `ui/notifications/initialized`;
  * renders `ui/notifications/tool-result` (and shows the arguments of `ui/notifications/tool-input`
    as a pending state); answers `ui/resource-teardown` with an empty result;
  * reports its height with `ui/notifications/size-changed`;
  * card actions send `tools/call` through the host (which proxies them to this server when it grants
    `serverTools`) and render the returned result; an action with an `input` (the task card's Answer)
    first collects that text in a textarea and sends it as `arguments[input.key]`; citations open
    through `ui/open-link`.
All tenant text is inserted with `textContent` only (never innerHTML), so card data cannot inject markup.
Variants differ in layout: business (facts list), items (name/price rows), commitment (status pill,
details, alternatives) and task (progress state and final outcome).
"""

from __future__ import annotations

from typing import Literal

from mcp.server.apps import Apps

CardVariant = Literal["business", "items", "commitment", "task"]

BUSINESS_CARD = "ui://askonce/business-card.html"
ITEMS_CARD = "ui://askonce/items-card.html"
COMMITMENT_CARD = "ui://askonce/commitment-card.html"
TASK_CARD = "ui://askonce/task-card.html"

CARD_URIS: dict[CardVariant, str] = {
    "business": BUSINESS_CARD,
    "items": ITEMS_CARD,
    "commitment": COMMITMENT_CARD,
    "task": TASK_CARD,
}

TITLES: dict[CardVariant, str] = {
    "business": "Business",
    "items": "Menu",
    "commitment": "Your request",
    "task": "Waiting on the owner",
}

_STYLE = """
:root { color-scheme: light dark; --fg: #1d1d1f; --muted: #6e6e73; --bg: #ffffff; --line: #e5e5ea;
        --accent: #0a66c2; --ok: #1a7f37; --warn: #9a6700; --bad: #c62828; --chip: #f2f2f7;
        font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; font-size: 15px; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #f5f5f7; --muted: #a1a1a6; --bg: #1c1c1e; --line: #38383a; --accent: #4ea1ff;
          --ok: #4ac26b; --warn: #d29922; --bad: #ff6b6b; --chip: #2c2c2e; }
}
:root[data-theme="dark"] { --fg: #f5f5f7; --muted: #a1a1a6; --bg: #1c1c1e; --line: #38383a;
          --accent: #4ea1ff; --ok: #4ac26b; --warn: #d29922; --bad: #ff6b6b; --chip: #2c2c2e; }
:root[data-theme="light"] { --fg: #1d1d1f; --muted: #6e6e73; --bg: #ffffff; --line: #e5e5ea;
          --accent: #0a66c2; --ok: #1a7f37; --warn: #9a6700; --bad: #c62828; --chip: #f2f2f7; }
* { box-sizing: border-box; }
body { margin: 0; padding: 14px 16px; color: var(--fg); background: var(--bg); line-height: 1.4; }
.title { font-weight: 650; font-size: 1.1rem; margin: 0; }
.subtitle { color: var(--muted); font-size: .9rem; margin-top: 2px; }
.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.badges { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.badge { border-radius: 999px; padding: 2px 10px; font-size: .78rem; background: var(--chip); }
.pill { border-radius: 999px; padding: 3px 12px; font-size: .8rem; font-weight: 600; white-space: nowrap;
        border: 1px solid currentColor; }
.pill.ok { color: var(--ok); } .pill.warn { color: var(--warn); } .pill.bad { color: var(--bad); }
.pill.info { color: var(--accent); }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; margin: 12px 0 0; }
dt { color: var(--muted); } dd { margin: 0; overflow-wrap: anywhere; }
.rows { list-style: none; padding: 0; margin: 12px 0 0; }
.rows li { display: flex; justify-content: space-between; gap: 12px; padding: 7px 0;
           border-bottom: 1px solid var(--line); }
.rows li:last-child { border-bottom: 0; }
.rows .value { color: var(--muted); white-space: nowrap; }
.speak { margin-top: 12px; padding: 10px 12px; border-radius: 10px; background: var(--chip); }
.progress { height: 4px; border-radius: 2px; margin-top: 12px; background: var(--line); overflow: hidden; }
.progress > span { display: block; height: 100%; width: 40%; background: var(--accent);
                   animation: slide 1.4s ease-in-out infinite; }
@keyframes slide { 0% { margin-left: -40%; } 100% { margin-left: 100%; } }
@media (prefers-reduced-motion: reduce) { .progress > span { animation: none; width: 100%; } }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
button { font: inherit; border-radius: 8px; border: 1px solid var(--line); background: var(--bg);
         color: var(--accent); padding: 6px 12px; cursor: pointer; }
button:disabled { opacity: .5; cursor: default; }
.cites { margin-top: 12px; font-size: .8rem; color: var(--muted); }
.cite { display: block; margin-top: 4px; }
.cite button { padding: 0; border: 0; background: none; font-size: .8rem; text-decoration: underline; }
.cite q { display: block; margin-top: 2px; font-style: italic; }
.handoff { margin-top: 12px; padding: 10px 12px; border-left: 3px solid var(--warn); background: var(--chip); }
.reply { display: grid; gap: 6px; margin-top: 12px; }
.reply label { color: var(--muted); font-size: .9rem; }
.reply textarea { font: inherit; width: 100%; min-height: 4.5em; resize: vertical; padding: 6px 8px;
                  border-radius: 8px; border: 1px solid var(--line); background: var(--bg); color: var(--fg); }
.reply button { justify-self: start; }
.error { color: var(--bad); margin-top: 8px; }
"""

_SCRIPT = """
(() => {
  "use strict";
  const VARIANT = document.body.dataset.variant;
  const STATUS_TONE = { confirmed: "ok", approved: "ok", answered: "ok", quoted: "ok",
    pending_owner: "info", info_requested: "warn", counter_offer: "warn", suggested_time: "warn",
    needs_info: "warn", declined: "bad", cancelled: "bad", expired: "bad", unavailable: "bad",
    not_offered: "bad", not_confirmed: "warn", working: "info", input_required: "warn",
    completed: "ok", failed: "bad" };
  let nextId = 1;
  let nextField = 1;
  const pending = new Map();

  function post(message) { window.parent.postMessage(message, "*"); }
  function request(method, params) {
    const id = nextId++;
    post({ jsonrpc: "2.0", id, method, params });
    return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  }
  function notify(method, params) { post({ jsonrpc: "2.0", method, params }); }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function label(status) { return String(status || "").replace(/_/g, " "); }

  function renderHead(root, card, status) {
    const head = el("div", "head");
    const left = el("div");
    left.append(el("h1", "title", card.title || document.title));
    if (card.subtitle) left.append(el("div", "subtitle", card.subtitle));
    head.append(left);
    if (status) head.append(el("span", "pill " + (STATUS_TONE[status] || "info"), label(status)));
    root.append(head);
    if ((card.badges || []).length) {
      const badges = el("div", "badges");
      card.badges.forEach((b) => badges.append(el("span", "badge", b)));
      root.append(badges);
    }
  }
  function renderLines(root, lines, asRows) {
    if (!lines || !lines.length) return;
    if (asRows) {
      const ul = el("ul", "rows");
      lines.forEach((l) => { const li = el("li"); li.append(el("span", null, l.label));
        li.append(el("span", "value", l.value)); ul.append(li); });
      root.append(ul);
      return;
    }
    const dl = el("dl");
    lines.forEach((l) => { dl.append(el("dt", null, l.label)); dl.append(el("dd", null, l.value)); });
    root.append(dl);
  }
  async function runAction(root, a, args, controls) {
    controls.forEach((c) => { c.disabled = true; });
    try { render(await request("tools/call", { name: a.tool, arguments: args })); }
    catch (err) { const e = el("div", "error", "That action isn't available here."); root.append(e); }
    finally { controls.forEach((c) => { c.disabled = false; }); }
  }
  // An action with `input` needs text from the user first (e.g. the answer to the owner's question):
  // a labelled textarea plus a send button, enabled once the text is non-blank. The tool is called
  // with the action's arguments plus { [input.key]: text }.
  function inputAction(root, a) {
    const box = el("div", "reply");
    const id = "reply-" + (nextField++);
    const caption = el("label", null, a.input.label || a.label);
    caption.htmlFor = id;
    const field = el("textarea");
    field.id = id;
    if (a.input.max_length) field.maxLength = a.input.max_length;
    const send = el("button", null, a.label);
    send.disabled = true;
    field.addEventListener("input", () => { send.disabled = !field.value.trim(); });
    send.addEventListener("click", () => {
      const text = field.value.trim();
      if (!text) return;
      const args = Object.assign({}, a.arguments || {});
      args[a.input.key] = text;
      runAction(root, a, args, [send, field]);
    });
    box.append(caption, field, send);
    return box;
  }
  function renderActions(root, actions) {
    if (!actions || !actions.length) return;
    const box = el("div", "actions");
    actions.forEach((a) => {
      if (a.input && a.input.key) { root.append(inputAction(root, a)); return; }
      const b = el("button", null, a.label);
      b.addEventListener("click", () => runAction(root, a, a.arguments || {}, [b]));
      box.append(b);
    });
    if (box.childNodes.length) root.append(box);
  }
  function renderCitations(root, citations) {
    if (!citations || !citations.length) return;
    const box = el("div", "cites");
    box.append(el("div", null, "Sources"));
    citations.forEach((c) => {
      const row = el("span", "cite");
      const name = c.title || c.url;
      if (/^https:\\/\\//.test(c.url || "")) {
        const b = el("button", null, name);
        b.addEventListener("click", () => request("ui/open-link", { url: c.url }).catch(() => {}));
        row.append(b);
      } else { row.append(el("span", null, name)); }
      if (c.quote) row.append(el("q", null, c.quote));
      box.append(row);
    });
    root.append(box);
  }
  function renderAlternatives(root, alternatives) {
    if (!alternatives || !alternatives.length) return;
    root.append(el("div", "subtitle", "Other options"));
    renderLines(root, alternatives.map((s) => ({ label: s.label_spoken, value: s.resource_spoken_name || "" })), true);
  }

  function render(result) {
    const sc = (result && (result.structuredContent || result.structured_content)) || {};
    const card = sc.card || { title: document.title, lines: [] };
    const status = sc.status || sc.task_status || null;
    const root = document.getElementById("card");
    root.replaceChildren();
    renderHead(root, card, VARIANT === "business" || VARIANT === "items" ? null : status);
    renderLines(root, card.lines, VARIANT === "items");
    if (VARIANT === "commitment") renderAlternatives(root, sc.alternatives);
    if (VARIANT === "task" && (status === "working" || status === "pending_owner")) {
      const bar = el("div", "progress"); bar.append(el("span")); root.append(bar);
    }
    if (sc.speak) root.append(el("div", "speak", sc.speak));
    if (sc.handoff) root.append(el("div", "handoff", "You can ask " + sc.handoff.approver_spoken_name + " about this."));
    renderActions(root, card.actions);
    renderCitations(root, card.citations);
    reportSize();
  }
  function renderPending(args) {
    const root = document.getElementById("card");
    root.replaceChildren(el("h1", "title", document.title), el("div", "subtitle", "Working on it..."));
    reportSize();
  }
  function reportSize() {
    const r = document.documentElement.getBoundingClientRect();
    notify("ui/notifications/size-changed", { width: Math.ceil(r.width), height: Math.ceil(r.height) });
  }
  function applyContext(ctx) {
    if (ctx && (ctx.theme === "dark" || ctx.theme === "light")) document.documentElement.dataset.theme = ctx.theme;
  }

  window.addEventListener("message", (ev) => {
    if (ev.source !== window.parent) return;
    const msg = ev.data || {};
    if (msg.jsonrpc !== "2.0") return;
    if (msg.id !== undefined && pending.has(msg.id) && !msg.method) {
      const p = pending.get(msg.id); pending.delete(msg.id);
      if (msg.error) p.reject(msg.error); else p.resolve(msg.result);
      return;
    }
    switch (msg.method) {
      case "ui/notifications/tool-input": renderPending(msg.params && msg.params.arguments); break;
      case "ui/notifications/tool-result": render(msg.params); break;
      case "ui/notifications/host-context-changed": applyContext(msg.params); break;
      case "ui/resource-teardown": if (msg.id !== undefined) post({ jsonrpc: "2.0", id: msg.id, result: {} }); break;
      default: break;
    }
  });

  request("ui/initialize", { appInfo: { name: "askonce-" + VARIANT + "-card", version: "1.0.0" },
                             appCapabilities: {} })
    .then((res) => { applyContext(res && res.hostContext); notify("ui/notifications/initialized", {}); })
    .catch(() => notify("ui/notifications/initialized", {}));
  if (window.ResizeObserver) new ResizeObserver(reportSize).observe(document.body);
})();
"""

_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{style}</style></head>
<body data-variant="{variant}">
  <main id="card" aria-live="polite"><h1 class="title">{title}</h1><div class="subtitle">Loading...</div></main>
  <script>{script}</script>
</body></html>"""


def card_html(variant: CardVariant) -> str:
    """The HTML shell for one card variant (safe: all data is inserted with textContent)."""
    return _TEMPLATE.format(title=TITLES[variant], variant=variant, style=_STYLE, script=_SCRIPT)


def register_cards(apps: Apps) -> None:
    """Register every card resource on `apps` (call before building the MCPServer)."""
    for variant, uri in CARD_URIS.items():
        apps.add_html_resource(
            uri,
            card_html(variant),
            name=f"askonce-{variant}-card",
            title=f"AskOnce {variant} card",
            description=f"Renders the `card` of {variant} tool results.",
            prefers_border=True,
        )
