"""`profile.loader`, `profile.schema` and the gold profiles end to end."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from support import EXAMPLE_PROFILE, SCHEMA_PATH, make_live

from shopfront_server.canonical import profile_sha256
from shopfront_server.errors import ProfileInvalid
from shopfront_server.profile.invariants import LoadReport
from shopfront_server.profile.loader import LoadedProfile, load_profile, load_profile_file
from shopfront_server.profile.model import Profile
from shopfront_server.profile.schema import json_pointer, load_schema, validate_schema
from shopfront_server.profile.snapshots import SnapshotProvider

GOLD = ["thimbleberry-bakehouse", "quillfern-salon", "kettlewick-home"]


def _only_skipped_l3(report: LoadReport) -> bool:
    return all(v.invariant == "L3" and v.skipped for v in report.warnings)


# --------------------------------------------------------------------------- gold profiles


@pytest.mark.parametrize("name", GOLD)
def test_gold_profiles_load_lenient_with_zero_errors(name: str, gold_raw: Callable[[str], dict[str, Any]]) -> None:
    loaded = load_profile(gold_raw(name), schema_path=SCHEMA_PATH, strict=False)
    assert loaded.report.ok and loaded.report.errors == []
    assert loaded.profile.profile_id == name
    assert _only_skipped_l3(loaded.report)


@pytest.mark.parametrize("name", GOLD)
def test_gold_profiles_made_live_load_strict(name: str, gold_raw: Callable[[str], dict[str, Any]]) -> None:
    loaded = load_profile(make_live(gold_raw(name)), schema_path=SCHEMA_PATH, strict=True)
    assert loaded.profile.is_live
    assert _only_skipped_l3(loaded.report)  # only "no snapshot" warnings, nothing else


@pytest.mark.parametrize("name", GOLD)
def test_gold_profiles_pass_l3_against_the_demo_sites(
    name: str, gold_raw: Callable[[str], dict[str, Any]], site_snapshots: SnapshotProvider
) -> None:
    """Every web quote of every gold profile occurs in the committed page (strict, no warnings)."""
    loaded = load_profile(make_live(gold_raw(name)), schema_path=SCHEMA_PATH, strict=True, snapshots=site_snapshots)
    assert loaded.report.violations == []


def test_example_profile_loads_lenient(example_raw: dict[str, Any]) -> None:
    loaded = load_profile(example_raw, schema_path=SCHEMA_PATH, strict=False)
    assert loaded.report.errors == []
    assert {v.invariant for v in loaded.report.warnings} == {"L3", "L7"}  # illustrative hashes
    assert load_profile(make_live(example_raw), schema_path=SCHEMA_PATH, strict=True).report.ok


def test_example_profile_fails_strict_on_its_placeholder_binding(example_raw: dict[str, Any]) -> None:
    with pytest.raises(ProfileInvalid) as excinfo:
        load_profile(example_raw, schema_path=SCHEMA_PATH, strict=True)
    report: LoadReport = excinfo.value.report  # type: ignore[assignment]
    assert {v.invariant for v in report.errors} == {"L7"}


def test_conftest_profiles_fixture_uses_the_real_loader(profiles: list[Profile]) -> None:
    assert profiles and all(isinstance(p, Profile) and p.is_live for p in profiles)


# --------------------------------------------------------------------------- loader behaviour


def test_loaded_profile_carries_report_and_sha(example_raw: dict[str, Any]) -> None:
    loaded = load_profile(make_live(example_raw), schema_path=SCHEMA_PATH)
    assert isinstance(loaded, LoadedProfile)
    assert loaded.report.profile_id == "thimbleberry-bakehouse" and loaded.report.revision == 6
    assert loaded.report.sha256 == loaded.profile.sha256 == profile_sha256(make_live(example_raw))


def test_the_callers_dict_is_never_retained(example_raw: dict[str, Any]) -> None:
    doc = make_live(example_raw)
    loaded = load_profile(doc, schema_path=SCHEMA_PATH)
    doc["business"]["name"] = "Mutated"
    assert loaded.profile.name == "Thimbleberry Bakehouse"


def test_a_frozen_profile_can_be_reloaded(example_raw: dict[str, Any]) -> None:
    first = load_profile(make_live(example_raw), schema_path=SCHEMA_PATH).profile
    again = load_profile(first.raw, schema_path=SCHEMA_PATH).profile
    assert again.sha256 == first.sha256


def test_invalid_profile_raises_with_a_report(example_raw: dict[str, Any]) -> None:
    del example_raw["business"]["timezone"]
    with pytest.raises(ProfileInvalid) as excinfo:
        load_profile(example_raw, schema_path=SCHEMA_PATH)
    report: LoadReport = excinfo.value.report  # type: ignore[assignment]
    assert report.profile_id == "thimbleberry-bakehouse" and report.sha256 is None
    assert report.errors[0].invariant == "schema" and report.errors[0].where == "/business"
    assert "'thimbleberry-bakehouse' is invalid" in str(excinfo.value) and "timezone" in str(excinfo.value)
    assert excinfo.value.code == "profile_invalid"


@pytest.mark.parametrize("raw", [[], "profile", None, 42])
def test_non_objects_are_invalid(raw: Any) -> None:
    with pytest.raises(ProfileInvalid):
        load_profile(raw, schema_path=SCHEMA_PATH)


def test_load_profile_file(tmp_path: Path, example_raw: dict[str, Any]) -> None:
    path = tmp_path / "bakery.json"
    path.write_text(json.dumps(make_live(example_raw)), encoding="utf-8")
    assert load_profile_file(path, schema_path=SCHEMA_PATH).profile.profile_id == "thimbleberry-bakehouse"
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ProfileInvalid, match="not JSON"):
        load_profile_file(broken, schema_path=SCHEMA_PATH)


# --------------------------------------------------------------------------- schema


def test_schema_is_cached_and_valid() -> None:
    assert load_schema(SCHEMA_PATH) is load_schema(SCHEMA_PATH)
    assert load_schema(SCHEMA_PATH)["$id"].endswith("business-profile.schema.json")


def test_validate_schema_accepts_the_example() -> None:
    assert validate_schema(json.loads(EXAMPLE_PROFILE.read_text()), schema_path=SCHEMA_PATH) == []


def test_validate_schema_reports_json_pointers(example_raw: dict[str, Any]) -> None:
    example_raw["business"]["contact"]["phone"] = "555-0142"  # not E.164
    example_raw["modules"]["menu"]["items"][0]["invented"] = True  # strict core
    violations = validate_schema(example_raw, schema_path=SCHEMA_PATH)
    assert all(v.invariant == "schema" and v.severity == "error" for v in violations)
    wheres = {v.where for v in violations}
    assert "/business/contact/phone" in wheres
    assert any(w.startswith("/modules/menu") for w in wheres)
    assert all(len(v.message) <= 400 for v in violations)


def test_validate_schema_checks_formats(example_raw: dict[str, Any]) -> None:
    closures = example_raw["business"]["locations"][0]["hours"]["closures"]
    closures[0]["from"] = "2026-02-30"  # matches the date pattern, fails the `date` format
    violations = validate_schema(example_raw, schema_path=SCHEMA_PATH)
    assert [v.where for v in violations] == ["/business/locations/0/hours/closures/0/from"]


def test_json_pointer_escaping() -> None:
    assert json_pointer([]) == ""
    assert json_pointer(["modules", "a/b", "c~d", 0]) == "/modules/a~1b/c~0d/0"
