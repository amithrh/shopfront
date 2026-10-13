"""CustomOrderService against the gold bakery: choices, the pickup day, approval, per-day capacity.

Clock: Wed 2026-10-14 09:00 in Los Angeles. Custom cakes need 72h before a 10:00 pickup opening, so
Saturday 2026-10-17 is the first possible day; the bench takes two cakes a day.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import anyio
import pytest

from askonce_server.domain.planning import CommitOutcome, CommitPlan
from askonce_server.errors import UnknownItem
from askonce_server.profile.factref import resolve
from askonce_server.profile.model import Profile

pytestmark = pytest.mark.anyio

Domain = Any  # the `domain` fixture (tests/domain/conftest.py)

CAKE = "menu/custom-cake"
SUNDAY = "2026-10-18"
FULL_CHOICES = {"size": "ten inch round", "flavor": "chocolate", "filling": "jam", "frosting": "vanilla frosting"}
SMALL_CHOICES = {"size": "6 inch", "flavor": "vanilla", "filling": "caramel", "frosting": "cream cheese"}
CUSTOMER = {"customer_name": "Sam Lee", "customer_phone": "+12175550100"}


async def plan_cake(domain: Domain, bakery: Profile, **kwargs: Any) -> CommitPlan:
    args: dict[str, Any] = {
        "item": "custom cake",
        "choices": FULL_CHOICES,
        "day": SUNDAY,
        "intake": {"servings": 30, **CUSTOMER},
        "principal": "anonymous",
    }
    args.update(kwargs)
    return await domain.orders.plan(bakery, **args)


async def test_big_cake_plan(domain: Domain, bakery: Profile) -> None:
    plan = await plan_cake(domain, bakery)
    assert plan == await plan_cake(domain, bakery)  # deterministic across MRTR rounds
    assert plan.ok, (plan.problems, plan.missing)
    assert (plan.kind, plan.operation, plan.module_id, plan.item_ref) == ("custom_order", "custom_order", "custom-cakes", CAKE)
    assert plan.choices == {"size": "10in-round", "flavor": "dark-chocolate", "filling": "marionberry-jam", "frosting": "vanilla-buttercream"}
    assert plan.day == date(2026, 10, 18) and plan.intake["pickup_date"] == SUNDAY
    assert plan.price is not None and (plan.price.kind, plan.price.amount_cents) == ("fixed", 12000)
    assert plan.approval.required and plan.approval.owner_module_id == "rosa"
    assert plan.approval.matched_rule_keys == ("servings", "choices")
    assert [item.key for item in plan.read_back] == ["item", "choices", "pickup_date", "servings", "price"]
    assert plan.say_before_confirm == ("info/deposit", "info/cancellation")
    assert [(h.key, h.units, h.limit) for h in plan.holds] == [("CAP#thimbleberry-bakehouse#custom-cakes#DAY#2026-10-18", 1, 2)]
    for ref in (*plan.fact_refs, *(r for item in plan.read_back for r in item.fact_refs)):
        assert resolve(bakery, ref).present, ref


async def test_every_required_option_group_must_be_answered(domain: Domain, bakery: Profile) -> None:
    plan = await plan_cake(domain, bakery, choices={"size": "ten inch round"})
    assert [(p.code, p.field) for p in plan.problems] == [
        ("missing_choice", "flavor"),
        ("missing_choice", "filling"),
        ("missing_choice", "frosting"),
    ]
    assert plan.problems[0].fact_ref == f"{CAKE}/flavor#spoken_question"
    assert resolve(bakery, plan.problems[0].fact_ref).present


@pytest.mark.parametrize(
    ("given", "group", "expected"),
    [
        ({"sponge": "gluten free vanilla"}, "flavor", "vanilla-bean-gf"),  # key names no group: matched by value
        ({"Cake flavor": "funfetti"}, "flavor", "birthday-sprinkle"),  # group name + synonym
        ({"extras": "flowers and fresh fruit"}, "extras", "buttercream-flowers,fresh-fruit"),
        ({"extras": ["fruit"]}, "extras", "fresh-fruit"),
    ],
)
async def test_choice_resolution(domain: Domain, bakery: Profile, given: dict[str, Any], group: str, expected: str) -> None:
    base = {k: v for k, v in FULL_CHOICES.items() if k != group}
    plan = await plan_cake(domain, bakery, choices={**base, **given})
    assert plan.problems == ()
    assert plan.choices[group] == expected


async def test_invalid_choices(domain: Domain, bakery: Profile) -> None:
    plan = await plan_cake(domain, bakery, choices={**FULL_CHOICES, "flavor": "pistachio", "topping": "glitter"})
    assert sorted((p.code, p.field) for p in plan.problems) == [("invalid_choice", "flavor"), ("invalid_choice", "topping")]


async def test_price_includes_per_tier_and_extra_deltas(domain: Domain, bakery: Profile) -> None:
    plan = await plan_cake(
        domain, bakery, choices={**FULL_CHOICES, "size": "two tier", "flavor": "gluten free chocolate", "extras": "flowers"}
    )
    assert plan.price is not None and plan.price.amount_cents == 21000 + 2 * 1200 + 1500


async def test_pickup_day_from_intake_or_argument(domain: Domain, bakery: Profile) -> None:
    from_intake = await plan_cake(domain, bakery, day=None, intake={"servings": 12, "pickup_date": "2026-10-17", **CUSTOMER})
    assert from_intake.day == date(2026, 10, 17)
    argument_wins = await plan_cake(domain, bakery, day=SUNDAY, intake={"servings": 12, "pickup_date": "2026-10-17", **CUSTOMER})
    assert argument_wins.day == date(2026, 10, 18) and argument_wins.intake["pickup_date"] == SUNDAY
    neither = await plan_cake(domain, bakery, day=None, intake={"servings": 12, **CUSTOMER})
    assert [spec.key for spec in neither.missing] == ["pickup_date"] and neither.problems == ()
    garbled = await plan_cake(domain, bakery, day="next sunday")
    assert [(p.code, p.field) for p in garbled.problems] == [("invalid_intake", "date")]


async def test_module_without_a_date_field_asks_for_one(
    domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]
) -> None:
    doc = raw("thimbleberry-bakehouse")
    module = doc["modules"]["custom-cakes"]
    module["intake_fields"] = [f for f in module["intake_fields"] if f["key"] != "pickup_date"]
    module["confirmation"]["read_back"] = ["item", "when", "price"]
    profile = build_profile(doc)
    asked = await plan_cake(domain, profile, day=None)
    assert [spec.key for spec in asked.missing] == ["date"]
    answered = await plan_cake(domain, profile, day=None, intake={"servings": 30, "date": SUNDAY, **CUSTOMER})
    assert answered.ok and answered.day == date(2026, 10, 18)
    assert [(item.key, item.value) for item in answered.read_back][1] == ("when", SUNDAY)


async def test_day_problems_offer_open_days(domain: Domain, bakery: Profile) -> None:
    friday = await plan_cake(domain, bakery, day="2026-10-16")
    assert [p.code for p in friday.problems] == ["lead_time"]
    assert [a.day.isoformat() for a in friday.problems[0].alternatives] == ["2026-10-17", "2026-10-18", "2026-10-20"]
    monday = await plan_cake(domain, bakery, day="2026-10-19")
    assert [p.code for p in monday.problems] == ["closed"]
    assert [a.day.isoformat() for a in monday.problems[0].alternatives] == ["2026-10-20", "2026-10-21", "2026-10-22"]


async def test_small_cake_is_confirmed_big_cake_waits_for_rosa(domain: Domain, bakery: Profile) -> None:
    small = await plan_cake(domain, bakery, choices=SMALL_CHOICES, intake={"servings": 12, **CUSTOMER})
    assert small.ok and not small.approval.required
    confirmed = await domain.orders.commit(bakery, small, task_id=None)
    assert confirmed.commitment is not None and confirmed.commitment.status == "confirmed"
    assert domain.notifier.calls == []

    big = await plan_cake(domain, bakery)
    pending = await domain.orders.commit(bakery, big, task_id=domain.ctx.ids.task_id(big.idempotency_key))
    assert pending.commitment is not None
    assert pending.commitment.status == "pending_owner"
    assert pending.commitment.task_id == domain.ctx.ids.task_id(big.idempotency_key)
    assert pending.commitment.expires_at == domain.clock.now() + timedelta(minutes=2880)
    assert pending.commitment.approval.reasons_spoken == big.approval.reasons_spoken
    assert domain.notifier.calls == [("thimbleberry-bakehouse", pending.commitment.commitment_id)]


async def test_two_cakes_a_day_then_capacity_full(domain: Domain, bakery: Profile) -> None:
    for principal in ("ann", "bob"):
        outcome = await domain.orders.commit(bakery, await plan_cake(domain, bakery, principal=principal), task_id=None)
        assert outcome.commitment is not None
    third = await plan_cake(domain, bakery, principal="cy")
    assert [p.code for p in third.problems] == ["capacity_full"]
    assert [a.day.isoformat() for a in third.problems[0].alternatives] == ["2026-10-20", "2026-10-21", "2026-10-22"]
    days = await domain.orders.open_days(bakery, item="birthday cake", limit=2)
    assert [(d.day.isoformat(), d.remaining) for d in days] == [("2026-10-17", 2), ("2026-10-20", 2)]
    # A retry of an order that already holds one of the two places still replays.
    retry = await plan_cake(domain, bakery, principal="ann")
    assert retry.ok and (await domain.orders.commit(bakery, retry, task_id=None)).replayed


async def test_ten_customers_two_places(domain: Domain, bakery: Profile) -> None:
    plans = [await plan_cake(domain, bakery, principal=f"customer-{n}") for n in range(10)]
    outcomes: list[CommitOutcome] = []

    async def commit(plan: CommitPlan) -> None:
        outcomes.append(await domain.orders.commit(bakery, plan, task_id=None))

    async with anyio.create_task_group() as tg:
        for plan in plans:
            tg.start_soon(commit, plan)
    assert sum(o.commitment is not None for o in outcomes) == 2
    assert {o.problem.code for o in outcomes if o.problem} == {"capacity_full"}
    assert domain.state.capacity["CAP#thimbleberry-bakehouse#custom-cakes#DAY#2026-10-18"] == 2


async def test_unknown_items(domain: Domain, bakery: Profile) -> None:
    unknown = await plan_cake(domain, bakery, item="moon cake")
    assert [p.code for p in unknown.problems] == ["unknown_item"] and unknown.module_id == "custom-cakes"
    not_custom = await plan_cake(domain, bakery, item="baguette")
    assert [p.code for p in not_custom.problems] == ["not_offered"]
    with pytest.raises(UnknownItem):
        await domain.orders.open_days(bakery, item="baguette")
