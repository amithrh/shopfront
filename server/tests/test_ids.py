"""`ids`: derived ids and the 8-digit spoken booking code (VUX-11)."""

from __future__ import annotations

import secrets

import pytest

from shopfront_server.ids import BOOKING_CODE_DIGITS, IdFactory, new_id, reference_prefix, spoken_reference


def test_round2_misc_low_code_round_trips_to_the_id_prefix() -> None:
    assert spoken_reference("c_exyqratrum2x44qc423hhejvjm") == "04973073"
    assert reference_prefix("04973073") == "c_exyqr"
    factory = IdFactory(secrets.token_bytes(32))
    ids = [factory.commitment_id(f"key-{n}") for n in range(2000)] + [new_id("c") for _ in range(2000)]
    for commitment_id in ids:
        code = spoken_reference(commitment_id)
        assert code is not None and len(code) == BOOKING_CODE_DIGITS and code.isdigit()
        prefix = reference_prefix(code)
        assert prefix is not None and commitment_id.startswith(prefix) and len(prefix) == len("c_") + 5


@pytest.mark.parametrize("said", ["0497 3073", "0497-3073", " 04973073 ", "0497.3073", "0 4 9 7, 3 0 7 3"])
def test_round2_misc_low_code_spellings_people_use_are_accepted(said: str) -> None:
    assert reference_prefix(said) == "c_exyqr"


@pytest.mark.parametrize(
    "said",
    [
        "0497307",  # 7 digits
        "049730731",  # 9 digits
        "33554432",  # 32**5: no 5-character prefix has it
        "99999999",
        "0497 307a",  # letters
        "c_exyqratrum2x44qc423hhejvjm",  # a full reference stays a reference
        "c_2345672345abcdefghijklmnop",  # ... even one holding 8+ digits of its own
        "c_23456723abcdefghijklmnopqr",  # ... or exactly 8
        "#04973073",
        "",
        "   ",
        "-",
    ],
)
def test_round2_misc_low_anything_else_is_not_a_code(said: str) -> None:
    assert reference_prefix(said) is None


@pytest.mark.parametrize("commitment_id", ["c_abc", "t_exyqratrum2x44", "c_EXYQRATRUM", "c_exyq1", "cexyqratrum", "booking-1"])
def test_round2_misc_low_ids_without_the_derived_shape_have_no_code(commitment_id: str) -> None:
    assert spoken_reference(commitment_id) is None


def test_round2_misc_low_extreme_codes() -> None:
    assert spoken_reference("c_aaaaaxyz") == "00000000" and reference_prefix("00000000") == "c_aaaaa"
    assert spoken_reference("c_77777xyz") == f"{32**5 - 1:08d}" and reference_prefix("33554431") == "c_77777"
