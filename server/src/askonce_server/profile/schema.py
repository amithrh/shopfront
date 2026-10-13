"""JSON Schema validation of a raw profile (Draft 2020-12 with format checking)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match, relevance

from askonce_server.profile.invariants import Violation

MAX_MESSAGE_CHARS = 400
"""jsonschema messages embed the failing instance, which can be a whole module: keep reports readable."""


@lru_cache(maxsize=4)
def load_schema(path: Path) -> Mapping[str, Any]:
    """Read and `check_schema` the profile JSON Schema (cached per path)."""
    schema = json.loads(Path(path).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema  # type: ignore[no-any-return]


@lru_cache(maxsize=4)
def _validator(path: Path) -> Draft202012Validator:
    return Draft202012Validator(load_schema(path), format_checker=Draft202012Validator.FORMAT_CHECKER)


def json_pointer(parts: Iterable[str | int]) -> str:
    """RFC 6901 pointer for a path (`""` is the document root)."""
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _short(message: str) -> str:
    return message if len(message) <= MAX_MESSAGE_CHARS else message[: MAX_MESSAGE_CHARS - 1] + "…"


def _violation(error: ValidationError) -> Violation:
    return Violation(
        invariant="schema", severity="error", where=json_pointer(error.absolute_path), message=_short(error.message)
    )


def validate_schema(raw: Mapping[str, Any], *, schema_path: Path) -> list[Violation]:
    """Every schema error as a `Violation(invariant="schema", severity="error")`, best match first.

    Uses `jsonschema.Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)`.
    The `path` of each violation is the JSON pointer of the failing instance.
    """
    errors = list(_validator(schema_path).iter_errors(raw))
    if not errors:
        return []
    best = best_match(errors)
    ordered = [best, *(e for e in sorted(errors, key=relevance, reverse=True) if e is not best)]
    return [_violation(error) for error in ordered]
