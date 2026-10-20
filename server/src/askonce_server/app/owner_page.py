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
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#f2f3f1" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0f1211" media="(prefers-color-scheme: dark)">
<title>Requests</title>
<style>
/* The owner's phone. System type, iOS-style grouped surfaces, one thumb-sized decision per request.
   Colour carries meaning: pine = act, lilac = an answer that becomes a cited fact, red = decline. */
:root {
  color-scheme: light;
  --bg: #f2f3f1; --card: #ffffff; --ink: #18201d; --muted: #66706c; --line: rgba(24, 32, 29, .12); --fill: rgba(24, 32, 29, .05);
  --brand: #22604f; --brand-ink: #ffffff; --brand-soft: #e3efe9;
  --answer: #5b43c4; --answer-ink: #ffffff; --answer-soft: #efebfd;
  --ok: #24804a; --bad: #c23a2e; --bad-soft: #fbeceb; --wait: #94620b; --wait-soft: #fbf1df;
  --shadow: 0 1px 2px rgba(24, 32, 29, .06), 0 8px 24px -16px rgba(24, 32, 29, .25);
  --nav: rgba(242, 243, 241, .82);
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, "Segoe UI", Roboto, sans-serif;
  font-size: 17px; line-height: 1.4; -webkit-text-size-adjust: 100%;
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #0f1211; --card: #1b201e; --ink: #eef2f0; --muted: #9aa49f; --line: rgba(238, 242, 240, .12); --fill: rgba(238, 242, 240, .07);
    --brand: #7fd0b4; --brand-ink: #0c211a; --brand-soft: #1b2e27;
    --answer: #b9a6ff; --answer-ink: #1d1442; --answer-soft: #262039;
    --ok: #6fd394; --bad: #ff8a7e; --bad-soft: #3a1f1c; --wait: #e8b75c; --wait-soft: #33291a;
    --shadow: 0 1px 0 rgba(255, 255, 255, .03) inset;
    --nav: rgba(15, 18, 17, .78);
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { background: var(--bg); }
body { margin: 0; background: var(--bg); color: var(--ink); min-height: 100vh; -webkit-tap-highlight-color: transparent; }
button, input, select, textarea { font: inherit; color: inherit; }
:focus-visible { outline: 3px solid var(--brand); outline-offset: 2px; border-radius: 6px; }

/* ------------------------------------------------------------ nav + title */
.nav {
  position: sticky; top: 0; z-index: 5; background: var(--nav); -webkit-backdrop-filter: saturate(1.6) blur(18px); backdrop-filter: saturate(1.6) blur(18px);
  padding: calc(env(safe-area-inset-top, 0px) + 10px) max(16px, env(safe-area-inset-right, 0px)) 10px max(16px, env(safe-area-inset-left, 0px));
  display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; border-bottom: .5px solid transparent; transition: border-color .2s ease;
}
.nav.scrolled { border-bottom-color: var(--line); }
.nav .small { grid-column: 2; font-weight: 600; font-size: .94rem; opacity: 0; transition: opacity .2s ease; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 56vw; }
.nav.scrolled .small { opacity: 1; }
.live { grid-column: 3; justify-self: end; display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: .8rem; }
.live i { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 22%, transparent); }
.live.off i { background: var(--bad); box-shadow: 0 0 0 3px color-mix(in srgb, var(--bad) 22%, transparent); }
main {
  max-width: 600px; margin: 0 auto;
  padding: 2px max(16px, env(safe-area-inset-right, 0px)) calc(env(safe-area-inset-bottom, 0px) + 40px) max(16px, env(safe-area-inset-left, 0px));
}
.title { padding: 4px 4px 18px; }
h1 { font-size: 2rem; line-height: 1.12; letter-spacing: -0.022em; margin: 0; font-weight: 700; text-wrap: balance; }
.sub { color: var(--muted); margin: 6px 0 0; font-size: 1rem; }
.sub b { color: var(--ink); font-weight: 600; }
h2 { font-size: .82rem; font-weight: 600; color: var(--muted); margin: 26px 16px 8px; letter-spacing: .01em; }
section:first-of-type h2 { margin-top: 4px; }

