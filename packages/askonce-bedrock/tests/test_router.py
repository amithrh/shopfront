"""BedrockRouter against a real bedrock-runtime client wired to botocore's Stubber (no network)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber

from askonce_bedrock import (
    DEFAULT_ROLES,
    BedrockRouter,
    RouterConfig,
    RouterExhausted,
    TokenBucket,
    classify,
    default_rpm,
)
from askonce_bedrock.router import (
    HAIKU_AU,
    HAIKU_GLOBAL,
    NOVA_2_LITE_GLOBAL,
    NOVA_LITE_APAC,
    NOVA_PRO_APAC,
    SONNET_AU,
    SONNET_GLOBAL,
)

MESSAGES = [{"role": "user", "content": [{"text": "Say hi"}]}]


class FakeTime:
    """Monotonic clock plus a sleep that only advances it (and records what was slept)."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def stubbed() -> Iterator[tuple[Any, Stubber]]:
    client = boto3.client(
        "bedrock-runtime", region_name="ap-southeast-2", aws_access_key_id="test", aws_secret_access_key="test"
    )
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def make_router(client: Any, fake_time: FakeTime, **config: Any) -> BedrockRouter:
    import random

    return BedrockRouter(
        client, RouterConfig(**config), clock=fake_time.clock, sleep=fake_time.sleep, rng=random.Random(7)
    )


def reply(text: str = "hi", *, inputs: int = 5, outputs: int = 2) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": inputs, "outputTokens": outputs, "totalTokens": inputs + outputs},
        "metrics": {"latencyMs": 12},
    }


def ok(stubber: Stubber, model_id: str, text: str = "hi") -> None:
    stubber.add_response("converse", reply(text), {"modelId": model_id, "messages": MESSAGES})


def fail(stubber: Stubber, model_id: str, code: str, message: str = "nope", status: int = 400) -> None:
    stubber.add_client_error(
        "converse",
        service_error_code=code,
        service_message=message,
        http_status_code=status,
        expected_params={"modelId": model_id, "messages": MESSAGES},
    )


# ---------------------------------------------------------------------------- routing


def test_first_model_answers_and_metrics_count_it(stubbed: tuple[Any, Stubber], fake_time: FakeTime) -> None:
    client, stubber = stubbed
    ok(stubber, SONNET_AU, "hello there")
    router = make_router(client, fake_time)
    result = router.converse("host", MESSAGES)
    assert result.model_id == SONNET_AU and result.text == "hello there" and result.role == "host"
    assert [a.outcome for a in result.attempts] == ["ok"]
    stats = router.metrics.snapshot()[SONNET_AU]
    assert stats["calls"] == 1 and stats["successes"] == 1 and stats["throttles"] == 0
    assert stats["input_tokens"] == 5 and stats["output_tokens"] == 2


def test_throttling_fails_over_and_cools_the_model_down(stubbed: tuple[Any, Stubber], fake_time: FakeTime) -> None:
    client, stubber = stubbed
    fail(stubber, SONNET_AU, "ThrottlingException", "Too many requests", 429)
    ok(stubber, SONNET_GLOBAL)
    ok(stubber, HAIKU_AU)  # the next call: Sonnet au is cooling down, Sonnet global has no token left
    router = make_router(client, fake_time)
    first = router.converse("host", MESSAGES)
    assert first.model_id == SONNET_GLOBAL
    assert [(a.model_id, a.outcome) for a in first.attempts] == [(SONNET_AU, "throttled"), (SONNET_GLOBAL, "ok")]
    assert fake_time.slept == []  # failover is immediate, no sleeping
    second = router.converse("host", MESSAGES)
    assert second.model_id == HAIKU_AU
    snap = router.metrics.snapshot()
    assert snap[SONNET_AU]["throttles"] == 1 and snap[SONNET_AU]["successes"] == 0
    assert snap[SONNET_GLOBAL]["successes"] == 1 and snap[HAIKU_AU]["successes"] == 1


def test_token_bucket_spreads_a_burst_across_the_role(stubbed: tuple[Any, Stubber], fake_time: FakeTime) -> None:
    """Burst 1: back-to-back calls each take the next model with a token, without a single throttle."""
    client, stubber = stubbed
    order = DEFAULT_ROLES["simulate"]
    for model_id in order:
        ok(stubber, model_id)
    router = make_router(client, fake_time)
    answered = [router.converse("simulate", MESSAGES).model_id for _ in order]
    assert answered == list(order) and fake_time.slept == []


