"""Talk to AskOnce in the terminal, the way Alexa+ would: a Bedrock model plus the AskOnce MCP tools.

You type as the customer. The model (through the askonce-bedrock router, role `host`) calls the
server's tools; their results go back to it as `toolResult`s and it answers in short, spoken-style
text. When a tool needs the customer (a confirmation read-back or missing details), the server asks
through MRTR elicitation and this script asks you in the terminal.

The MCP client behaves like Alexa+'s documented client: protocol 2026-07-28, no tasks extension (so
owner approvals come back as `task_id`s polled with get_task / check_request_status).

Every Bedrock call goes through the spend ledger: the run stops a call (BudgetExceeded) rather than
pass the cap (`ASKONCE_BEDROCK_BUDGET_USD`, default $5.00 in total). After each turn the running spend
is printed.

    # server running locally (docs/local-testing.md), then from the repo root:
    AWS_PROFILE=shopfront server/.venv/bin/python scripts/chat.py [--url http://localhost:8000/mcp]

Commands: /quit, /reset (new conversation), /spend.
Piped stdin works too (each line is one customer turn or one answer to a question); lines are echoed.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "packages" / "askonce-bedrock" / "src"))

import mcp_types as types  # noqa: E402
from mcp import Client  # noqa: E402

from askonce_bedrock import BedrockRouter, BudgetExceeded, RouterConfig, RouterError, SpendLedger  # noqa: E402
from askonce_bedrock.router import (  # noqa: E402
    HAIKU_AU,
    HAIKU_GLOBAL,
    NOVA_2_LITE_GLOBAL,
    NOVA_PRO_APAC,
    SONNET_AU,
    SONNET_GLOBAL,
)

ROLE = "host"
NOVA_FIRST: tuple[str, ...] = (NOVA_PRO_APAC, NOVA_2_LITE_GLOBAL, SONNET_AU, SONNET_GLOBAL, HAIKU_AU, HAIKU_GLOBAL)
"""The host role while Claude is blocked on this account ("use case details not submitted"): Nova
first, Claude after it (parked automatically when it refuses). `ASKONCE_BEDROCK_ROLE_HOST` or
`--models` replace it."""

MAX_TOOL_ROUNDS = 6
"""Model calls per customer turn (each may call tools) before the turn is cut short."""

RESULT_DROP_KEYS = frozenset({"card", "trace_id", "profile_revision", "evidence_url", "replayed"})
"""Display-only result fields left out of the toolResult sent to the model (cards are for screens)."""

SYSTEM_PROMPT = """\
You are a voice assistant like Alexa+, talking to a customer out loud. You help them with local
businesses through the AskOnce tools.

How to talk:
- Every reply is spoken: one to three short sentences, plain words. No lists, markdown, URLs or emojis.
- Each tool result has a "speak" field. Say it faithfully: you may shorten it, but never change a
  name, price, time, date, allergen or policy in it.

Facts:
- Never invent business facts (prices, hours, availability, allergens, dietary claims, policies).
  Only say what a tool result says. If the tools do not say it, say you don't know.
- If a result carries a "handoff", offer to ask the owner; call ask_owner only after the customer
  says yes.
- Use business_id and item names exactly as the tools return them.

Changing things:
- book, place_custom_order, request_quote, cancel_booking, cancel_request and ask_owner act for the
  customer. Call them only when the customer has clearly asked for that, with what they asked for.
  The tool itself reads the details back and asks the customer to confirm; never say something is
  booked, ordered or cancelled unless the tool result says so.
- If a result has a task_id, the owner still has to approve. Say so, and when the customer asks
  for news, call check_request_status with that task_id.