/* ------------------------------------------------------------ request cards */
.stack { display: grid; gap: 14px; }
.req { background: var(--card); border-radius: 20px; box-shadow: var(--shadow); padding: 16px 16px 16px; position: relative; transition: opacity .3s ease, transform .3s ease; }
.req.fresh { animation: arrive 1.6s ease-out; }
@keyframes arrive { 0% { box-shadow: 0 0 0 3px var(--brand), var(--shadow); transform: translateY(-6px); } 30% { transform: none; } 100% { box-shadow: var(--shadow); } }
.req.leaving { opacity: 0; transform: scale(.97); }
.top { display: flex; align-items: center; gap: 10px; }
.icon { width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center; background: var(--brand-soft); color: var(--brand); flex: none; }
.icon svg { width: 19px; height: 19px; }
.req.q .icon { background: var(--answer-soft); color: var(--answer); }
.kindname { font-weight: 600; font-size: .9rem; }
.ago { margin-left: auto; color: var(--muted); font-size: .82rem; white-space: nowrap; }
.newdot { width: 8px; height: 8px; border-radius: 50%; background: var(--brand); margin-left: 6px; }
.req.q .newdot { background: var(--answer); }
.what { font-size: 1.3rem; font-weight: 650; line-height: 1.25; letter-spacing: -0.012em; margin: 14px 0 0; text-wrap: pretty; overflow-wrap: anywhere; }
.req.q .what { font-size: 1.4rem; }
.req.q .what::before { content: "\201C"; } .req.q .what::after { content: "\201D"; }
.key { margin: 6px 0 0; color: var(--ink); font-weight: 500; }
.why { color: var(--muted); margin: 8px 0 0; font-size: .92rem; display: flex; gap: 8px; align-items: flex-start; }
.why svg { width: 16px; height: 16px; flex: none; margin-top: 3px; color: var(--answer); }
dl { margin: 14px 0 0; border-radius: 14px; background: var(--fill); padding: 4px 14px; font-size: .94rem; }
dl div { display: flex; justify-content: space-between; gap: 16px; padding: 9px 0; border-top: .5px solid var(--line); }
dl div:first-child { border-top: 0; }
dt { color: var(--muted); flex: none; }
dd a { color: var(--brand); text-decoration: none; font-weight: 500; }
dd { margin: 0; text-align: right; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
.due { display: flex; align-items: center; gap: 6px; color: var(--wait); font-size: .85rem; margin: 12px 0 0; }
.due svg { width: 15px; height: 15px; }
.waitnote { background: var(--wait-soft); color: var(--wait); border-radius: 12px; padding: 10px 12px; font-size: .9rem; margin: 12px 0 0; }
.decide { display: grid; gap: 8px; margin-top: 16px; }
.decide .more { display: flex; flex-wrap: wrap; gap: 8px; }
.decide .more button { flex: 1 1 auto; white-space: nowrap; padding-inline: 12px; }

.btn {
  appearance: none; border: 0; border-radius: 14px; min-height: 50px; padding: 12px 16px; font-weight: 600; font-size: 1.02rem; cursor: pointer;
  background: var(--fill); color: var(--ink); display: inline-flex; align-items: center; justify-content: center; gap: 8px; touch-action: manipulation;
  transition: transform .1s ease, filter .15s ease;
}
.btn:active { transform: scale(.98); }
.btn.primary { background: var(--brand); color: var(--brand-ink); }
.btn.answer { background: var(--answer); color: var(--answer-ink); }
.btn.danger { color: var(--bad); }
.btn.danger.solid { background: var(--bad); color: #fff; }
.btn.secondary { min-height: 44px; font-size: .95rem; }
.btn:disabled { opacity: .55; cursor: default; transform: none; }
.btn svg { width: 19px; height: 19px; }

/* empty + done */
.empty { background: var(--card); border-radius: 20px; box-shadow: var(--shadow); padding: 28px 22px; text-align: center; }
.empty .ring { width: 52px; height: 52px; margin: 0 auto 12px; border-radius: 50%; display: grid; place-items: center; background: var(--brand-soft); color: var(--brand); }
.empty .ring svg { width: 26px; height: 26px; }
.empty b { display: block; font-size: 1.1rem; }
.empty p { color: var(--muted); margin: 6px 0 0; font-size: .94rem; }
.list { background: var(--card); border-radius: 18px; box-shadow: var(--shadow); overflow: hidden; }
.row { display: grid; grid-template-columns: auto 1fr auto; gap: 4px 12px; align-items: start; padding: 12px 16px; border-top: .5px solid var(--line); }
.row:first-child { border-top: 0; }
.row .dot { width: 24px; height: 24px; border-radius: 50%; display: grid; place-items: center; background: var(--brand-soft); color: var(--ok); margin-top: 1px; }
.row .dot svg { width: 14px; height: 14px; }
.row.no .dot { background: var(--bad-soft); color: var(--bad); }
.row.ans .dot { background: var(--answer-soft); color: var(--answer); }
.row .t { font-weight: 500; overflow-wrap: anywhere; }
.row .s { grid-column: 2 / 4; color: var(--muted); font-size: .88rem; overflow-wrap: anywhere; }
.row .s q { font-style: italic; }
.row time { color: var(--muted); font-size: .82rem; white-space: nowrap; }
.fatal { margin-top: 12px; }

/* ------------------------------------------------------------ the decision sheet */
dialog.sheet {
  position: fixed; inset: auto 0 0 0; margin: 0 auto; width: 100%; max-width: 600px; max-height: 92vh; max-height: 92dvh; padding: 0; border: 0;
  border-radius: 22px 22px 0 0; background: var(--card); color: var(--ink); box-shadow: 0 -10px 40px -10px rgba(0, 0, 0, .35);
  display: flex; flex-direction: column; overflow: hidden;
}
dialog.sheet:not([open]) { display: none; }
dialog.sheet[open] { animation: up .32s cubic-bezier(.2, .8, .2, 1); }
dialog.sheet::backdrop { background: rgba(10, 14, 12, .42); animation: fade .25s ease; }
@keyframes up { from { transform: translateY(100%); } }
@keyframes fade { from { opacity: 0; } }
.grab { width: 38px; height: 5px; border-radius: 3px; background: var(--line); margin: 8px auto 0; flex: none; }
.sheethead { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; padding: 6px 8px 8px; flex: none; }
.sheethead h3 { grid-column: 2; margin: 0; font-size: 1.02rem; font-weight: 650; text-align: center; }
.sheethead .close { grid-column: 1; grid-row: 1; justify-self: start; background: none; border: 0; color: var(--brand); font-weight: 500; padding: 10px 10px; cursor: pointer; border-radius: 10px; min-height: 44px; }
.sheetbody { overflow: auto; padding: 4px 18px 16px; display: grid; gap: 14px; overscroll-behavior: contain; }
.recap { background: var(--fill); border-radius: 14px; padding: 12px 14px; }
.recap .rk { color: var(--muted); font-size: .82rem; font-weight: 600; }
.recap .rv { font-weight: 600; margin-top: 2px; overflow-wrap: anywhere; }
.recap.q .rv { font-size: 1.12rem; }
.explain { display: grid; grid-template-columns: auto 1fr; gap: 10px; align-items: start; background: var(--answer-soft); border-radius: 14px; padding: 12px 14px; font-size: .93rem; }
.explain svg { width: 20px; height: 20px; color: var(--answer); margin-top: 1px; }
.explain b { display: block; margin-bottom: 2px; }
label.f { display: grid; gap: 6px; font-weight: 600; font-size: .9rem; }
label.f .opt { font-weight: 400; color: var(--muted); }
.f input, .f select, .f textarea {
  font-weight: 400; font-size: 17px; background: var(--fill); border: 1px solid transparent; border-radius: 12px; padding: 12px 14px; width: 100%; min-height: 48px;
}
.f [aria-invalid="true"] { border-color: var(--bad); }
.f input:focus, .f select:focus, .f textarea:focus { outline: none; border-color: var(--brand); background: var(--card); }
.f textarea { min-height: 120px; resize: vertical; line-height: 1.4; }
.f .count { justify-self: end; color: var(--muted); font-weight: 400; font-size: .8rem; margin-top: -2px; }
.check { display: flex; align-items: center; gap: 10px; font-weight: 500; min-height: 44px; }
.check input { width: 22px; height: 22px; accent-color: var(--brand); }
.preview { border: 1px solid color-mix(in srgb, var(--answer) 35%, transparent); border-radius: 14px; padding: 12px 14px; }
.preview .pk { color: var(--muted); font-size: .82rem; font-weight: 600; }
.preview q { display: block; font-family: "New York", "Iowan Old Style", Georgia, serif; font-style: italic; font-size: 1.05rem; line-height: 1.4; margin: 6px 0 8px; min-height: 1.4em; overflow-wrap: anywhere; }
.preview q.placeholder { color: var(--muted); }
.preview .cite { display: inline-flex; align-items: center; gap: 6px; font-size: .82rem; font-weight: 600; color: var(--answer); background: var(--answer-soft); border-radius: 999px; padding: 3px 10px; }
.preview .cite svg { width: 14px; height: 14px; }
.err { color: var(--bad); background: var(--bad-soft); border-radius: 12px; padding: 10px 12px; margin: 0; font-size: .92rem; }
.sheetfoot { padding: 10px 18px calc(env(safe-area-inset-bottom, 0px) + 14px); border-top: .5px solid var(--line); flex: none; background: var(--card); }
.sheetfoot .btn { width: 100%; }
.success { text-align: center; padding: 26px 8px 18px; }
.success .ring { width: 64px; height: 64px; margin: 0 auto 14px; border-radius: 50%; display: grid; place-items: center; background: var(--ok); color: #fff; animation: pop .45s cubic-bezier(.2, 1.4, .4, 1); }
.success.ans .ring { background: var(--answer); color: var(--answer-ink); }
.success .ring svg { width: 32px; height: 32px; }
.success b { display: block; font-size: 1.3rem; letter-spacing: -0.01em; }
.success p { color: var(--muted); margin: 8px auto 0; max-width: 30ch; }
@keyframes pop { from { transform: scale(.4); opacity: 0; } }

.toast {
  position: fixed; left: 50%; transform: translateX(-50%); bottom: calc(env(safe-area-inset-bottom, 0px) + 18px); z-index: 20;
  background: var(--ink); color: var(--bg); padding: 12px 18px; border-radius: 14px; font-weight: 600; font-size: .95rem;
  max-width: calc(100% - 32px); box-shadow: 0 10px 30px -10px rgba(0, 0, 0, .4); display: flex; align-items: center; gap: 8px;
}
.toast svg { width: 18px; height: 18px; }

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}
</style>
</head>
<body>
<header class="nav" id="nav">
  <span class="small" id="navtitle" aria-hidden="true"></span>
  <span class="live" id="live" role="status"><i aria-hidden="true"></i><span>Live</span></span>
</header>
<main>
  <div class="title">
    <h1 id="biz">Requests</h1>
    <p class="sub" id="sub">Loading…</p>
  </div>
  <section aria-labelledby="h-open"><h2 id="h-open">Waiting for you</h2><div class="stack" id="open"></div></section>
  <section aria-labelledby="h-done" id="done-wrap" hidden><h2 id="h-done">Done</h2><div class="list" id="done"></div></section>
</main>
<dialog class="sheet" id="sheet" aria-labelledby="sheettitle">
  <div class="grab" aria-hidden="true"></div>
  <div class="sheethead"><button type="button" class="close" id="sheetclose">Cancel</button><h3 id="sheettitle"></h3></div>
  <form id="sheetform" novalidate>
    <div class="sheetbody" id="sheetbody"></div>
    <div class="sheetfoot" id="sheetfoot"></div>
  </form>
</dialog>
<div class="toast" id="toast" role="status" aria-live="polite" hidden></div>
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
  const NS = "http://www.w3.org/2000/svg";
  const icon = (name) => { // static icon paths only, never data
    const s = document.createElementNS(NS, "svg");
    for (const [k, v] of [["viewBox", "0 0 24 24"], ["fill", "none"], ["stroke", "currentColor"], ["stroke-width", "2"], ["stroke-linecap", "round"], ["stroke-linejoin", "round"], ["aria-hidden", "true"]]) s.setAttribute(k, v);
    for (const d of ICONS[name] || []) { const p = document.createElementNS(NS, "path"); p.setAttribute("d", d); s.append(p); }
    return s;
  };
  const ICONS = {
    custom_order: ["M4 21h16", "M5 21v-7a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v7", "M5 16c1.5 1 3 1 4.5 0s3-1 4.5 0 3 1 5 0", "M12 12V8", "M12 5.5c.8-.7.8-1.6 0-2.5-.8.9-.8 1.8 0 2.5z"],
    booking: ["M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z", "M4 10h16", "M8 3v4M16 3v4", "M9 15l2 2 4-4"],
    quote_request: ["M6 3h12v18l-3-2-3 2-3-2-3 2z", "M9 8h6M9 12h6M9 16h3"],
    owner_question: ["M4 5h16v11H9l-5 4z", "M10 9.5a2 2 0 1 1 3 1.7c-.6.4-1 .8-1 1.3", "M12 14.5h.01"],
    check: ["M5 12.5l4.5 4.5L19 7.5"],
    x: ["M6 6l12 12M18 6L6 18"],
    clock: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M12 7v5l3 2"],
    shield: ["M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6z", "M8.5 12l2.5 2.5 4.5-5"],
    repeat: ["M17 2l3 3-3 3", "M4 11V9a4 4 0 0 1 4-4h12", "M7 22l-3-3 3-3", "M20 13v2a4 4 0 0 1-4 4H4"],
    eye: ["M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z", "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"],
  };
  const base = "/owner/" + encodeURIComponent(biz);
  const seen = new Set();
  let first = true;

  const KIND = { custom_order: "Custom order", booking: "Booking", quote_request: "Quote request", owner_question: "Customer question" };
  const DECISION = {
    approve: { label: "Approve", title: "Approve request", cls: "primary", done: "Approved", told: "We're telling the customer now." },
    decline: { label: "Decline", title: "Decline request", cls: "danger", send: "Decline request", sendCls: "danger solid", done: "Declined", told: "We're letting the customer know." },
    counter_offer: { label: "Offer changes", title: "Offer changes", done: "Changes offered", told: "The customer will hear your suggestion." },
    quote: { label: "Send quote", title: "Send a quote", cls: "primary", done: "Quote sent", told: "The customer will hear your quote now." },
    suggest_time: { label: "Other time", title: "Suggest another time", done: "Time suggested", told: "The customer will hear your suggestion." },
    request_info: { label: "Ask customer", title: "Ask the customer", send: "Send question", done: "Question sent", told: "Their device will ask them and bring back the answer." },
    answer: { label: "Answer once", title: "Answer once", cls: "answer", send: "Save answer", done: "Answered", told: "The customer is hearing it now. Everyone who asks this later gets the same answer, cited to you." },
  };
  const PRIMARY = ["answer", "approve", "quote"];

  async function api(path, opts = {}) {
    const res = await fetch(base + path, { ...opts, headers: { "Accept": "application/json", "Authorization": "Bearer " + token, ...(opts.body ? { "Content-Type": "application/json" } : {}) } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { const err = new Error(data.error || ("HTTP " + res.status)); err.status = res.status; err.details = data.details; throw err; }
    return data;
  }

  function toast(text) {
    const t = $("toast"); t.replaceChildren(icon("check"), el("span", null, text)); t.hidden = false;
    clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, 3200);
  }

  // ------------------------------------------------------------ formatting
  const money = (cents, cur) => (cents == null ? null : new Intl.NumberFormat(undefined, { style: "currency", currency: cur || "USD", currencyDisplay: "narrowSymbol", minimumFractionDigits: cents % 100 ? 2 : 0 }).format(cents / 100));
  const when = (iso) => { try { return new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }); } catch (e) { return iso; } };
  const day = (iso) => { try { return new Date(iso + "T12:00:00").toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" }); } catch (e) { return iso; } };
  const hm = (iso) => new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const ago = (iso) => {
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (!isFinite(s)) return "";
    if (s < 45) return "Just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  };
  const words = (s) => String(s).replace(/^[a-z]+\//, "").replace(/[-_]/g, " ").replace(/^./, (c) => c.toUpperCase());
  const show = (v) => Array.isArray(v) ? v.map(words).join(", ") : typeof v === "boolean" ? (v ? "Yes" : "No")
    : /^[a-z][a-z0-9]*(-[a-z0-9]+)*$/.test(String(v)) ? words(v) : String(v); // option slugs read as words
  const titleOf = (c) => c.kind === "owner_question" ? (c.question || "A customer asked a question") : (c.item_ref ? words(c.item_ref) : (KIND[c.kind] || "Request"));

  function keyLine(c) { // the one line an owner decides on: when, how many, how much
    const bits = [];
    if (c.day) bits.push(day(c.day));
    else if (c.window) bits.push(when(c.window.start) + "–" + hm(c.window.end));
    if (c.party_size) bits.push(c.party_size === 1 ? "1 person" : c.party_size + " people");
    if (c.price && c.price.amount_cents != null) bits.push(money(c.price.amount_cents, c.price.currency) + (c.price.kind === "from" ? " and up" : ""));
    return bits.join(", ");
  }
  function details(c) {
    const rows = [];
    for (const [k, v] of Object.entries(c.choices || {})) rows.push([words(k), words(v)]);
    const title = titleOf(c);
    for (const [k, v] of Object.entries(c.intake || {})) if (v !== null && v !== "" && v !== c.day && show(v) !== title) rows.push([words(k), show(v)]);
    for (const v of Object.values(c.customer_input || {})) rows.push(["Customer replied", show(v)]);
    return rows;
  }

  // ------------------------------------------------------------ request cards
  function card(item) {
    const c = item.commitment, isQ = c.kind === "owner_question";
    const art = el("article", "req" + (isQ ? " q" : "")); art.dataset.id = c.commitment_id;
    art.setAttribute("aria-label", (KIND[c.kind] || "Request") + ": " + titleOf(c));
    const isNew = !first && !seen.has(c.commitment_id);
    if (isNew) art.classList.add("fresh");
    const top = el("div", "top");
    const ic = el("span", "icon"); ic.append(icon(ICONS[c.kind] ? c.kind : "booking"));
    top.append(ic, el("span", "kindname", KIND[c.kind] || words(c.kind)), el("span", "ago", ago(c.created_at)));
    if (isNew) { const d = el("span", "newdot"); d.setAttribute("aria-label", "New"); top.append(d); }
    art.append(top);
    art.append(el("p", "what", titleOf(c)));
    if (isQ) {
      const why = el("p", "why"); why.append(icon("shield"), el("span", null, "Not on your website, so the assistant didn't guess. Answer once and it's on record for everyone who asks."));
      art.append(why);
    } else {
      const k = keyLine(c); if (k) art.append(el("p", "key", k));
      const rows = details(c);
      if (rows.length) {
        const dl = el("dl");
        for (const [k2, v] of rows) {
          const d = el("div"), dd = el("dd");
          if (/phone/i.test(k2) && /^\+?[0-9 ()-]{7,}$/.test(v)) { const a = el("a", null, v); a.href = "tel:" + v.replace(/[^+0-9]/g, ""); dd.append(a); } else dd.textContent = v;
          d.append(el("dt", null, k2), dd); dl.append(d);
        }
        art.append(dl);
      }
    }
    if (item.task_status === "input_required") art.append(el("p", "waitnote", "Waiting for the customer to answer your question."));
    else if (c.expires_at) { const due = el("p", "due"); due.append(icon("clock"), el("span", null, "Reply by " + when(c.expires_at))); art.append(due); }

    const ds = item.allowed_decisions || [];
    if (ds.length) {
      const box = el("div", "decide");
      const main = ds.find((d) => PRIMARY.includes(d));
      const button = (d, secondary) => {
        const spec = DECISION[d] || { label: words(d) };
        const b = el("button", "btn " + (secondary ? "secondary " + (spec.cls === "danger" ? "danger" : "") : (spec.cls || "primary")));
        if (d === "answer") b.append(icon("repeat"));
        b.append(el("span", null, spec.label)); b.type = "button";
        b.addEventListener("click", () => openSheet(item, d, art));
        return b;
      };
      if (main) box.append(button(main, false));
      const rest = ds.filter((d) => d !== main).sort((x, y) => (x === "decline") - (y === "decline")); // destructive last
      if (rest.length) { const more = el("div", "more"); rest.forEach((d) => more.append(button(d, true))); box.append(more); }
      art.append(box);
    }
    return art;
  }

  // ------------------------------------------------------------ the decision sheet
  const sheet = $("sheet");
  let sheetCtx = null;
  function fieldInput(f) {
    if (f.type === "boolean") {
      const wrap = el("label", "check"); const input = el("input"); input.type = "checkbox"; input.name = f.key; input.dataset.type = f.type;
      wrap.append(input, el("span", null, f.label)); return wrap;
    }
    const label = el("label", "f"); const cap = el("span", null, f.label); if (!f.required) cap.append(el("span", "opt", " (optional)")); label.append(cap);
    let input;
    if (f.type === "choice") {
      input = el("select"); input.append(el("option", null, "Choose…")); input.firstChild.value = "";
      for (const o of f.options || []) { const opt = el("option", null, o.label || o.value); opt.value = o.value; input.append(opt); }
    } else if (f.type === "long_text") { input = el("textarea"); if (f.max_length) input.maxLength = f.max_length; }
    else {
      input = el("input");
      input.type = { money: "number", number: "number", integer: "number", date: "date", time: "time", email: "email", phone: "tel" }[f.type] || "text";
      if (f.type === "money") { input.step = "0.01"; input.inputMode = "decimal"; }
      if (f.type === "integer") { input.step = "1"; input.inputMode = "numeric"; }
      if (f.min != null) input.min = f.min; if (f.max != null) input.max = f.max;
      if (f.max_length) input.maxLength = f.max_length;
    }
    input.name = f.key; input.dataset.type = f.type; if (f.required) input.required = true;
    label.append(input); return label;
  }
  function fieldsFor(item, decision) {
    if (decision === "decline" || decision === "request_info") return [];
    return (item.response_fields || []).filter((f) => f.required || decision === "suggest_time" || !/^suggested_/.test(f.key));
  }
  function openSheet(item, decision, art) {
    const c = item.commitment, spec = DECISION[decision] || { label: words(decision), title: words(decision) };
    sheetCtx = { item, decision, art };
    clearTimeout(success.timer); $("sheetclose").textContent = "Cancel";
    $("sheettitle").textContent = spec.title;
    const body = $("sheetbody"); body.replaceChildren();
    const recap = el("div", "recap" + (c.kind === "owner_question" ? " q" : ""));
    recap.append(el("div", "rk", c.kind === "owner_question" ? "A customer asked" : (KIND[c.kind] || "Request")), el("div", "rv", c.kind === "owner_question" ? "“" + titleOf(c) + "”" : titleOf(c) + (keyLine(c) ? ", " + keyLine(c) : "")));
    body.append(recap);
    let preview = null;
    if (decision === "answer") {
      const ex = el("div", "explain"); const t = el("div");
      t.append(el("b", null, "You answer once."), el("span", null, "AskOnce checks it, saves it as your answer, and gives it, cited to you, to every customer who asks this from now on."));
      ex.append(icon("repeat"), t); body.append(ex);
    }
    if (decision === "request_info") {
      const l = el("label", "f"); l.append(el("span", null, "Your question for the customer"));
      const q = el("textarea"); q.name = "__question"; q.maxLength = 300; q.required = true; l.append(q); body.append(l);
    }
    for (const f of fieldsFor(item, decision)) body.append(fieldInput(f));
    const hasNote = (item.response_fields || []).some((f) => f.key === "note");
    if (decision !== "answer" && !(hasNote && fieldsFor(item, decision).some((f) => f.key === "note"))) {
      const l = el("label", "f"); const cap = el("span", null, decision === "decline" ? "Reason for the customer" : "Note for the customer"); cap.append(el("span", "opt", " (optional)")); l.append(cap);
      const n = el("textarea"); n.name = "__note"; n.maxLength = 1000; n.style.minHeight = "84px"; l.append(n); body.append(l);
    }
    if (decision === "answer") {
      const ta = body.querySelector("textarea[name=answer]");
      if (ta) {
        ta.placeholder = "For example: Yes, two free spots right out front.";
        const count = el("span", "count"); ta.parentElement.append(count);
        preview = el("div", "preview");
        const q = el("q", "placeholder", "Your answer appears here.");
        const cite = el("span", "cite"); cite.append(icon("check"), el("span", null, "Confirmed by you"));
        preview.append(el("div", "pk", "What customers will hear and see"), q, cite);
        body.append(preview);
        const sync = () => {
          const v = ta.value.trim(); q.textContent = v || "Your answer appears here."; q.classList.toggle("placeholder", !v);
          count.textContent = ta.maxLength > 0 ? ta.value.length + " / " + ta.maxLength : "";
        };
        ta.addEventListener("input", sync); sync();
      }
    }
    const err = el("p", "err"); err.setAttribute("role", "alert"); err.hidden = true; err.id = "sheeterr"; body.append(err);
    const foot = $("sheetfoot"); foot.replaceChildren();
    const send = el("button", "btn " + (spec.sendCls || spec.cls || "primary"), spec.send || spec.label); send.type = "submit"; send.id = "sheetsend";
    foot.append(send); foot.hidden = false;
    sheet.showModal();
    // focus a field only when it must be filled; otherwise the thumb lands on the decision, no keyboard
    const firstInput = body.querySelector("[required]");
    if (firstInput) firstInput.focus(); else send.focus();
  }
  function closeSheet() { if (sheet.open) sheet.close(); sheetCtx = null; }
  $("sheetclose").addEventListener("click", closeSheet);
  sheet.addEventListener("click", (e) => { if (e.target === sheet) closeSheet(); }); // tap the backdrop
  sheet.addEventListener("close", () => { sheetCtx = null; });
  $("sheetform").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!sheetCtx) return;
    const { item, decision, art } = sheetCtx, c = item.commitment, spec = DECISION[decision] || {};
    const form = $("sheetform"), err = $("sheeterr"), send = $("sheetsend");
    err.hidden = true;
    const body = { decision, values: {} };
    const missing = [];
    for (const input of form.querySelectorAll("input, select, textarea")) {
      const v = input.type === "checkbox" ? input.checked : input.value.trim();
      const empty = input.required && v === "";
      input.setAttribute("aria-invalid", String(empty));
      if (empty) { const lab = input.closest("label"); missing.push({ input, name: lab ? lab.firstChild.textContent : input.name }); }
      if (input.name === "__note") { if (v) body.note = v; continue; }
      if (input.name === "__question") { body.question = v; continue; }
      if (v === "") continue;
      body.values[input.name] = ["money", "number"].includes(input.dataset.type) ? Number(v) : input.dataset.type === "integer" ? parseInt(v, 10) : v;
    }
    if (missing.length) { err.textContent = "Fill in " + missing.map((m) => m.name).join(" and ").toLowerCase() + " first."; err.hidden = false; missing[0].input.focus(); return; }
    const label = send.textContent; send.disabled = true; send.textContent = "Sending…";
    try {
      await api("/requests/" + encodeURIComponent(c.commitment_id) + "/decision", { method: "POST", body: JSON.stringify(body) });
      success(decision, spec);
      art.classList.add("leaving");
      setTimeout(() => { art.remove(); lastSig = null; refresh(); }, 320);
    } catch (e) {
      err.textContent = e.status === 409 ? "This request is no longer waiting for you." : (e.details && e.details.length ? e.details.map((d) => d.msg).join(" ") : e.message);
      err.hidden = false; send.disabled = false; send.textContent = label;
    }
  });
  function success(decision, spec) {
    const body = $("sheetbody"); $("sheetfoot").hidden = true; $("sheettitle").textContent = "";
    const box = el("div", "success" + (decision === "answer" ? " ans" : "")); const ring = el("div", "ring"); ring.append(icon(decision === "decline" ? "x" : "check"));
    box.append(ring, el("b", null, decision === "answer" ? "Answer saved" : (spec.done || "Sent")), el("p", null, spec.told || "We're telling the customer now."));
    body.replaceChildren(box);
    $("sheetclose").textContent = "Done"; $("sheetclose").focus();
    toast(decision === "answer" ? "Answer saved and on record" : (spec.done || "Sent"));
    clearTimeout(success.timer); success.timer = setTimeout(() => { closeSheet(); $("sheetclose").textContent = "Cancel"; }, 2600);
    sheet.addEventListener("close", () => { $("sheetclose").textContent = "Cancel"; }, { once: true });
  }

  // ------------------------------------------------------------ lists
  let lastSig = null;
  function renderOpen(items) {
    const sig = JSON.stringify(items.map((i) => [i.commitment.commitment_id, i.commitment.version, i.task_status, i.allowed_decisions]));
    if (sig === lastSig) { // unchanged: keep the DOM (focus, taps in flight); just refresh "x min ago"
      for (const a of $("open").querySelectorAll("article")) { const it = items.find((i) => i.commitment.commitment_id === a.dataset.id); const s = a.querySelector(".ago"); if (it && s) s.textContent = ago(it.commitment.created_at); }
      return;
    }
    lastSig = sig;
    const box = $("open");
    box.replaceChildren();
    if (!items.length) {
      const e = el("div", "empty"); const ring = el("div", "ring"); ring.append(icon("check"));
      e.append(ring, el("b", null, single ? "Nothing to decide here" : "You're all caught up"), el("p", null, single ? "This request has been handled." : "New requests appear here on their own. Keep this page open."));
      box.append(e);
    }
    for (const item of items) box.append(card(item));
    for (const item of items) seen.add(item.commitment.commitment_id);
    first = false;
  }

  function renderDone(items) {
    const done = items.filter((i) => i.commitment.decision).slice(0, 12);
    $("done-wrap").hidden = !done.length;
    $("done").replaceChildren(...done.map((i) => {
      const c = i.commitment, d = c.decision.decision;
      const row = el("div", "row" + (d === "decline" ? " no" : d === "answer" ? " ans" : ""));
      const dot = el("span", "dot"); dot.append(icon(d === "decline" ? "x" : "check"));
      const label = d === "answer" ? "Answered" : ((DECISION[d] || {}).done || words(d));
      const t = el("time", null, ago(c.decision.decided_at)); t.dateTime = c.decision.decided_at; t.title = when(c.decision.decided_at);
      row.append(dot, el("span", "t", label + ": " + (c.kind === "owner_question" ? "“" + titleOf(c) + "”" : titleOf(c))), t);
      const v = c.decision.values || {};
      const said = v.answer || c.decision.note || v.note || c.decision.question;
      if (said) { const s = el("span", "s"); if (d === "answer") s.append(el("span", null, "On record: "), el("q", null, String(said))); else s.textContent = String(said); row.append(s); }
      return row;
    }));
  }

  function live(ok) { $("live").classList.toggle("off", !ok); $("live").lastChild.textContent = ok ? "Live" : "Reconnecting"; }
  function setTitle(name, sub) { $("biz").textContent = name; $("navtitle").textContent = name; $("sub").replaceChildren(...sub); }

  async function refresh() {
    try {
      if (single) {
        const item = await api("/requests/" + encodeURIComponent(single));
        setTitle("Request", [document.createTextNode("Decide this one request")]);
        renderOpen(item.allowed_decisions && item.allowed_decisions.length ? [item] : []);
        renderDone(item.commitment.decision ? [item] : []);
      } else {
        const [open, all] = await Promise.all([api("/inbox?status=open"), api("/inbox?status=all&limit=30")]);
        const n = open.items.length;
        setTitle(open.business_name, n ? [document.createTextNode("Customers are waiting on "), el("b", null, n === 1 ? "1 reply" : n + " replies")] : [document.createTextNode("Nobody is waiting on you right now")]);
        document.title = (n ? "(" + n + ") " : "") + open.business_name;
        renderOpen(open.items);
        renderDone(all.items.slice().sort((a, b) => ((b.commitment.decision || {}).decided_at || "").localeCompare((a.commitment.decision || {}).decided_at || "")));
      }
      live(true);
    } catch (e) {
      live(false);
      if (e.status === 401) {
        clearInterval(timer);
        fatal("This link has expired or isn't valid", "Open the newest link AskOnce sent you, or ask for a new one.");
      }
    }
  }
  function fatal(head, text) {
    setTitle("Requests", [document.createTextNode("Link not valid")]);
    $("live").hidden = true; $("h-open").hidden = true; $("done-wrap").hidden = true;
    const e = el("div", "empty fatal"); const ring = el("div", "ring"); ring.append(icon("x"));
    e.append(ring, el("b", null, head), el("p", null, text)); $("open").replaceChildren(e);
  }

  const nav = $("nav");
  const onScroll = () => nav.classList.toggle("scrolled", window.scrollY > 40);
  window.addEventListener("scroll", onScroll, { passive: true }); onScroll();

  if (!token) { fatal("Open this page from your link", "Use the link AskOnce sent you. It signs you in to your requests."); return; }
  const timer = setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  refresh();
})();
</script>
</body>
</html>
"""
