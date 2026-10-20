"""AskOnce voice host: a browser stand-in for an Alexa+ device with a screen, on localhost.

The customer talks (Web Speech recognition in the browser) or types; this process runs the same loop
as `scripts/chat.py` (a Bedrock model through the askonce-bedrock router and spend ledger, calling the
AskOnce MCP tools); the reply is spoken by the browser (speechSynthesis) and the tools' cards are drawn
on the device screen, citations included.

What a real Alexa+ device would do and this host does too:
  * MCP client on protocol 2026-07-28 with form elicitation and no tasks extension: when a tool needs the
    customer (a read-back to confirm, a name and phone) the server's MRTR question is spoken and shown on
    screen, and the answer (voice, buttons or fields) goes back to the tool call that is waiting for it;
  * owner approvals come back as task ids; the host watches them with `check_request_status` (no model
    call, so no Bedrock cost) and *announces* the owner's reply as soon as it lands
    ("Rosa confirmed your cake for Saturday"), which also goes into the model's context for the next turn.

Run (AskOnce server on :8000 first, see docs/local-testing.md), from the repo root:

    AWS_PROFILE=shopfront server/.venv/bin/python host/voice_host.py      # http://localhost:8100

Options: --port, --mcp-url, --models, --max-tokens, --admin-token (dev: mints owner-page links so the
"Owner's phone" button can open the owner page next to the device).

Browser -> host API (JSON over fetch; host -> browser events over one SSE stream per session):
  POST /api/sessions                      -> {"id"}           a fresh conversation and MCP client
  GET  /api/sessions/{id}/events          -> text/event-stream of {"type": ...} events (see `Session.emit`)
  POST /api/sessions/{id}/say      {"text"}                   one customer turn
  POST /api/sessions/{id}/answer   {"ask_id","action","content"}  reply to an `ask` event
  POST /api/sessions/{id}/action   {"tool","arguments"}       a card button (calls the tool directly)
  POST /api/sessions/{id}/reset                               new conversation
  GET  /api/owner-link/{business_id}                          -> {"url"} (dev; needs --admin-token)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "packages" / "askonce-bedrock" / "src"))

import httpx  # noqa: E402
import mcp_types as types  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from mcp import Client  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import chat  # noqa: E402  (scripts/chat.py: the Bedrock + MCP loop this host shares with the terminal chat)
from askonce_bedrock import BedrockRouter, BudgetExceeded, RouterError  # noqa: E402

logger = logging.getLogger("askonce.voice_host")

STATIC = Path(__file__).resolve().parent / "static"
ASK_TIMEOUT_S = 180.0
"""How long a tool call waits for the customer to answer a question before it is cancelled."""
POLL_S = 2.5
"""How often watched owner tasks are checked (a plain MCP call, no model)."""
IDLE_SESSION_S = 30 * 60
TERMINAL = frozenset({"completed", "failed", "cancelled"})

SYSTEM_EXTRA = """
This is a device with a screen: tool results also show as cards, so keep the spoken reply short and
do not read out lists the card already shows. Lines starting with "[announced]" are things the device
already told the customer on its own (the owner's reply arrived); do not repeat them unless asked."""


# ------------------------------------------------------------------------------- one conversation


class Session:
    """One browser tab: an MCP client, the model conversation, and the event stream to the page."""

    def __init__(self, sid: str, host: Host) -> None:
        self.id = sid
        self.host = host
        self.events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.commands: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()
        self.messages: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.watched: dict[str, str | None] = {}
        """task_id -> last task_status seen."""
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.last_seen = time.monotonic()
        self.client: Client | None = None
        self.tool_config: dict[str, Any] = {}
        self.runner: asyncio.Task[None] | None = None
        now = _dt.datetime.now()
        self.system = chat.SYSTEM_PROMPT.format(today=now.date().isoformat(), weekday=now.strftime("%A")) + SYSTEM_EXTRA

    # -- events -------------------------------------------------------------------------------

    def emit(self, kind: str, **data: Any) -> None:
        """Queue one event for the page. Types: ready, state (thinking|idle), you, tool, card, ask,
        ask_done, reply, announce, spend, error, reset."""
        self.events.put_nowait({"type": kind, **data})

    # -- lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        self.runner = asyncio.create_task(self._run(), name=f"session-{self.id}")

    async def close(self) -> None:
        await self.commands.put(None)
        for future in self.pending.values():
            if not future.done():
                future.cancel()
        if self.runner:
            self.runner.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.runner
        await self.events.put(None)

    async def _run(self) -> None:
        try:
            async with Client(self.host.mcp_url, elicitation_callback=self._elicit, cache=None, mode="2026-07-28") as client:
                self.client = client
                tools = (await client.list_tools()).tools
                self.tool_config = chat.tool_config(tools)
                self.emit(
                    "ready",
                    protocol=client.protocol_version,
                    tools=len(tools),
                    models=list(self.host.router.models(chat.ROLE)) if self.host.router else [],
                )
                self._emit_spend(None)
                poller = asyncio.create_task(self._poll_tasks())
                try:
                    while (command := await self.commands.get()) is not None:
                        kind, payload = command
                        try:
                            if kind == "say":
                                await self._turn(payload)
                            elif kind == "action":
                                await self._action(payload)
                        finally:
                            self.emit("state", state="idle")
                finally:
                    poller.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - shown on the page
            logger.exception("session %s failed", self.id)
            self.emit("error", text=f"Can't reach AskOnce at {self.host.mcp_url}: {type(exc).__name__}: {exc}")

    # -- elicitation (the server's MRTR questions) -----------------------------------------------

    async def _elicit(self, _context: Any, params: Any) -> types.ElicitResult:
        ask_id = secrets.token_urlsafe(6)
        schema = getattr(params, "requested_schema", None) or {}
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[ask_id] = future
        self.emit("ask", id=ask_id, message=getattr(params, "message", "") or "", schema=schema)
        self.emit("state", state="waiting")
        try:
            answer = await asyncio.wait_for(future, ASK_TIMEOUT_S)
        except (TimeoutError, asyncio.CancelledError):
            self.emit("ask_done", id=ask_id, action="cancel")
            return types.ElicitResult(action="cancel")
        finally:
            self.pending.pop(ask_id, None)
            self.emit("state", state="thinking")
        action = answer.get("action") or "accept"
        self.emit("ask_done", id=ask_id, action=action)
        if action != "accept":
            return types.ElicitResult(action=action)
        content = _coerce_content(answer.get("content") or {}, schema)
        return types.ElicitResult(action="accept", content=content)

    def answer(self, ask_id: str, payload: dict[str, Any]) -> bool:
        future = self.pending.get(ask_id)
        if future is None or future.done():
            return False
        future.set_result(payload)
        return True

    # -- tools --------------------------------------------------------------------------------

    async def _call_tool(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """Call one tool; returns (structured result or None, text). Emits the trace and the card."""
        assert self.client is not None
        started = time.monotonic()
        try:
            result = await self.client.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 - the model hears about it
            self.emit("tool", name=name, args=args, status=f"failed: {type(exc).__name__}", ms=_ms(started))
            return None, f"Tool call failed: {exc}"
        data = result.structured_content
        if result.is_error or data is None:
            text = " ".join(getattr(c, "text", "") for c in result.content).strip() or "error"
            self.emit("tool", name=name, args=args, status="error" if result.is_error else "ok", ms=_ms(started))
            return None, text
        status = str(data.get("status") or data.get("task_status") or "ok")
        task_id = data.get("task_id")
        self.emit("tool", name=name, args=args, status=status, task_id=task_id, ms=_ms(started))
        if data.get("card"):
            self.emit("card", card=data["card"], speak=data.get("speak"), tool=name)
        task_status = data.get("task_status")
        if task_id and (task_status is None or task_status not in TERMINAL):
            self.watched.setdefault(task_id, task_status)
        return data, ""

    async def _action(self, payload: dict[str, Any]) -> None:
        """A card button: the tool runs directly (no model), its speak is said and noted for the model."""
        self.emit("state", state="thinking")
        data, text = await self._call_tool(payload["tool"], dict(payload.get("arguments") or {}))
        said = (data or {}).get("speak") or text
        if said:
            self.emit("reply", text=said, source="tool")
            self.notes.append(f"[announced] {said}")

    async def _poll_tasks(self) -> None:
        """Watch owner tasks and announce the owner's reply the moment it lands."""
        assert self.client is not None
        while True:
            await asyncio.sleep(POLL_S)
            for task_id, last in list(self.watched.items()):
                try:
                    result = await self.client.call_tool("check_request_status", {"request_id_or_code": task_id})
                except Exception:  # noqa: BLE001 - try again next round
                    continue
                data = result.structured_content or {}
                status = data.get("task_status")
                if not data.get("found", True):
                    self.watched.pop(task_id, None)
                    continue
                if status == last:
                    continue
                self.watched[task_id] = status
                if status in TERMINAL or status == "input_required":
                    speak = data.get("speak") or data.get("status_message") or ""
                    self.emit("announce", text=speak, card=data.get("card"), task_id=task_id, status=status)
                    self.notes.append(f"[announced] {speak}")
                    if status in TERMINAL:
                        self.watched.pop(task_id, None)

    # -- one customer turn --------------------------------------------------------------------

    async def _turn(self, text: str) -> None:
        router = self.host.router
        if router is None:
            self.emit("error", text="No Bedrock credentials: run `aws login --profile shopfront`, then restart the host.")
            return
        before = router.ledger.snapshot()["spent_usd"] if router.ledger else None
        if self.notes:
            text = "\n".join(self.notes) + "\n" + text
            self.notes.clear()
        checkpoint = len(self.messages)
        self.messages.append({"role": "user", "content": [{"text": text}]})
        self.emit("state", state="thinking")
        speaks: list[str] = []
        try:
            for _ in range(chat.MAX_TOOL_ROUNDS):
                result = await asyncio.to_thread(
                    router.converse,
                    chat.ROLE,
                    self.messages,
                    system=self.system,
                    inference_config={"maxTokens": self.host.max_tokens, "temperature": 0},
                    tool_config=self.tool_config,
                )
                message = result.response["output"]["message"]
                content = [b for b in message.get("content") or [] if "text" in b or "toolUse" in b]
                words = chat.spoken("".join(b.get("text", "") for b in content))
                uses = [b["toolUse"] for b in content if "toolUse" in b]
                if not uses and not words:
                    words = " ".join(speaks) or "Sorry, I didn't catch that."
                    content = [{"text": words}]
                self.messages.append({"role": "assistant", "content": content})
                usage = result.response.get("usage") or {}
                self.emit(
                    "model",
                    model=result.model_id,
                    tokens_in=usage.get("inputTokens"),
                    tokens_out=usage.get("outputTokens"),
                    cost=result.cost_usd or 0,
                )
                if not uses:
                    self.emit("reply", text=words or "...")
                    return
                results = []
                for use in uses:
                    data, err = await self._call_tool(use["name"], dict(use.get("input") or {}))
                    if data is None:
                        results.append({"toolUseId": use["toolUseId"], "content": [{"text": err[:4000]}], "status": "error"})
                    else:
                        results.append({"toolUseId": use["toolUseId"], "content": [{"json": chat._compact(data)}]})  # noqa: SLF001
                speaks = [
                    b["content"][0]["json"]["speak"]
                    for b in results
                    if "json" in b["content"][0] and b["content"][0]["json"].get("speak")
                ]
                self.messages.append({"role": "user", "content": [{"toolResult": r} for r in results]})
            self.messages.append({"role": "assistant", "content": [{"text": "Sorry, that took too long."}]})
            self.emit("reply", text="Sorry, that took too long.")
        except BudgetExceeded as exc:
            del self.messages[checkpoint:]
            self.emit("error", text=f"Bedrock spend cap reached: {exc}")
        except RouterError as exc:
            del self.messages[checkpoint:]
            self.emit("error", text=f"Bedrock: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("turn failed")
            del self.messages[checkpoint:]
            self.emit("error", text=f"{type(exc).__name__}: {exc}")
        finally:
            self._emit_spend(before)

    def _emit_spend(self, before: float | None) -> None:
        ledger = self.host.router.ledger if self.host.router else None
        if ledger is None:
            return
        snap = ledger.snapshot()
        turn = snap["spent_usd"] - before if before is not None else None
        self.emit("spend", turn=turn, total=snap["spent_usd"], budget=snap["budget_usd"])

    def reset(self) -> None:
        self.messages.clear()
        self.notes.clear()
        self.emit("reset")


def _ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _coerce_content(content: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Page answers arrive as strings/booleans; give the server the schema's types."""
    props: dict[str, Any] = schema.get("properties") or {}
    out: dict[str, Any] = {}
    for key, value in content.items():
        spec = props.get(key)
        if spec is None or value is None or value == "":
            continue
        if isinstance(value, str):
            choices = spec.get("enum") or [o.get("const") for o in spec.get("oneOf") or [] if "const" in o]
            try:
                value = chat._coerce(value.strip(), spec, choices)  # noqa: SLF001
            except ValueError:
                pass
        out[key] = value
    return out


# ------------------------------------------------------------------------------- the app


class Host:
    def __init__(self, router: BedrockRouter | None, mcp_url: str, max_tokens: int, admin_token: str | None) -> None:
        self.router = router
        self.mcp_url = mcp_url
        self.max_tokens = max_tokens
        self.admin_token = admin_token
        self.sessions: dict[str, Session] = {}

    def get(self, sid: str) -> Session:
        session = self.sessions.get(sid)
        if session is None:
            raise HTTPException(404, "unknown session")
        session.last_seen = time.monotonic()
        return session

    async def reap(self) -> None:
        while True:
            await asyncio.sleep(60)
            cutoff = time.monotonic() - IDLE_SESSION_S
            for sid, session in list(self.sessions.items()):
                if session.last_seen < cutoff:
                    self.sessions.pop(sid, None)
                    await session.close()


class SayBody(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class AnswerBody(BaseModel):
    ask_id: str
    action: str = "accept"
    content: dict[str, Any] = Field(default_factory=dict)


class ActionBody(BaseModel):
    tool: str = Field(max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)


def create_app(host: Host) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
        reaper = asyncio.create_task(host.reap())
        try:
            yield
        finally:
            reaper.cancel()
            for session in list(host.sessions.values()):
                await session.close()

    app = FastAPI(title="AskOnce voice host", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.post("/api/sessions")
    async def new_session() -> dict[str, str]:
        sid = secrets.token_urlsafe(12)
        session = Session(sid, host)
        host.sessions[sid] = session
        session.start()
        return {"id": sid}

    @app.get("/api/sessions/{sid}/events")
    async def events(sid: str, request: Request) -> StreamingResponse:
        session = host.get(sid)

        async def stream():  # type: ignore[no-untyped-def]
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(session.events.get(), 15)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event is None:
                    break
                session.last_seen = time.monotonic()
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.post("/api/sessions/{sid}/say")
    async def say(sid: str, body: SayBody) -> dict[str, bool]:
        session = host.get(sid)
        session.emit("you", text=body.text)
        await session.commands.put(("say", body.text))
        return {"ok": True}

    @app.post("/api/sessions/{sid}/answer")
    async def answer(sid: str, body: AnswerBody) -> dict[str, bool]:
        return {"ok": host.get(sid).answer(body.ask_id, body.model_dump())}

    @app.post("/api/sessions/{sid}/action")
    async def action(sid: str, body: ActionBody) -> dict[str, bool]:
        await host.get(sid).commands.put(("action", body.model_dump()))
        return {"ok": True}

    @app.post("/api/sessions/{sid}/reset")
    async def reset(sid: str) -> dict[str, bool]:
        host.get(sid).reset()
        return {"ok": True}

    @app.get("/api/owner-link/{business_id}")
    async def owner_link(business_id: str) -> dict[str, str]:
        if not host.admin_token:
            raise HTTPException(404, "owner links need --admin-token")
        base = host.mcp_url.rsplit("/mcp", 1)[0]
        async with httpx.AsyncClient(timeout=10) as http:
            res = await http.post(
                f"{base}/owner/{business_id}/links", headers={"Authorization": f"Bearer {host.admin_token}"}
            )
        if res.status_code != 200:
            raise HTTPException(res.status_code, "could not mint an owner link")
        return {"url": res.json()["inbox_url"]}

    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AskOnce voice host (browser device + Bedrock + MCP).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ASKONCE_HOST_PORT", "8100")))
    parser.add_argument("--mcp-url", default=os.environ.get("ASKONCE_MCP_URL", "http://localhost:8000/mcp"))
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE") or "shopfront")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION") or "ap-southeast-2")
    parser.add_argument("--models", help="comma-separated model ids for the host role (default: Nova first)")
    parser.add_argument("--max-tokens", type=int, default=400)
    parser.add_argument(
        "--admin-token",
        default=os.environ.get("ASKONCE_ADMIN_TOKEN", "DEV-ONLY-admin-token"),
        help="the AskOnce server's admin token, to mint owner-page links (dev default)",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    models = tuple(m.strip() for m in (args.models or "").split(",") if m.strip())
    try:
        router: BedrockRouter | None = chat.bedrock_router(args.profile, args.region, models)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - the page still works for owner links and tools
        print(f"WARNING: no Bedrock (run `aws login --profile {args.profile}`, then restart): {exc}", flush=True)
        router = None
    host = Host(router, args.mcp_url, args.max_tokens, args.admin_token or None)
    print(f"AskOnce voice host: http://localhost:{args.port}  (MCP {args.mcp_url})", flush=True)
    uvicorn.run(create_app(host), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