Today is {today} ({weekday}). Dates go to tools as YYYY-MM-DD, times as local ISO-8601.
Ask a short question when you need something from the customer before calling a tool."""


# ------------------------------------------------------------------------------- terminal


class Term:
    def __init__(self) -> None:
        self.color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.echo = not sys.stdin.isatty()

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def dim(self, text: str) -> None:
        print(self._wrap("2", text), flush=True)

    def say(self, who: str, text: str) -> None:
        label = self._wrap("1", f"{who}>")
        body = textwrap.fill(text, width=100, subsequent_indent=" " * (len(who) + 2))
        print(f"{label} {body}", flush=True)

    def warn(self, text: str) -> None:
        print(self._wrap("33", text), flush=True)

    async def ask(self, prompt: str) -> str | None:
        """One line from stdin (None at EOF). Piped input is echoed so transcripts read naturally."""
        print(prompt, end="", flush=True)
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            print()
            return None
        line = line.rstrip("\n")
        if self.echo:
            print(line, flush=True)
        return line


TERM = Term()


# ------------------------------------------------------------------------------- AWS credentials


def bedrock_router(profile: str | None, region: str, models: tuple[str, ...]) -> BedrockRouter:
    """The router (with the default spend ledger) over the given AWS profile.

    A profile created by `aws login` needs `botocore[crt]`, which the server venv does not ship; in
    that case the credentials come from `aws configure export-credentials` (refreshed when they expire)."""
    import boto3
    import botocore.session
    from botocore.credentials import RefreshableCredentials
    from botocore.exceptions import MissingDependencyException

    config = RouterConfig.from_env()
    if models:
        config.roles[ROLE] = models
    elif not os.environ.get("ASKONCE_BEDROCK_ROLE_HOST"):
        config.roles[ROLE] = NOVA_FIRST
    if not os.environ.get("ASKONCE_BEDROCK_BURST"):
        # A turn makes 2-4 calls back to back; with burst 1 they would hop between models mid-turn.
        config.burst = 4.0

    session = boto3.Session(profile_name=profile, region_name=region)
    try:
        if session.get_credentials() is None:
            raise SystemExit("No AWS credentials: set AWS_PROFILE (e.g. shopfront) or pass --profile.")
    except MissingDependencyException:

        def fetch() -> dict[str, str]:
            cmd = ["aws", "configure", "export-credentials", "--format", "process"]
            if profile:
                cmd += ["--profile", profile]
            data = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
            expiry = data.get("Expiration") or (_dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=15)).isoformat()
            return {
                "access_key": data["AccessKeyId"],
                "secret_key": data["SecretAccessKey"],
                "token": data.get("SessionToken"),
                "expiry_time": expiry,
            }

        core = botocore.session.get_session()
        core._credentials = RefreshableCredentials.create_from_metadata(  # noqa: SLF001
            metadata=fetch(), refresh_using=fetch, method="aws-cli-export-credentials"
        )
        session = boto3.Session(botocore_session=core, region_name=region)
    return BedrockRouter.from_env(session=session, region=region, config=config)


# ------------------------------------------------------------------------------- MCP -> Converse


def _simplify(schema: Any) -> Any:
    """JSON Schema the Converse tool spec (Nova in particular) accepts: `anyOf [X, null]` -> X,
    no `title`s."""
    if isinstance(schema, list):
        return [_simplify(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title":
            continue
        if key == "properties" and isinstance(value, dict):  # keys are property names, not keywords
            out[key] = {name: _simplify(sub) for name, sub in value.items()}
        else:
            out[key] = _simplify(value)
    any_of = out.get("anyOf")
    if isinstance(any_of, list):
        rest = [s for s in any_of if not (isinstance(s, dict) and s.get("type") == "null")]
        if len(rest) == 1 and isinstance(rest[0], dict):
            merged = {**rest[0], **{k: v for k, v in out.items() if k != "anyOf"}}
            if "default" in merged and merged["default"] is None:
                del merged["default"]
            return merged
    return out


def tool_config(tools: list[types.Tool]) -> dict[str, Any]:
    specs = []
    for tool in tools:
        description = " ".join((tool.description or tool.name).split())
        specs.append(
            {
                "toolSpec": {
                    "name": tool.name,
                    "description": description[:1000],
                    "inputSchema": {"json": _simplify(tool.input_schema)},
                }
            }
        )
    return {"tools": specs, "toolChoice": {"auto": {}}}


def _compact(value: Any) -> Any:
    """Drop display-only keys and empty values, to keep the model's input (and the bill) small."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in RESULT_DROP_KEYS:
                continue
            item = _compact(item)
            if item is None or item == [] or item == {}:
                continue
            out[key] = item
        return out
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def _short(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def trace_line(name: str, args: dict[str, Any], status: str, elapsed_s: float, asked: int) -> str:
    shown = ", ".join(f"{k}={_short(v, 40)}" for k, v in args.items() if k != "business_id")
    biz = f"@{args['business_id']} " if args.get("business_id") else ""
    extra = f", asked {asked}x" if asked else ""
    return f"   · {name} {biz}({_short(shown, 140)}) -> {status}{extra} [{elapsed_s * 1000:.0f} ms]"


_THINKING = re.compile(r"<thinking>.*?</thinking>", re.DOTALL)


def spoken(text: str) -> str:
    """The reply minus Nova's `<thinking>` blocks."""
    return " ".join(_THINKING.sub("", text).split())


# ------------------------------------------------------------------------------- elicitation


class Elicitor:
    """Answers the server's MRTR questions by asking the person at the terminal."""

    def __init__(self) -> None:
        self.count = 0

    async def __call__(self, _context: Any, params: Any) -> types.ElicitResult:
        self.count += 1
        message = getattr(params, "message", "") or ""
        schema = getattr(params, "requested_schema", None) or {}
        props: dict[str, Any] = schema.get("properties") or {}
        required = set(schema.get("required") or ())
        TERM.say("alexa (asks)", message)
        if not props:
            answer = await TERM.ask("  [y/n] ")
            return types.ElicitResult(action="accept" if _yes(answer) else "decline", content={})
        if set(props) == {"confirm"}:
            answer = await TERM.ask("  confirm? [y/n] ")
            if answer is None:
                return types.ElicitResult(action="cancel")
            return types.ElicitResult(action="accept", content={"confirm": _yes(answer)})
        content: dict[str, Any] = {}
        for key, spec in props.items():
            label = spec.get("title") or key.replace("_", " ")
            hint = spec.get("description") or ""
            choices = spec.get("enum") or [o.get("const") for o in spec.get("oneOf") or [] if "const" in o]
            while True:
                suffix = f" ({' / '.join(map(str, choices))})" if choices else ""
                optional = "" if key in required else " [optional, Enter to skip]"
                if hint and hint.lower() != label.lower():
                    TERM.dim(f"  {hint}")
                answer = await TERM.ask(f"  {label}{suffix}{optional}: ")
                if answer is None or answer.strip().lower() in ("/cancel", "cancel"):
                    return types.ElicitResult(action="cancel")
                answer = answer.strip()
                if not answer and key not in required:
                    break
                try:
                    content[key] = _coerce(answer, spec, choices)
                    break
                except ValueError as exc:
                    TERM.warn(f"  {exc}")
        return types.ElicitResult(action="accept", content=content)


def _yes(answer: str | None) -> bool:
    return (answer or "").strip().lower() in ("y", "yes", "yeah", "yep", "sure", "ok", "okay", "go ahead")


def _coerce(answer: str, spec: dict[str, Any], choices: list[Any]) -> Any:
    kind = spec.get("type")
    if kind == "boolean":
        return _yes(answer)
    if kind == "integer":
        try:
            return int(answer)
        except ValueError:
            raise ValueError("please give a whole number") from None
    if kind == "number":
        try:
            return float(answer)
        except ValueError:
            raise ValueError("please give a number") from None
    if choices:
        match = [c for c in choices if str(c).lower() == answer.lower()]
        if not match:
            raise ValueError(f"please pick one of: {', '.join(map(str, choices))}")
        return match[0]
    if kind == "array":
        return [part.strip() for part in answer.split(",") if part.strip()]
    return answer


# ------------------------------------------------------------------------------- the chat


class Chat:
    def __init__(self, client: Client, router: BedrockRouter, tools: list[types.Tool], elicitor: Elicitor, args: Any):
        self.client = client
        self.router = router
        self.tool_config = tool_config(tools)
        self.elicitor = elicitor
        self.max_tokens = args.max_tokens
        self.debug = args.debug
        self.messages: list[dict[str, Any]] = []
        now = _dt.datetime.now()
        self.system = SYSTEM_PROMPT.format(today=now.date().isoformat(), weekday=now.strftime("%A"))

    def reset(self) -> None:
        self.messages = []

    async def _call_tool(self, use: dict[str, Any]) -> dict[str, Any]:
        name, args = use["name"], dict(use.get("input") or {})
        before = self.elicitor.count
        started = time.monotonic()
        try:
            result = await self.client.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001 - the model hears about it, the chat goes on
            TERM.dim(trace_line(name, args, f"FAILED {type(exc).__name__}", time.monotonic() - started, 0))
            return {"toolUseId": use["toolUseId"], "content": [{"text": f"Tool call failed: {exc}"}], "status": "error"}
        elapsed = time.monotonic() - started
        data = result.structured_content
        if result.is_error or data is None:
            text = " ".join(getattr(c, "text", "") for c in result.content).strip() or "error"
            status = "ERROR" if result.is_error else "ok (text)"
            TERM.dim(trace_line(name, args, status, elapsed, self.elicitor.count - before))
            block: dict[str, Any] = {"toolUseId": use["toolUseId"], "content": [{"text": text[:4000]}]}
            if result.is_error:
                block["status"] = "error"
            return block
        status = str(data.get("status") or data.get("task_status") or "ok")
        if data.get("task_id"):
            status += f" task={data['task_id']}"
        TERM.dim(trace_line(name, args, status, elapsed, self.elicitor.count - before))
        return {"toolUseId": use["toolUseId"], "content": [{"json": _compact(data)}]}

    async def turn(self, text: str) -> None:
        """One customer turn: model -> tools -> model ... until it answers in words."""
        checkpoint = len(self.messages)
        self.messages.append({"role": "user", "content": [{"text": text}]})
        speaks: list[str] = []
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                result = await asyncio.to_thread(
                    self.router.converse,
                    ROLE,
                    self.messages,
                    system=self.system,
                    inference_config={"maxTokens": self.max_tokens, "temperature": 0},
                    tool_config=self.tool_config,
                )
                message = result.response["output"]["message"]
                if self.debug:
                    TERM.dim(f"   raw: {json.dumps(message.get('content'), ensure_ascii=False)[:1500]}")
                content = [b for b in message.get("content") or [] if "text" in b or "toolUse" in b]
                words = spoken("".join(b.get("text", "") for b in content))
                uses = [b["toolUse"] for b in content if "toolUse" in b]
                if not uses and not words:
                    # Seen with Nova: an end_turn with only a <thinking> block (or nothing). Fall back to
                    # the tools' own spoken text, which is faithful by construction.
                    words = " ".join(speaks) or "Sorry, I didn't catch that."
                    content = [{"text": words}]
                    TERM.dim("   (model returned no spoken text; using the tool's speak)")
                self.messages.append({"role": "assistant", "content": content})
                usage = result.response.get("usage") or {}
                TERM.dim(
                    f"   ~ {result.model_id}: {usage.get('inputTokens')}+{usage.get('outputTokens')} tokens,"
                    f" ${result.cost_usd or 0:.4f}, stop={result.response.get('stopReason')}"
                )
                if words and uses:
                    TERM.dim(f"   (model, before tools: {words})")
                if not uses:
                    TERM.say("alexa", words or "...")
                    return
                results = [await self._call_tool(use) for use in uses]
                speaks = [
                    b["content"][0]["json"]["speak"]
                    for b in results
                    if "json" in b["content"][0] and b["content"][0]["json"].get("speak")
                ]
                self.messages.append({"role": "user", "content": [{"toolResult": r} for r in results]})
            TERM.warn(f"(stopped after {MAX_TOOL_ROUNDS} model calls this turn)")
            self.messages.append({"role": "assistant", "content": [{"text": "Sorry, that took too long."}]})
        except BudgetExceeded as exc:
            TERM.warn(str(exc))
            del self.messages[checkpoint:]
        except RouterError as exc:
            TERM.warn(f"Bedrock: {exc}")
            del self.messages[checkpoint:]
        except Exception as exc:  # noqa: BLE001 - e.g. a ValidationException from the model API
            TERM.warn(f"error: {type(exc).__name__}: {exc}")
            del self.messages[checkpoint:]


def print_spend(ledger: SpendLedger | None, since: float | None = None) -> float:
    if ledger is None:
        return 0.0
    snap = ledger.snapshot()
    turn = f"turn ${snap['spent_usd'] - since:.4f} · " if since is not None else ""
    TERM.dim(
        f"   [spend] {turn}total ${snap['spent_usd']:.4f} of ${snap['budget_usd']:.2f}"
        f" · remaining ${snap['remaining_usd']:.4f}"
    )
    return float(snap["spent_usd"])


async def main(args: argparse.Namespace) -> int:
    models = tuple(m.strip() for m in (args.models or "").split(",") if m.strip())
    router = bedrock_router(args.profile, args.region, models)
    elicitor = Elicitor()
    async with Client(args.url, elicitation_callback=elicitor, cache=None, mode=args.protocol) as client:
        tools = (await client.list_tools()).tools
        TERM.dim(
            f"connected to {args.url} (protocol {client.protocol_version}, no tasks extension); "
            f"{len(tools)} tools; models: {', '.join(router.models(ROLE))}"
        )
        TERM.dim("commands: /quit /reset /spend")
        spent = print_spend(router.ledger)
        chat = Chat(client, router, tools, elicitor, args)
        while True:
            line = await TERM.ask("\nyou> ")
            if line is None or line.strip() in ("/quit", "/exit"):
                break
            text = line.strip()
            if not text:
                continue
            if text == "/reset":
                chat.reset()
                TERM.dim("   (new conversation)")
                continue
            if text == "/spend":
                from askonce_bedrock.spend import format_snapshot

                if router.ledger is not None:
                    TERM.dim(textwrap.indent(format_snapshot(router.ledger.snapshot()), "   "))
                continue
            await chat.turn(text)
            spent = print_spend(router.ledger, spent)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Terminal chat host for AskOnce (Bedrock + MCP).")
    parser.add_argument("--url", default=os.environ.get("ASKONCE_MCP_URL", "http://localhost:8000/mcp"))
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE") or "shopfront")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION") or "ap-southeast-2")
    parser.add_argument("--models", help="comma-separated model ids for the host role (default: Nova first)")
    parser.add_argument("--max-tokens", type=int, default=400, help="maxTokens per model call (default 400)")
    parser.add_argument("--debug", action="store_true", help="print the model's raw content blocks")
    parser.add_argument(
        "--protocol",
        default="2026-07-28",
        help="MCP protocol: 2026-07-28 (default, like Alexa+), 'auto' (probe) or 'legacy' (2025-11-25 handshake)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main(parse_args())))
    except KeyboardInterrupt:
        print()
