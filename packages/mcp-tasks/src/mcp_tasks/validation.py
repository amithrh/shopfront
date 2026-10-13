"""Checks an accepted form elicitation answer against the form's `requestedSchema`.

`tasks/update` carries answers to questions the server asked through `input_required`. The generic
`InputResponses` shape says nothing about the content, so without this check a client could answer
a one-line form with megabytes of text, unrequested keys or the wrong types, and the server would
store it verbatim. MCP form elicitation schemas are flat objects of primitive properties, so a small
validator for that subset is enough (no `jsonschema` dependency):

  * `string`: `minLength`, `maxLength`, `enum`, `oneOf[{const}]`;
  * `number` / `integer`: `minimum`, `maximum` (booleans are not numbers);
  * `boolean`;
  * `array` of enum strings: `minItems`, `maxItems`, `items.enum` / `items.anyOf[{const}]`;
  * `required`, and no properties beyond the schema's.

Every string is also capped at `MAX_STRING_LENGTH` when the schema sets no `maxLength`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAX_STRING_LENGTH = 10_000
"""Default cap for a string answer whose schema has no `maxLength`."""


class InvalidContent(ValueError):
    """An accepted answer does not match the form it answers."""


def _allowed_values(spec: Mapping[str, Any]) -> list[Any] | None:
    if isinstance(spec.get("enum"), list):
        return list(spec["enum"])
    for key in ("oneOf", "anyOf"):
        options = spec.get(key)
        if isinstance(options, list) and options and all(isinstance(o, Mapping) and "const" in o for o in options):
            return [o["const"] for o in options]
    return None


def _check_string(name: str, spec: Mapping[str, Any], value: Any) -> None:
    if not isinstance(value, str):
        raise InvalidContent(f"{name} must be a string")
    max_length = spec.get("maxLength")
    limit = max_length if isinstance(max_length, int) else MAX_STRING_LENGTH
    if len(value) > limit:
        raise InvalidContent(f"{name} is longer than {limit} characters")
    min_length = spec.get("minLength")
    if isinstance(min_length, int) and len(value) < min_length:
        raise InvalidContent(f"{name} is shorter than {min_length} characters")
    allowed = _allowed_values(spec)
    if allowed is not None and value not in allowed:
        raise InvalidContent(f"{name} is not one of the offered choices")


def _check_number(name: str, spec: Mapping[str, Any], value: Any, *, integer: bool) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or (integer and not isinstance(value, int)):
        raise InvalidContent(f"{name} must be {'an integer' if integer else 'a number'}")
    minimum, maximum = spec.get("minimum"), spec.get("maximum")
    if isinstance(minimum, int | float) and value < minimum:
        raise InvalidContent(f"{name} must be at least {minimum}")
    if isinstance(maximum, int | float) and value > maximum:
        raise InvalidContent(f"{name} must be at most {maximum}")


def _check_array(name: str, spec: Mapping[str, Any], value: Any) -> None:
    if not isinstance(value, list):
        raise InvalidContent(f"{name} must be a list")
    min_items, max_items = spec.get("minItems"), spec.get("maxItems")
    if isinstance(min_items, int) and len(value) < min_items:
        raise InvalidContent(f"{name} needs at least {min_items} choices")
    if isinstance(max_items, int) and len(value) > max_items:
        raise InvalidContent(f"{name} allows at most {max_items} choices")
    items = spec.get("items")
    allowed = _allowed_values(items) if isinstance(items, Mapping) else None
    for entry in value:
        if not isinstance(entry, str) or len(entry) > MAX_STRING_LENGTH:
            raise InvalidContent(f"{name} must be a list of strings")
        if allowed is not None and entry not in allowed:
            raise InvalidContent(f"{name} has a choice that was not offered")


def check_form_content(schema: Mapping[str, Any], content: Any) -> None:
    """Raise `InvalidContent` unless `content` answers the flat form `schema`."""
    if not isinstance(content, Mapping):
        raise InvalidContent("the answer must be an object")
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    extra = sorted(set(content) - set(properties))
    if extra:
        raise InvalidContent(f"unexpected fields {extra}")
    required = schema.get("required")
    for name in required if isinstance(required, list) else ():
        if content.get(name) is None:
            raise InvalidContent(f"{name} is required")
    for name, value in content.items():
        spec = properties[name]
        if value is None or not isinstance(spec, Mapping):
            continue
        kind = spec.get("type")
        if kind == "string":
            _check_string(name, spec, value)
        elif kind in ("number", "integer"):
            _check_number(name, spec, value, integer=kind == "integer")
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise InvalidContent(f"{name} must be true or false")
        elif kind == "array":
            _check_array(name, spec, value)
        elif isinstance(value, str):
            _check_string(name, spec, value)
        else:
            raise InvalidContent(f"{name} has an unsupported type")


def requested_schema(input_request: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The form schema of a wire-shaped `elicitation/create` input request, or None for other kinds."""
    if input_request.get("method") != "elicitation/create":
        return None
    params = input_request.get("params")
    if not isinstance(params, Mapping) or params.get("mode", "form") != "form":
        return None
    schema = params.get("requestedSchema")
    return schema if isinstance(schema, Mapping) else None