def test_waits_for_the_next_token_when_every_model_is_spent(stubbed: tuple[Any, Stubber], fake_time: FakeTime) -> None:
    client, stubber = stubbed
    ok(stubber, NOVA_LITE_APAC)
    ok(stubber, NOVA_LITE_APAC)
    router = make_router(client, fake_time, roles={"cheap": (NOVA_LITE_APAC,)})
    router.converse("cheap", MESSAGES)
    router.converse("cheap", MESSAGES)
    assert sum(fake_time.slept) == pytest.approx(60 / 18)  # Nova's default 18 RPM: one token per 3.33 s


def test_exhausted_when_capacity_does_not_come_back_in_time(stubbed: tuple[Any, Stubber], fake_time: FakeTime) -> None:
    client, stubber = stubbed
    fail(stubber, HAIKU_AU, "ThrottlingException", "slow down", 429)
    fail(stubber, HAIKU_GLOBAL, "ServiceQuotaExceededException", "quota", 429)
    router = make_router(client, fake_time, roles={"r": (HAIKU_AU, HAIKU_GLOBAL)}, max_wait_s=1.0)
    with pytest.raises(RouterExhausted) as caught:
        router.converse("r", MESSAGES)
    assert [a.outcome for a in caught.value.attempts] == ["throttled", "throttled"]
    assert router.metrics.get(HAIKU_AU).throttles == 1 and router.metrics.get(HAIKU_GLOBAL).throttles == 1


def test_throttled_everywhere_backs_off_then_succeeds(stubbed: tuple[Any, Stubber], fake_time: FakeTime) -> None:
    client, stubber = stubbed
    fail(stubber, HAIKU_AU, "ThrottlingException", "slow down", 429)
    fail(stubber, HAIKU_GLOBAL, "ThrottlingException", "slow down", 429)
    ok(stubber, HAIKU_AU)
    router = make_router(client, fake_time, roles={"r": (HAIKU_AU, HAIKU_GLOBAL)}, max_wait_s=120.0)
    result = router.converse("r", MESSAGES)
    assert result.model_id in (HAIKU_AU, HAIKU_GLOBAL) and fake_time.slept
    assert [a.outcome for a in result.attempts] == ["throttled", "throttled", "ok"]
    # The wait honoured both the backoff and Claude's 9 RPM bucket (one token per 6.67 s).
    assert sum(fake_time.slept) >= 60 / 9 - 1e-9


def test_unusable_model_is_parked(stubbed: tuple[Any, Stubber], fake_time: FakeTime) -> None:
    client, stubber = stubbed
    fail(stubber, SONNET_AU, "AccessDeniedException", "not available for this account", 403)
    ok(stubber, SONNET_GLOBAL)
    ok(stubber, SONNET_GLOBAL)
    router = make_router(client, fake_time)
    assert router.converse("extract", MESSAGES).model_id == SONNET_GLOBAL
    fake_time.now += 30.0  # buckets refill; the parked model stays parked (600 s)
    assert router.converse("extract", MESSAGES).model_id == SONNET_GLOBAL
    assert router.metrics.get(SONNET_AU).unavailable == 1 and router.metrics.get(SONNET_AU).calls == 1


def test_bad_model_id_validation_fails_over(stubbed: tuple[Any, Stubber], fake_time: FakeTime) -> None:
    client, stubber = stubbed
    fail(stubber, NOVA_PRO_APAC, "ValidationException", "The provided model identifier is invalid.")
    ok(stubber, NOVA_2_LITE_GLOBAL)
    router = make_router(client, fake_time, roles={"r": (NOVA_PRO_APAC, NOVA_2_LITE_GLOBAL)})
    assert router.converse("r", MESSAGES).model_id == NOVA_2_LITE_GLOBAL


def test_an_invalid_request_raises_without_failover(stubbed: tuple[Any, Stubber], fake_time: FakeTime) -> None:
    client, stubber = stubbed
    fail(stubber, SONNET_AU, "ValidationException", "messages: a conversation must start with a user message")
    router = make_router(client, fake_time)
    with pytest.raises(Exception) as caught:
        router.converse("host", MESSAGES)
    assert classify(caught.value) == "fatal"
    assert router.metrics.get(SONNET_AU).errors == 1  # and no other model was called (Stubber checks)


