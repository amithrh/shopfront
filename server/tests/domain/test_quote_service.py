"""QuoteService against the gold home-services profile: service areas, visit windows, always-approval."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from askonce_server.domain.planning import CommitPlan
from askonce_server.errors import UnknownModule
from askonce_server.profile.factref import resolve
from askonce_server.profile.model import Profile

pytestmark = pytest.mark.anyio

Domain = Any  # the `domain` fixture (tests/domain/conftest.py)

DENVER = ZoneInfo("America/Denver")
JOB = {
    "description": "Slow leak under the kitchen sink",
    "zip": "80202",
    "name": "Sam Lee",
    "contact_by": "text",
    "phone": "+13035550100",
}


async def plan_quote(domain: Domain, home: Profile, **kwargs: Any) -> CommitPlan:
    args: dict[str, Any] = {
        "item": "leak repair",
        "intake": JOB,
        "principal": "anonymous",
        "day": "2026-10-16",
        "visit_window": "weekday morning",
    }
    args.update(kwargs)
    return await domain.quotes.plan(home, **args)


@pytest.mark.parametrize(
    ("postal_code", "locality", "served", "location"),
    [
        ("80202", None, True, "core-area"),
        ("80202-1234", None, True, "core-area"),
        (" 80214 ", None, True, "extended-area"),
        (None, "cherry creek", True, "core-area"),
        (None, "Lakewood", True, "extended-area"),
        ("80033", None, False, None),
        (None, "Boulder", False, None),
        (None, None, None, None),
        # unlisted: a "no" only where the profile says so (policies/outside-area) or in another region
        ("80010", None, False, None),
        ("90210", None, False, None),
        (None, "Aurora", False, None),
        ("80239", None, None, None),
        (None, "Denver", None, None),
        (None, "Stapleton", None, None),
        # how people write a listed place
        (None, "Lakewood, CO", True, "extended-area"),
        (None, "lakewood co", True, "extended-area"),
        (None, "Highlands", True, "core-area"),
        (None, "Cap Hill", True, "core-area"),
        (None, "Wash Park", True, "core-area"),
    ],
)
def test_service_area(
    domain: Domain, home: Profile, postal_code: str | None, locality: str | None, served: bool | None, location: str | None
) -> None:
    check = domain.quotes.check_service_area(home, postal_code=postal_code, locality=locality)
    assert (check.served, check.location_id, check.module_id) == (served, location, "quotes")
    for ref in check.fact_refs:
        assert resolve(home, ref).present, ref
    if served:
        assert check.fact_refs and check.matched


def test_radius_only_areas_cannot_be_decided(
    domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]
) -> None:
    doc = raw("kettlewick-home")
    for location in doc["business"]["locations"]:
        if location["kind"] == "service_area":
            location["area"] = {"radius": {"distance": 10, "unit": "mi", "from_location": "office"}}
    check = domain.quotes.check_service_area(build_profile(doc), postal_code="80033", locality=None)
    assert check.served is None and "locations/core-area#area.radius" in check.fact_refs


def test_service_area_needs_a_quote_module(domain: Domain, bakery: Profile) -> None:
    with pytest.raises(UnknownModule):
        domain.quotes.check_service_area(bakery, postal_code="97202", locality=None)


async def test_quote_plan(domain: Domain, home: Profile) -> None:
    plan = await plan_quote(domain, home)
    assert plan == await plan_quote(domain, home)
    assert plan.ok, (plan.problems, plan.missing)
    assert (plan.kind, plan.operation, plan.module_id, plan.item_ref) == ("quote_request", "quote_request", "quotes", "services/leak-repair")
    assert plan.approval.required and plan.approval.owner_module_id == "owner"
    assert plan.approval.fact_refs == ("quotes#approval.mode",)
    assert plan.visit_window_id == "weekday-morning"
    assert plan.window is not None
    assert plan.window.start == datetime(2026, 10, 16, 8, 0, tzinfo=DENVER) and plan.window.end.hour == 12
    assert plan.intake["job_type"] == "leak-repair" and plan.intake["time_window"] == "weekday-morning"
    assert plan.price is not None and (plan.price.kind, plan.price.amount_cents) == ("from", 15000)
    assert [item.key for item in plan.read_back] == ["job_type", "description", "zip", "time_window", "contact_by"]
    assert plan.say_before_confirm == ("policies/no-online-booking", "policies/quote-turnaround")
    for ref in (*plan.fact_refs, *(r for item in plan.read_back for r in item.fact_refs)):
        assert resolve(home, ref).present, ref


async def test_visit_window_from_intake_and_no_preference(domain: Domain, home: Profile) -> None:
    afternoon = await plan_quote(domain, home, visit_window=None, intake={**JOB, "time_window": "Weekday afternoon (arrival 12–4 pm)"})
    assert afternoon.visit_window_id == "weekday-afternoon"
    whenever = await plan_quote(domain, home, visit_window=None, day=None, intake={**JOB, "time_window": "first-available"})
    assert whenever.ok and whenever.visit_window_id is None and whenever.window is None and whenever.day is None
    unknown = await plan_quote(domain, home, visit_window="midnight")
    assert [(p.code, p.field) for p in unknown.problems] == [("unknown_visit_window", "visit_window")]


async def test_quote_problems(domain: Domain, home: Profile) -> None:
    outside = await plan_quote(domain, home, intake={**JOB, "zip": "80033"})
    assert [(p.code, p.field) for p in outside.problems] == [("outside_service_area", "zip")]
    unlisted = await plan_quote(domain, home, intake={**JOB, "zip": "80239"})  # policies/unlisted-zip: Jo decides
    assert unlisted.ok, unlisted.problems
    sunday = await plan_quote(domain, home, day="2026-10-18")
    assert [p.code for p in sunday.problems] == ["closed"]
    assert [a.day for a in sunday.problems[0].alternatives] == [date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21)]
    missing = await plan_quote(domain, home, intake={"zip": "80202"})
    assert [spec.key for spec in missing.missing] == ["description", "name", "contact_by", "phone"]
    unknown = await plan_quote(domain, home, item="roof replacement")
    assert [p.code for p in unknown.problems] == ["unknown_item"]


async def test_quote_only_jobs_carry_no_price(domain: Domain, home: Profile) -> None:
    plan = await plan_quote(domain, home, item="something else")
    assert plan.item_ref == "services/other-job" and plan.price is None and plan.intake["job_type"] == "other-job"


async def test_quote_requests_always_wait_for_the_owner(domain: Domain, home: Profile) -> None:
    plan = await plan_quote(domain, home)
    outcome = await domain.quotes.commit(home, plan, task_id="t_quote")
    assert outcome.commitment is not None
    assert outcome.commitment.status == "pending_owner" and outcome.commitment.kind == "quote_request"
    assert outcome.commitment.expires_at == domain.clock.now() + timedelta(minutes=2880)
    assert outcome.commitment.visit_window_id == "weekday-morning"
    assert domain.notifier.calls == [("kettlewick-home", outcome.commitment.commitment_id)]


async def test_quote_open_days(domain: Domain, home: Profile) -> None:
    days = await domain.quotes.open_days(home, item="water heater", limit=3)
    assert [d.day for d in days] == [date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16)]
