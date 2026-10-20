"""The owner's phone page: one static HTML file served at the same URLs as the owner link.

The signed links the server mints (`/owner/<business>/inbox?token=...`, and per-request
`/owner/<business>/requests/<id>?token=...`) point at the JSON API. When a *browser* opens one (a GET
whose `Accept` prefers `text/html` and that carries no `Authorization` header), `OwnerPageMiddleware`
answers with this page instead; the page then calls the very same URLs with `fetch` and
`Authorization: Bearer <token>` (JSON). So a link works both for the owner on a phone and for API
clients, and there is still exactly one credential: the link token.

The page holds no tenant data in its HTML (everything arrives through the authenticated API), inserts
all text with `textContent`, keeps the token in `sessionStorage` and strips it from the address bar.
It polls the inbox every few seconds (no SMS/push channel, see `notifier.py`).
"""

from __future__ import annotations

import re

from starlette.types import ASGIApp, Receive, Scope, Send

_PAGE_PATH = re.compile(r"^/owner/[a-z0-9][a-z0-9_-]{0,63}/(inbox|requests/[a-z0-9][a-z0-9_-]{0,63})$")

PAGE_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-type", b"text/html; charset=utf-8"),
    (b"cache-control", b"no-store"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (
        b"content-security-policy",
        b"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
        b"img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
]


def wants_page(scope: Scope) -> bool:
    """A browser navigating to an owner link (not the page's own `fetch` calls)."""
    if scope["type"] != "http" or scope["method"] != "GET" or not _PAGE_PATH.match(scope["path"]):
        return False
    headers = dict(scope["headers"])
    if b"authorization" in headers:
        return False
    accept = headers.get(b"accept", b"").decode("latin-1").lower()
    return "text/html" in accept


class OwnerPageMiddleware:
    """Serve `OWNER_PAGE` for browser navigations to owner links (see module docstring)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.body = OWNER_PAGE.encode("utf-8")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not wants_page(scope):
            await self.app(scope, receive, send)
            return
        headers = [*PAGE_HEADERS, (b"content-length", str(len(self.body)).encode())]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": self.body})


OWNER_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>Requests · AskOnce</title>
<style>
:root {
  color-scheme: light;
  --paper: #fbfaf7; --card: #ffffff; --ink: #1f2430; --muted: #687083; --line: #e4e1da;
  --brand: #2f5d8a; --brand-ink: #ffffff; --answer: #7a4dbf; --ok: #2c7a4b; --bad: #b4372f; --wait: #9a6a00;
  --soft-answer: #f3eefb; --soft-ok: #eaf5ee; --soft-bad: #fbecea; --soft-wait: #fbf3e2;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  font-size: 16px; line-height: 1.45;
}
@media (prefers-color-scheme: dark) {
  :root { color-scheme: dark; --paper: #15181e; --card: #1d2129; --ink: #eceef2; --muted: #a0a7b5; --line: #2e333d;
    --brand: #7fb0e0; --brand-ink: #10161e; --answer: #b99af0; --ok: #6cc18a; --bad: #f08a80; --wait: #e2b04f;
    --soft-answer: #2a2338; --soft-ok: #1e2e24; --soft-bad: #35211f; --soft-wait: #33291a; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink);
  padding: calc(env(safe-area-inset-top, 0px) + 12px) 16px calc(env(safe-area-inset-bottom, 0px) + 32px); }
main { max-width: 560px; margin: 0 auto; }
header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; padding: 8px 2px 16px; }
h1 { font-size: 1.5rem; line-height: 1.2; margin: 0; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 0.9rem; margin: 2px 0 0; }
.live { color: var(--muted); font-size: 0.8rem; white-space: nowrap; display: flex; align-items: center; gap: 6px; }
.live i { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); display: inline-block; }
.live.off i { background: var(--bad); }
h2 { font-size: 1rem; margin: 24px 2px 8px; color: var(--muted); font-weight: 600; }
.empty { border: 1px dashed var(--line); border-radius: 14px; padding: 20px; color: var(--muted); text-align: center; }
article { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 16px; margin: 0 0 12px; }
article.fresh { animation: arrive 1.2s ease-out; }
@keyframes arrive { from { box-shadow: 0 0 0 3px var(--brand); } to { box-shadow: 0 0 0 0 transparent; } }
@media (prefers-reduced-motion: reduce) { article.fresh { animation: none; } }
.kind { display: inline-block; font-size: 0.8rem; font-weight: 600; padding: 2px 10px; border-radius: 999px;
  background: var(--soft-wait); color: var(--wait); }
.kind.question { background: var(--soft-answer); color: var(--answer); }
.title { font-size: 1.15rem; font-weight: 650; margin: 10px 0 4px; }
.quote { font-size: 1.15rem; font-weight: 600; margin: 10px 0 6px; }
.quote::before { content: "\201C"; } .quote::after { content: "\201D"; }
.why { color: var(--muted); font-size: 0.9rem; margin: 0 0 8px; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; margin: 10px 0 0; font-size: 0.95rem; }
dt { color: var(--muted); } dd { margin: 0; overflow-wrap: anywhere; }
.meta { color: var(--muted); font-size: 0.8rem; margin-top: 10px; }
.decisions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
button { font: inherit; font-weight: 600; border-radius: 12px; padding: 11px 16px; border: 1px solid var(--line);
  background: var(--card); color: var(--ink); cursor: pointer; min-height: 44px; }
button.primary { background: var(--brand); color: var(--brand-ink); border-color: var(--brand); }
button.answer { background: var(--answer); color: #fff; border-color: var(--answer); }
button.danger { color: var(--bad); }
button[aria-pressed="true"] { outline: 2px solid var(--brand); outline-offset: 1px; }
button:disabled { opacity: 0.55; cursor: default; }
:focus-visible { outline: 3px solid var(--brand); outline-offset: 2px; }
form { margin-top: 14px; display: grid; gap: 12px; }
label { display: grid; gap: 4px; font-size: 0.9rem; font-weight: 600; }
label small { font-weight: 400; color: var(--muted); }
input, select, textarea { font: inherit; font-weight: 400; color: var(--ink); background: var(--paper);
  border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; width: 100%; }
textarea { min-height: 96px; resize: vertical; }
.check { display: flex; align-items: center; gap: 8px; font-weight: 500; }
.check input { width: auto; }
.hint { background: var(--soft-answer); color: var(--ink); border-radius: 10px; padding: 10px 12px; font-size: 0.9rem; }
.err { color: var(--bad); font-size: 0.9rem; margin: 0; }
.done { display: flex; justify-content: space-between; gap: 12px; padding: 12px 14px; border-bottom: 1px solid var(--line); font-size: 0.95rem; }
.done:last-child { border-bottom: 0; }
.done span:last-child { color: var(--muted); white-space: nowrap; }
.list { background: var(--card); border: 1px solid var(--line); border-radius: 16px; }
.toast { position: fixed; left: 50%; transform: translateX(-50%); bottom: calc(env(safe-area-inset-bottom, 0px) + 20px);
  background: var(--ink); color: var(--paper); padding: 12px 18px; border-radius: 12px; font-weight: 600;
  max-width: calc(100% - 32px); }
.fatal { margin-top: 40px; text-align: center; }
</style>
</head>
<body>
<main>
  <header>
    <div><h1 id="biz">Requests</h1><p class="sub" id="sub">Loading…</p></div>
    <div class="live" id="live" role="status"><i></i><span>Live</span></div>
  </header>
  <section aria-labelledby="h-open"><h2 id="h-open">Waiting for you</h2><div id="open"></div></section>
  <section aria-labelledby="h-done" id="done-wrap" hidden><h2 id="h-done">Done</h2><div class="list" id="done"></div></section>
</main>
<div class="toast" id="toast" role="status" hidden></div>
<script>
(() => {
  "use strict";
  const POLL_MS = 3000;
  const parts = location.pathname.split("/").filter(Boolean); // owner, biz, inbox | requests, id
  const biz = parts[1];
  const single = parts[2] === "requests" ? parts[3] : null;
  const storeKey = "askonce-owner:" + location.pathname;
  const params = new URLSearchParams(location.search);
  let token = params.get("token");
  try {
    if (token) sessionStorage.setItem(storeKey, token); else token = sessionStorage.getItem(storeKey);
  } catch (e) { /* storage blocked: keep the in-memory token */ }
  if (params.has("token")) history.replaceState(null, "", location.pathname);

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const base = "/owner/" + encodeURIComponent(biz);
  const seen = new Set();
  const editing = new Set(); // commitment ids with an open form: never re-render those
  let first = true;

  const KIND = { custom_order: "Custom order", booking: "Booking", quote_request: "Quote request", owner_question: "Customer question" };
  const DECISION = {
    approve: ["Approve", "primary"], decline: ["Decline", "danger"], counter_offer: ["Offer changes", ""],
    quote: ["Send quote", "primary"], suggest_time: ["Suggest another time", ""],
    request_info: ["Ask the customer", ""], answer: ["Answer", "answer"],
  };
  const DONE = { approve: "Approved", decline: "Declined", counter_offer: "Changes offered", quote: "Quote sent",
    suggest_time: "Time suggested", request_info: "Question sent", answer: "Answered" };

  async function api(path, opts = {}) {
    const res = await fetch(base + path, { ...opts, headers: { "Accept": "application/json", "Authorization": "Bearer " + token, ...(opts.body ? { "Content-Type": "application/json" } : {}) } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { const err = new Error(data.error || ("HTTP " + res.status)); err.status = res.status; err.details = data.details; throw err; }
    return data;
  }

  function toast(text) {
    const t = $("toast"); t.textContent = text; t.hidden = false;
    clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, 3500);
  }

  const money = (cents, cur) => (cents == null ? null : new Intl.NumberFormat(undefined, { style: "currency", currency: cur || "USD" }).format(cents / 100));
  const when = (iso) => { try { return new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }); } catch (e) { return iso; } };
  const day = (iso) => { try { return new Date(iso + "T12:00:00").toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" }); } catch (e) { return iso; } };
  const words = (s) => String(s).replace(/^[a-z]+\//, "").replace(/[-_]/g, " ").replace(/^./, (c) => c.toUpperCase());
  const show = (v) => Array.isArray(v) ? v.map(words).join(", ") : typeof v === "boolean" ? (v ? "Yes" : "No") : String(v);

  function details(c) {
    const rows = [];
    for (const [k, v] of Object.entries(c.choices || {})) rows.push([words(k), words(v)]);
    if (c.day) rows.push(["Date", day(c.day)]);
    if (c.window) rows.push(["Time", when(c.window.start) + " – " + new Date(c.window.end).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })]);
    if (c.party_size) rows.push(["Party size", String(c.party_size)]);
    if (c.price && c.price.amount_cents != null) rows.push(["Price", money(c.price.amount_cents, c.price.currency) + (c.price.kind === "from" ? " and up" : "")]);
    for (const [k, v] of Object.entries(c.intake || {})) if (v !== null && v !== "" && v !== c.day) rows.push([words(k), show(v)]);
    for (const [k, v] of Object.entries(c.customer_input || {})) rows.push(["Customer replied", show(v)]);
    return rows;
  }

  function fieldInput(f) {
    const label = el("label"); label.append(el("span", null, f.label + (f.required ? "" : " (optional)")));
    let input;
    if (f.type === "choice") {
      input = el("select"); input.append(el("option", null, "Choose…")); input.firstChild.value = "";
      for (const o of f.options || []) { const opt = el("option", null, o.label || o.value); opt.value = o.value; input.append(opt); }
    } else if (f.type === "long_text") { input = el("textarea"); if (f.max_length) input.maxLength = f.max_length; }
    else if (f.type === "boolean") {
      const wrap = el("label", "check"); input = el("input"); input.type = "checkbox"; input.name = f.key;
      wrap.append(input, el("span", null, f.label)); return wrap;
    } else {
      input = el("input");
      input.type = { money: "number", number: "number", integer: "number", date: "date", time: "time", email: "email", phone: "tel" }[f.type] || "text";
      if (f.type === "money") { input.step = "0.01"; input.inputMode = "decimal"; }
      if (f.type === "integer") input.step = "1";
      if (f.min != null) input.min = f.min; if (f.max != null) input.max = f.max;
      if (f.max_length) input.maxLength = f.max_length;
    }
    input.name = f.key; input.dataset.type = f.type; if (f.required) input.required = true;
    label.append(input); return label;
  }

  function decisionForm(item, decision, art) {
    const c = item.commitment;
    const form = el("form"); form.noValidate = true;
    if (decision === "answer") {
      form.append(el("p", "hint", "Answer once. AskOnce checks your answer, cites it, and gives it to every customer who asks this from now on."));
    }
    if (decision === "request_info") {
      const l = el("label"); l.append(el("span", null, "Your question for the customer"));
      const q = el("textarea"); q.name = "__question"; q.maxLength = 300; q.required = true; l.append(q); form.append(l);
    } else if (decision !== "decline") {
      for (const f of item.response_fields || []) form.append(fieldInput(f));
    }
    const hasNote = (item.response_fields || []).some((f) => f.key === "note");
    if (decision !== "answer" && !(hasNote && decision !== "decline" && decision !== "request_info")) {
      const l = el("label"); l.append(el("span", null, "Note for the customer (optional)"));
      const n = el("textarea"); n.name = "__note"; n.maxLength = 1000; n.style.minHeight = "64px"; l.append(n); form.append(l);
    }
    const err = el("p", "err"); err.setAttribute("role", "alert"); err.hidden = true;
    const [label, cls] = DECISION[decision] || [decision, "primary"];
    const text = decision === "answer" ? "Send answer" : label;
    const send = el("button", cls || "primary", text); send.type = "submit";
    form.append(err, send);
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault(); err.hidden = true;
      const body = { decision, values: {} };
      for (const input of form.querySelectorAll("input, select, textarea")) {
        const v = input.type === "checkbox" ? input.checked : input.value.trim();
        if (input.name === "__note") { if (v) body.note = v; continue; }
        if (input.name === "__question") { body.question = v; continue; }
        if (v === "" ) continue;
        body.values[input.name] = ["money", "number"].includes(input.dataset.type) ? Number(v) : input.dataset.type === "integer" ? parseInt(v, 10) : v;
      }
      send.disabled = true; send.textContent = "Sending…";
      try {
        await api("/requests/" + encodeURIComponent(c.commitment_id) + "/decision", { method: "POST", body: JSON.stringify(body) });
        editing.delete(c.commitment_id);
        toast(DONE[decision] || "Sent");
        art.remove(); lastSig = null;
        refresh();
      } catch (e) {
        err.textContent = e.status === 409 ? "This request is no longer waiting for you." : (e.details && e.details.length ? e.details.map((d) => d.msg).join(" ") : e.message);
        err.hidden = false; send.disabled = false; send.textContent = text;
      }
    });
    return form;
  }

  function card(item) {
    const c = item.commitment;
    const art = el("article"); art.dataset.id = c.commitment_id;
    if (!first && !seen.has(c.commitment_id)) art.classList.add("fresh");
    const isQ = c.kind === "owner_question";
    art.append(el("span", "kind" + (isQ ? " question" : ""), KIND[c.kind] || words(c.kind)));
    if (isQ) {
      art.append(el("p", "quote", c.question || "A customer asked a question."));
      if (c.fact_ref) art.append(el("p", "why", "Your website doesn't say this, so the assistant didn't guess."));
    } else {
      art.append(el("p", "title", c.item_ref ? words(c.item_ref) : (KIND[c.kind] || "Request")));
    }
    const rows = details(c);
    if (rows.length) { const dl = el("dl"); for (const [k, v] of rows) dl.append(el("dt", null, k), el("dd", null, v)); art.append(dl); }
    const meta = "Received " + when(c.created_at) + (c.expires_at ? " · expires " + when(c.expires_at) : "");
    art.append(el("p", "meta", meta));
    if (item.task_status === "input_required") art.append(el("p", "why", "Waiting for the customer to answer your question."));
    const bar = el("div", "decisions"); let formEl = null;
    for (const d of item.allowed_decisions || []) {
      const [text, cls] = DECISION[d] || [d, ""];
      const b = el("button", cls, text); b.type = "button"; b.setAttribute("aria-pressed", "false");
      b.addEventListener("click", () => {
        const open = b.getAttribute("aria-pressed") === "true";
        for (const other of bar.children) other.setAttribute("aria-pressed", "false");
        if (formEl) { formEl.remove(); formEl = null; }
        if (open) { editing.delete(c.commitment_id); return; }
        b.setAttribute("aria-pressed", "true"); editing.add(c.commitment_id);
        formEl = decisionForm(item, d, art); art.append(formEl);
        const firstInput = formEl.querySelector("input, select, textarea"); if (firstInput) firstInput.focus();
      });
      bar.append(b);
    }
    if (bar.children.length) art.append(bar);
    return art;
  }

  let lastSig = null;
  function renderOpen(items) {
    const sig = JSON.stringify(items);
    if (sig === lastSig) return; // unchanged: keep the DOM (focus, open forms, taps in flight)
    lastSig = sig;
    const box = $("open");
    const keep = new Map([...box.querySelectorAll("article")].filter((a) => editing.has(a.dataset.id)).map((a) => [a.dataset.id, a]));
    box.replaceChildren();
    if (!items.length) { box.append(el("div", "empty", single ? "Nothing to decide here any more." : "Nothing waiting. New requests show up here on their own.")); }
    for (const item of items) box.append(keep.get(item.commitment.commitment_id) || card(item));
    for (const item of items) seen.add(item.commitment.commitment_id);
    first = false;
  }

  function renderDone(items) {
    const done = items.filter((i) => i.commitment.decision).slice(0, 10);
    $("done-wrap").hidden = !done.length;
    $("done").replaceChildren(...done.map((i) => {
      const c = i.commitment, row = el("div", "done");
      const what = c.kind === "owner_question" ? (c.question || "Question") : (c.item_ref ? words(c.item_ref) : (KIND[c.kind] || "Request"));
      row.append(el("span", null, (DONE[c.decision.decision] || words(c.decision.decision)) + ": " + what), el("span", null, when(c.decision.decided_at)));
      return row;
    }));
  }

  function live(ok) { $("live").classList.toggle("off", !ok); $("live").lastChild.textContent = ok ? "Live" : "Offline"; }

  async function refresh() {
    try {
      if (single) {
        const item = await api("/requests/" + encodeURIComponent(single));
        $("biz").textContent = "Request"; $("sub").textContent = "Decide this one request";
        renderOpen(item.allowed_decisions && item.allowed_decisions.length ? [item] : []);
        renderDone(item.commitment.decision ? [item] : []);
      } else {
        const [open, all] = await Promise.all([api("/inbox?status=open"), api("/inbox?status=all&limit=30")]);
        $("biz").textContent = open.business_name;
        const n = open.items.length;
        $("sub").textContent = n ? (n === 1 ? "1 request is waiting for you" : n + " requests are waiting for you") : "You're all caught up";
        document.title = (n ? "(" + n + ") " : "") + open.business_name + " · Requests";
        renderOpen(open.items);
        renderDone(all.items.slice().sort((a, b) => ((b.commitment.decision || {}).decided_at || "").localeCompare((a.commitment.decision || {}).decided_at || "")));
      }
      live(true);
    } catch (e) {
      live(false);
      if (e.status === 401) {
        clearInterval(timer);
        const m = $("open"); m.replaceChildren(el("div", "empty fatal", "This link has expired or isn't valid. Ask for a new link to your requests."));
        $("sub").textContent = "Link not valid";
      }
    }
  }

  if (!token) { $("sub").textContent = "Link not valid"; $("open").append(el("div", "empty fatal", "Open this page from the link AskOnce sent you.")); return; }
  const timer = setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  refresh();
})();
</script>
</body>
</html>
"""