def test_transient_server_errors_fail_over(stubbed: tuple[Any, Stubber], fake_time: FakeTime) -> None:
    client, stubber = stubbed
    fail(stubber, HAIKU_AU, "ServiceUnavailableException", "busy", 503)
    ok(stubber, HAIKU_GLOBAL)
    router = make_router(client, fake_time)
    result = router.converse("simulate", MESSAGES)
    assert result.model_id == HAIKU_GLOBAL and result.attempts[0].outcome == "ServiceUnavailableException"


def test_converse_passes_system_and_config_through(stubbed: tuple[Any, Stubber], fake_time: FakeTime) -> None:
    client, stubber = stubbed
    stubber.add_response(
        "converse",
        reply(),
        {
            "modelId": SONNET_AU,
            "messages": MESSAGES,
            "system": [{"text": "Be brief."}],
            "inferenceConfig": {"maxTokens": 16, "temperature": 0},
        },
    )
    router = make_router(client, fake_time)
    router.converse("host", MESSAGES, system="Be brief.", inference_config={"maxTokens": 16, "temperature": 0})


def test_unknown_role(stubbed: tuple[Any, Stubber], fake_time: FakeTime) -> None:
    client, _stubber = stubbed
    with pytest.raises(KeyError, match="unknown Bedrock role"):
        make_router(client, fake_time).converse("nope", MESSAGES)


# ---------------------------------------------------------------------------- pieces


def test_default_budgets() -> None:
    assert default_rpm(SONNET_AU) == default_rpm(HAIKU_GLOBAL) == 9
    assert default_rpm(NOVA_LITE_APAC) == default_rpm(NOVA_2_LITE_GLOBAL) == default_rpm(NOVA_PRO_APAC) == 18
    assert default_rpm("meta.llama3-70b-instruct-v1:0") == 9  # unknown families get the cautious budget


def test_token_bucket_refills_at_rpm(fake_time: FakeTime) -> None:
    bucket = TokenBucket(9, burst=1, clock=fake_time.clock)
    assert bucket.try_acquire() and not bucket.try_acquire()
    assert bucket.wait_time() == pytest.approx(60 / 9)
    fake_time.now += 60 / 9
    assert bucket.try_acquire()
    with pytest.raises(ValueError):
        TokenBucket(0)


def test_config_from_env() -> None:
    config = RouterConfig.from_env(
        {
            "ASKONCE_BEDROCK_ROLE_HOST": f"{HAIKU_AU}, {NOVA_LITE_APAC}",
            "ASKONCE_BEDROCK_ROLE_JUDGE": SONNET_GLOBAL,
            "ASKONCE_BEDROCK_CLAUDE_RPM": "20",
            "ASKONCE_BEDROCK_RPM": f"{NOVA_LITE_APAC}=40, {SONNET_AU}=5",
            "ASKONCE_BEDROCK_BURST": "2",
            "ASKONCE_BEDROCK_MAX_WAIT_S": "15",
        }
    )
    assert config.roles["host"] == (HAIKU_AU, NOVA_LITE_APAC) and config.roles["judge"] == (SONNET_GLOBAL,)
    assert config.roles["extract"] == DEFAULT_ROLES["extract"]
    assert config.rpm_for(HAIKU_GLOBAL) == 20 and config.rpm_for(SONNET_AU) == 5
    assert config.rpm_for(NOVA_LITE_APAC) == 40 and config.rpm_for(NOVA_PRO_APAC) == 18
    assert config.burst == 2 and config.max_wait_s == 15
    with pytest.raises(ValueError):
        RouterConfig.from_env({"ASKONCE_BEDROCK_RPM": "no-equals-sign"})


def test_from_env_builds_a_client_in_the_project_region(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test")
    router = BedrockRouter.from_env(session=session)
    assert router.client.meta.region_name == "ap-southeast-2"
    assert router.client.meta.config.retries["total_max_attempts"] == 1  # the router does the retrying
    assert router.ledger is not None and router.ledger.budget_usd == 5.0  # the $5 cap is on by default
