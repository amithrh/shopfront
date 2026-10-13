"""Evaluate a transactional module's `approval` block against a planned request.

`mode: always` -> approval required (reason: a generic owner line built by the tools layer).
`mode: when_any_rule_matches` -> required if any rule matches. Rule semantics:

  when  : an intake key, or a built-in (union of the schema's two built-in lists):
            item              item ref, e.g. "menu/custom-cake"
            choices           list of full choice refs "<item ref>/<group id>/<choice id>",
                              e.g. "menu/custom-cake/size/10in-round"
            when              slot start (local ISO) or day (ISO date)
            price / order_total_cents   computed total in cents (None for quote_only)
            party_size        int
            visit_window      visit window id
            lead_minutes      minutes from now until the slot/day starts
  op    : > >= < <= == compare numbers (== also strings); `in` = value in list (for multi_choice
          intake values: any overlap); `present` = the key has a non-empty value
  A rule whose `when` value is unknown (not supplied) does not match; the missing-intake step asks for
  required fields before confirmation, so required keys are always known at commit time.

Comparisons are tolerant of representation, never of meaning: numeric strings compare as numbers,
strings compare case-insensitively, ISO dates/times compare as strings (which orders them correctly),
and a list-valued fact (choices, multi_choice) matches if any element does.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from shopfront_server.domain.models import ApprovalCheck
from shopfront_server.profile.model import JsonObj

BUILT_IN_KEYS: frozenset[str] = frozenset(
    {"item", "choices", "when", "price", "order_total_cents", "party_size", "visit_window", "lead_minutes"}
)

_COMPARISONS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


def evaluate_approval(
    module_id: str, module: JsonObj, facts: Mapping[str, Any], *, owner_module_id: str | None
) -> ApprovalCheck:
    """`facts` holds intake values plus the built-ins. Fact refs point at `<module>#approval.rules`
    (`<module>#approval.mode` for `mode: always`). A module without an `approval` block never needs one."""
    approval = module.get("approval")
    if not approval:
        return ApprovalCheck(required=False)
    owner = approval.get("module") or owner_module_id
    if approval.get("mode") == "always":
        return ApprovalCheck(required=True, owner_module_id=owner, fact_refs=(f"{module_id}#approval.mode",))
    matched = [rule for rule in approval.get("rules", ()) or () if rule_matches(rule, facts)]
    return ApprovalCheck(
        required=bool(matched),
        owner_module_id=owner,
        reasons_spoken=tuple(dict.fromkeys(str(rule["reason_spoken"]) for rule in matched if rule.get("reason_spoken"))),
        matched_rule_keys=tuple(dict.fromkeys(str(rule["when"]) for rule in matched)),
        fact_refs=(f"{module_id}#approval.rules",),
    )


def rule_matches(rule: JsonObj, facts: Mapping[str, Any]) -> bool:
    """One rule against the facts (see module docstring)."""
    op = rule.get("op")
    actual = facts.get(str(rule.get("when")))
    if op == "present":
        return _present(actual)
    if not _present(actual):
        return False
    expected = rule.get("value")
    values = list(actual) if isinstance(actual, list | tuple | set | frozenset) else [actual]
    if op == "in":
        allowed = list(expected) if isinstance(expected, list | tuple) else [expected]
        return any(_equal(value, option) for value in values for option in allowed)
    if op == "==":
        return any(_equal(value, expected) for value in values)
    compare = _COMPARISONS.get(str(op))
    if compare is None:
        return False
    for value in values:
        pair = _comparable(value, expected)
        if pair is not None and compare(*pair):
            return True
    return False


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list | tuple | set | frozenset | dict):
        return len(value) > 0
    return True


def _number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        return number if number.is_finite() else None
    if isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    return None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().casefold()
        if token in ("true", "yes"):
            return True
        if token in ("false", "no"):
            return False
    return None


def _equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        left, right = _boolean(actual), _boolean(expected)
        return left is not None and left == right
    left_number, right_number = _number(actual), _number(expected)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    if isinstance(actual, str) and isinstance(expected, str):
        return actual.strip().casefold() == expected.strip().casefold()
    return False


def _comparable(actual: Any, expected: Any) -> tuple[Any, Any] | None:
    left_number, right_number = _number(actual), _number(expected)
    if left_number is not None and right_number is not None:
        return left_number, right_number
    if isinstance(actual, str) and isinstance(expected, str):
        return actual.strip(), expected.strip()
    return None
