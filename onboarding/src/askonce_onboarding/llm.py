"""Model calls for onboarding, behind a record/replay cache so reruns and tests cost nothing.

Every reply is stored under `onboarding/fixtures/llm/<key>.json`, keyed by the prompt version and the
exact request text. `CachedModel(mode="replay")` never calls Bedrock (tests, CI, the offline demo);
`mode="live"` calls Bedrock through the askonce-bedrock router (and its $5 spend ledger) on a cache
miss and records the reply.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "llm"
ROLE = "onboard"
DEFAULT_MODELS = (
    "au.anthropic.claude-haiku-4-5-20251001-v1:0",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "apac.amazon.nova-pro-v1:0",
)
"""Haiku first: a whole demo run (17 pages) costs about a third of Sonnet's, inside the $5 cap.
Nova Pro is the fallback while Claude is unavailable on the account. `ASKONCE_BEDROCK_ROLE_ONBOARD`
(comma-separated model ids) replaces the list."""


class ModelUnavailable(RuntimeError):
    """A reply is not cached and live calls are off (or Bedrock refused)."""


class Model(Protocol):
    def complete(self, *, kind: str, system: str, user: str, max_tokens: int) -> str: ...


@dataclass
class Usage:
    calls: int = 0
    cache_hits: int = 0
    cost_usd: float = 0.0


def cache_key(kind: str, system: str, user: str) -> str:
    digest = hashlib.sha256(f"{kind}\n{system}\n{user}".encode()).hexdigest()
    return f"{kind}-{digest[:20]}"


class CachedModel:
    def __init__(
        self,
        mode: Literal["replay", "live"] = "replay",
        *,
        cache_dir: Path = FIXTURES_DIR,
        router: Any = None,
        profile: str | None = None,
        region: str | None = None,
    ) -> None:
        self.mode = mode
        self.cache_dir = Path(cache_dir)
        self.usage = Usage()
        self._router = router
        self._profile = profile or os.environ.get("AWS_PROFILE") or "shopfront"
        self._region = region or os.environ.get("AWS_REGION") or "ap-southeast-2"

    def complete(self, *, kind: str, system: str, user: str, max_tokens: int) -> str:
        key = cache_key(kind, system, user)
        path = self.cache_dir / f"{key}.json"
        if path.is_file():
            self.usage.cache_hits += 1
            return str(json.loads(path.read_text(encoding="utf-8"))["text"])
        if self.mode != "live":
            raise ModelUnavailable(f"no cached reply for {key}; rerun with --live to call Bedrock")
        router = self._router or self._make_router()
        self._router = router
        result = router.converse(
            ROLE,
            messages=[{"role": "user", "content": [{"text": user}]}],
            system=system,
            inference_config={"maxTokens": max_tokens, "temperature": 0},
        )
        self.usage.calls += 1
        self.usage.cost_usd += float(getattr(result, "cost_usd", 0.0) or 0.0)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "key": key,
            "kind": kind,
            "model_id": result.model_id,
            "cost_usd": getattr(result, "cost_usd", None),
            "recorded_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
            "text": result.text,
        }
        path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        return result.text

    def _make_router(self) -> Any:
        """The askonce-bedrock router over the AWS profile, with the default (capped) spend ledger.

        A profile created by `aws login` needs `botocore[crt]`; without it the credentials come from
        `aws configure export-credentials` (the same fallback as `scripts/chat.py`).
        """
        import boto3
        import botocore.session
        from askonce_bedrock import BedrockRouter, RouterConfig
        from botocore.credentials import RefreshableCredentials
        from botocore.exceptions import MissingDependencyException

        config = RouterConfig.from_env()
        if not os.environ.get("ASKONCE_BEDROCK_ROLE_ONBOARD"):
            config.roles[ROLE] = DEFAULT_MODELS
        if not os.environ.get("ASKONCE_BEDROCK_BURST"):
            config.burst = 3.0  # one site's pages go out together; keep them on one model
        session = boto3.Session(profile_name=self._profile, region_name=self._region)
        try:
            if session.get_credentials() is None:
                raise ModelUnavailable("No AWS credentials: run `aws login --profile shopfront`.")
        except MissingDependencyException:
            profile = self._profile

            def fetch() -> dict[str, str]:
                cmd = ["aws", "configure", "export-credentials", "--format", "process", "--profile", profile]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode != 0:
                    raise ModelUnavailable(f"AWS credentials unavailable: {proc.stderr.strip()[:200]}")
                data = json.loads(proc.stdout)
                expiry = data.get("Expiration") or (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=15)).isoformat()
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
            session = boto3.Session(botocore_session=core, region_name=self._region)
        return BedrockRouter.from_env(session=session, region=self._region, config=config)


def parse_json_object(text: str) -> dict[str, Any]:
    """The JSON object in a model reply (tolerates code fences and prose around it)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text[text.find("{") : text.rfind("}") + 1]
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        repaired = _close_truncated(text[text.find("{") :]) if "{" in text else None
        if repaired is None:
            raise ValueError(f"model reply is not JSON: {exc}") from exc
        value = repaired
    if not isinstance(value, dict):
        raise ValueError("model reply is not a JSON object")
    return value


def _close_truncated(text: str) -> dict[str, Any] | None:
    """A reply cut off at the token limit: keep every complete value and close the open brackets.

    Scans the JSON once, remembering the last point where a value inside an array or object ended;
    the text up to there plus the matching closing brackets is valid JSON with only the unfinished
    record lost."""
    stack: list[str] = []
    in_string = escape = False
    cut: tuple[int, list[str]] | None = None
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            if not stack:
                return None  # complete object: the parse error is elsewhere
            cut = (i + 1, list(stack))
    if cut is None:
        return None
    end, open_brackets = cut
    try:
        value = json.loads(text[:end] + "".join(reversed(open_brackets)))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
