"""The spend ledger and the router's hard budget cap (botocore Stubber, no network)."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from askonce_bedrock import (
    ALL_MODELS,
    PRICES_PER_MTOK,
    BedrockRouter,
    BudgetExceeded,
    LedgerError,
    RouterConfig,
    SpendLedger,
    cost_usd,
    default_ledger_path,
    worst_case_tokens,
)
from askonce_bedrock.ledger import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    TOOL_OVERHEAD_TOKENS,
    UNKNOWN_MODEL_PRICE,
)
from askonce_bedrock.router import NOVA_2_LITE_GLOBAL, NOVA_LITE_APAC, NOVA_PRO_APAC, SONNET_AU
from askonce_bedrock.spend import main as spend_main

MESSAGES = [{"role": "user", "content": [{"text": "Say hi"}]}]
PACKAGE_SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def stubbed() -> Iterator[tuple[Any, Stubber]]:
    client = boto3.client(
        "bedrock-runtime", region_name="ap-southeast-2", aws_access_key_id="test", aws_secret_access_key="test"
    )
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def reply(*, inputs: int, outputs: int, **extra_usage: int) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": inputs, "outputTokens": outputs, "totalTokens": inputs + outputs, **extra_usage},
        "metrics": {"latencyMs": 12},
    }


def router_for(client: Any, ledger: SpendLedger | None, roles: dict[str, tuple[str, ...]]) -> BedrockRouter:
    return BedrockRouter(
        client,
        RouterConfig(roles=roles, max_wait_s=0),
        clock=lambda: 1000.0,
        sleep=lambda _s: None,
        rng=random.Random(1),
        ledger=ledger,
    )


# ---------------------------------------------------------------------------- pricing


def test_every_router_model_has_a_price() -> None:
    assert set(ALL_MODELS) <= set(PRICES_PER_MTOK)
    for model_id in ALL_MODELS:
        in_price, out_price = PRICES_PER_MTOK[model_id]
        assert 0 < in_price < out_price


def test_cost_from_usage_including_cache_tokens() -> None:
    in_price, out_price = PRICES_PER_MTOK[NOVA_PRO_APAC]
    usage = {"inputTokens": 1000, "outputTokens": 200, "cacheReadInputTokens": 100, "cacheWriteInputTokens": 40}
    expected = (1100 * in_price + 40 * in_price * 1.25 + 200 * out_price) / 1e6
    assert cost_usd(NOVA_PRO_APAC, usage) == pytest.approx(expected)
    assert cost_usd(NOVA_PRO_APAC, None) == 0
    unknown = cost_usd("vendor.mystery-model-v9", {"inputTokens": 1_000_000})
    assert unknown == pytest.approx(UNKNOWN_MODEL_PRICE[0])  # unknown models get the most expensive price


def test_worst_case_tokens() -> None:
    request = {"messages": MESSAGES, "inferenceConfig": {"maxTokens": 300}}
    chars = len(json.dumps(request, ensure_ascii=False))
    assert worst_case_tokens(request) == (-(-chars // 3), 300)
    assert worst_case_tokens({"messages": MESSAGES})[1] == DEFAULT_MAX_OUTPUT_TOKENS
    with_tools = {**request, "toolConfig": {"tools": [{"toolSpec": {"name": "t", "inputSchema": {"json": {}}}}]}}
    assert worst_case_tokens(with_tools)[0] > worst_case_tokens(request)[0] + TOOL_OVERHEAD_TOKENS - 1
    assert worst_case_tokens({**request, "modelId": "x" * 300}) == worst_case_tokens(request)


# ---------------------------------------------------------------------------- router + ledger


def test_a_call_is_priced_and_persisted(stubbed: tuple[Any, Stubber], isolated_ledger: Path) -> None:
    client, stubber = stubbed
    stubber.add_response("converse", reply(inputs=1000, outputs=500), {"modelId": NOVA_LITE_APAC, "messages": MESSAGES})
    router = router_for(client, SpendLedger.from_env(), {"r": (NOVA_LITE_APAC,)})
    result = router.converse("r", MESSAGES)
    in_price, out_price = PRICES_PER_MTOK[NOVA_LITE_APAC]
    expected = (1000 * in_price + 500 * out_price) / 1e6
    assert result.cost_usd == pytest.approx(expected)
    saved = json.loads(isolated_ledger.read_text())
    assert saved["total_usd"] == pytest.approx(expected) and saved["calls"] == 1
    assert saved["models"][NOVA_LITE_APAC] == {
        "calls": 1,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cost_usd": pytest.approx(expected),
    }
    assert saved["reservations"] == {}
    # A fresh ledger (another process, a restart) sees the same total.
    assert SpendLedger(isolated_ledger).spent_usd == pytest.approx(expected)


def test_the_router_uses_the_env_ledger_by_default(stubbed: tuple[Any, Stubber], isolated_ledger: Path) -> None:
    client, stubber = stubbed
    stubber.add_response("converse", reply(inputs=10, outputs=5), {"modelId": NOVA_LITE_APAC, "messages": MESSAGES})
    router = BedrockRouter(client, RouterConfig(roles={"r": (NOVA_LITE_APAC,)}))
    assert router.ledger is not None and router.ledger.path == isolated_ledger and router.ledger.budget_usd == 5.0
    router.converse("r", MESSAGES)
    assert isolated_ledger.exists()
    assert BedrockRouter(client, ledger=None).ledger is None  # explicit opt-out


def test_budget_exceeded_raises_before_calling(stubbed: tuple[Any, Stubber], isolated_ledger: Path) -> None:
    client, _stubber = stubbed  # no responses queued: any real call would make the Stubber fail
    ledger = SpendLedger(isolated_ledger, budget_usd=0.01)
    router = router_for(client, ledger, {"r": (SONNET_AU,)})
    with pytest.raises(BudgetExceeded) as caught:
        router.converse("r", MESSAGES)  # no maxTokens: 8192 Sonnet output tokens alone cost ~$0.14
    err = caught.value
    assert err.model_id == SONNET_AU and err.estimate_usd > 0.01 and err.spent_usd == 0
    assert "Nothing was sent" in str(err) and "ASKONCE_BEDROCK_BUDGET_USD" in str(err)
    assert ledger.snapshot()["reserved_usd"] == 0 and ledger.spent_usd == 0
    assert router.metrics.snapshot() == {}  # not counted as a model error


def test_a_smaller_max_tokens_fits_under_the_remaining_budget(
    stubbed: tuple[Any, Stubber], isolated_ledger: Path
) -> None:
    client, stubber = stubbed
    ledger = SpendLedger(isolated_ledger, budget_usd=0.05)
    ledger.record(SONNET_AU, {"inputTokens": 2000, "outputTokens": 2000})  # ~$0.041 already spent
    router = router_for(client, ledger, {"r": (SONNET_AU,)})
    with pytest.raises(BudgetExceeded):
        router.converse("r", MESSAGES, inference_config={"maxTokens": 1000})  # worst case ~$0.017
    router = router_for(client, ledger, {"r": (SONNET_AU,)})  # fresh token bucket
    stubber.add_response(
        "converse",
        reply(inputs=12, outputs=40),
        {"modelId": SONNET_AU, "messages": MESSAGES, "inferenceConfig": {"maxTokens": 100}},
    )
    result = router.converse("r", MESSAGES, inference_config={"maxTokens": 100})  # worst case ~$0.0017
    assert result.cost_usd is not None and ledger.spent_usd < 0.05


def test_an_exhausted_budget_blocks_raw_runs_too(stubbed: tuple[Any, Stubber], isolated_ledger: Path) -> None:
    client, _stubber = stubbed
    ledger = SpendLedger(isolated_ledger, budget_usd=0.001)
    ledger.record(NOVA_LITE_APAC, {"inputTokens": 20_000})  # $0.0016 > budget
    router = router_for(client, ledger, {"r": (NOVA_LITE_APAC,)})
    with pytest.raises(BudgetExceeded):
        router.run("r", lambda c, m: c.converse(modelId=m, messages=MESSAGES))  # no worst_case given


def test_failed_attempts_release_their_reservation(stubbed: tuple[Any, Stubber], isolated_ledger: Path) -> None:
    client, stubber = stubbed
    stubber.add_client_error(
        "converse",
        service_error_code="ThrottlingException",
        service_message="slow down",
        http_status_code=429,
        expected_params={"modelId": NOVA_PRO_APAC, "messages": MESSAGES},
    )
    stubber.add_response(
        "converse", reply(inputs=100, outputs=10), {"modelId": NOVA_2_LITE_GLOBAL, "messages": MESSAGES}
    )
    stubber.add_client_error(
        "converse",
        service_error_code="ValidationException",
        service_message="messages: malformed",
        expected_params={"modelId": NOVA_LITE_APAC, "messages": MESSAGES},
    )
    ledger = SpendLedger(isolated_ledger)
    router = router_for(client, ledger, {"r": (NOVA_PRO_APAC, NOVA_2_LITE_GLOBAL), "bad": (NOVA_LITE_APAC,)})
    result = router.converse("r", MESSAGES)
    assert result.model_id == NOVA_2_LITE_GLOBAL
    with pytest.raises(Exception, match="malformed"):
        router.converse("bad", MESSAGES)
    snap = ledger.snapshot()
    assert snap["reserved_usd"] == 0 and snap["calls"] == 1  # only the successful call is billed
    assert set(snap["models"]) == {NOVA_2_LITE_GLOBAL}


def test_interrupted_call_releases_its_reservation(isolated_ledger: Path) -> None:
    ledger = SpendLedger(isolated_ledger)

    def interrupted(_client: Any, _model_id: str) -> dict[str, Any]:
        raise KeyboardInterrupt

    router = router_for(object(), ledger, {"r": (NOVA_LITE_APAC,)})
    with pytest.raises(KeyboardInterrupt):
        router.run("r", interrupted, worst_case=(100, 100))
    assert ledger.snapshot()["reserved_usd"] == 0


def test_a_corrupt_ledger_is_never_reset(stubbed: tuple[Any, Stubber], isolated_ledger: Path) -> None:
    client, _stubber = stubbed
    isolated_ledger.write_text("{not json")
    router = router_for(client, SpendLedger(isolated_ledger), {"r": (NOVA_LITE_APAC,)})
    with pytest.raises(LedgerError, match="fix or move it by hand"):
        router.converse("r", MESSAGES)
    assert isolated_ledger.read_text() == "{not json"


# ---------------------------------------------------------------------------- concurrency


def test_concurrent_reservations_never_pass_the_cap(isolated_ledger: Path) -> None:
    ledger = SpendLedger(isolated_ledger, budget_usd=1.0)
    one = ledger.estimate(SONNET_AU, 0, 10_000)  # $0.17 each: at most 5 fit under $1.00
    granted: list[Any] = []
    refused: list[BudgetExceeded] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            r = SpendLedger(isolated_ledger, budget_usd=1.0).reserve(SONNET_AU, 0, 10_000)
            with lock:
                granted.append(r)
        except BudgetExceeded as exc:
            with lock:
                refused.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == int(1.0 // one) == 5 and len(refused) == 7
    assert ledger.snapshot()["reserved_usd"] == pytest.approx(5 * one)
    for r in granted:
        ledger.release(r)
    assert ledger.snapshot()["reserved_usd"] == 0


def _record_many(path: str, n: int) -> None:
    ledger = SpendLedger(path)
    for _ in range(n):
        ledger.record(NOVA_LITE_APAC, {"inputTokens": 1000, "outputTokens": 1000})


def test_threads_and_processes_do_not_lose_updates(isolated_ledger: Path) -> None:
    threads = [threading.Thread(target=_record_many, args=(str(isolated_ledger), 10)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with ProcessPoolExecutor(max_workers=3) as pool:
        list(pool.map(_record_many, [str(isolated_ledger)] * 3, [10] * 3))
    snap = SpendLedger(isolated_ledger).snapshot()
    assert snap["calls"] == 70 and snap["models"][NOVA_LITE_APAC]["calls"] == 70
    assert snap["spent_usd"] == pytest.approx(
        70 * cost_usd(NOVA_LITE_APAC, {"inputTokens": 1000, "outputTokens": 1000})
    )


def test_reservations_of_dead_processes_are_dropped(isolated_ledger: Path) -> None:
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    data = {
        "version": 1,
        "total_usd": 0.0,
        "calls": 0,
        "models": {},
        "reservations": {
            "dead": {"model_id": SONNET_AU, "usd": 4.0, "pid": int(dead.stdout), "at": 0},
            "old": {"model_id": SONNET_AU, "usd": 0.5, "pid": os.getpid(), "at": 0},
        },
    }
    isolated_ledger.write_text(json.dumps(data))
    snap = SpendLedger(isolated_ledger).snapshot()
    assert snap["reserved_usd"] == 0 and snap["remaining_usd"] == 5.0


# ---------------------------------------------------------------------------- env + CLI


def test_from_env_and_default_path(monkeypatch: pytest.MonkeyPatch, isolated_ledger: Path) -> None:
    monkeypatch.setenv("ASKONCE_BEDROCK_BUDGET_USD", "0.25")
    ledger = SpendLedger.from_env()
    assert ledger.path == isolated_ledger and ledger.budget_usd == 0.25
    default = default_ledger_path()
    assert default.parts[-2:] == (".local", "bedrock-spend.json") and (default.parent.parent / ".git").exists()


def test_spend_cli(capsys: pytest.CaptureFixture[str], isolated_ledger: Path) -> None:
    ledger = SpendLedger(isolated_ledger)
    ledger.record(NOVA_PRO_APAC, {"inputTokens": 100_000, "outputTokens": 10_000})  # $0.14
    ledger.record(NOVA_LITE_APAC, {"inputTokens": 1000, "outputTokens": 100})
    assert spend_main([]) == 0
    out = capsys.readouterr().out
    assert "spent:     $0.1401" in out and "over 2 call(s)" in out and "remaining: $4.8599" in out
    assert NOVA_PRO_APAC in out and NOVA_LITE_APAC in out
    assert spend_main(["--json"]) == 0
    snap = json.loads(capsys.readouterr().out)
    assert snap["calls"] == 2 and snap["budget_usd"] == 5.0


def test_spend_cli_as_a_module(isolated_ledger: Path) -> None:
    env = {**os.environ, "PYTHONPATH": str(PACKAGE_SRC)}
    done = subprocess.run(
        [sys.executable, "-m", "askonce_bedrock.spend"], capture_output=True, text=True, env=env, check=True
    )
    assert "remaining: $5.0000" in done.stdout and str(isolated_ledger) in done.stdout
    assert "RuntimeWarning" not in done.stderr
