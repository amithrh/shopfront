"""Tool schemas are portable: every MCP client can load every tool (regression for the MCP Inspector
"schema portability" warning on every tool).

pydantic spells `Any` / `JsonValue` as the empty schema `{}` (or a bare `true`), which accepts anything
and carries no validation keyword; the Inspector flags it and some clients refuse such a tool outright
(`outputSchema.$defs.JsonValue` was on every tool through `Fact.value`). The walker below fails on that
and on the other constructs that do not survive translation into the JSON Schema subsets clients and
model providers accept (OpenAPI-style function declarations): type arrays, remote or dangling `$ref`s,
nested `$defs`, conditionals, tuple items and the like. It also checks that every `default` validates
against its own subschema, so a client that pre-fills defaults never produces an invalid call.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import jsonschema
import pytest
from support import ElicitationScript
from tools_support import BAKERY, HOME, SALON, Stack, structured

from askonce_server.tools.schemas import Fact, TaskStatusResult

pytestmark = pytest.mark.anyio

TYPED = frozenset({"type", "enum", "const", "$ref", "anyOf", "oneOf", "allOf"})
"""A subschema must say what it accepts with at least one of these."""

BOOLEAN_OK = frozenset({"additionalProperties", "unevaluatedProperties", "additionalItems", "unevaluatedItems"})
"""Where a bare `false` is the conventional spelling ("nothing else"). A bare `true` is never allowed."""

NON_PORTABLE = frozenset(
    {
        "$dynamicRef", "$dynamicAnchor", "$recursiveRef", "$recursiveAnchor", "$anchor", "$id", "$schema",
        "prefixItems", "if", "then", "else", "not", "unevaluatedProperties", "unevaluatedItems",
        "dependentSchemas", "dependentRequired", "dependencies", "patternProperties", "contentSchema",
        "nullable", "discriminator",
    }
)  # fmt: skip
"""2019-09/2020-12-only, conditional or vendor keywords that common client dialects drop or reject."""

SUBSCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")
SUBSCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
SUBSCHEMA_SINGLE = (
    "items", "additionalProperties", "unevaluatedProperties", "additionalItems", "unevaluatedItems", "contains",
    "propertyNames", "not", "if", "then", "else", "contentSchema",
)  # fmt: skip


def subschemas(node: Any, path: str, keyword: str | None = None) -> Iterator[tuple[str, str | None, Any]]:
    """Every (path, keyword it sits under, subschema) in `node`, depth first, `node` itself included."""
    yield path, keyword, node
    if not isinstance(node, dict):
        return
    for key in SUBSCHEMA_MAPS:
        for name, sub in (node.get(key) or {}).items():
            yield from subschemas(sub, f"{path}/{key}/{name}", key)
    for key in SUBSCHEMA_LISTS:
        for index, sub in enumerate(node.get(key) or ()):
            yield from subschemas(sub, f"{path}/{key}/{index}", key)
    for key in SUBSCHEMA_SINGLE:
        if key in node:
            value = node[key]
            if key == "items" and isinstance(value, list):
                for index, sub in enumerate(value):
                    yield from subschemas(sub, f"{path}/items/{index}", key)
            else:
                yield from subschemas(value, f"{path}/{key}", key)


def _resolve(root: dict[str, Any], ref: str) -> Any:
    node: Any = root
    for part in ref[2:].split("/") if ref.startswith("#/") else ():
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def portability_problems(schema: Any) -> list[str]:
    """Every non-portable construct in one tool schema, as 'path: problem' strings."""
    problems: list[str] = []
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return ["/: the root must be an object schema"]
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as error:
        problems.append(f"/: not a valid JSON Schema ({error.message})")
    for path, keyword, node in subschemas(schema, ""):
        where = path or "/"
        if node is True:
            problems.append(f"{where}: bare `true` accepts any value (declare a type)")
            continue
        if node is False:
            if keyword not in BOOLEAN_OK:
                problems.append(f"{where}: bare `false` where a schema object is expected")
            continue
        if not isinstance(node, dict):
            problems.append(f"{where}: not a schema ({type(node).__name__})")
            continue
        if keyword in ("$defs", "definitions") and path.count("$defs") + path.count("definitions") > 1:
            problems.append(f"{where}: nested definitions (keep $defs at the root)")
        if not (TYPED & node.keys()):
            problems.append(f"{where}: untyped schema {sorted(node)} accepts any value")
        if isinstance(node.get("type"), list):
            problems.append(f"{where}: `type` is an array (split it into anyOf branches)")
        for bad in sorted(NON_PORTABLE & node.keys()):
            problems.append(f"{where}: non-portable keyword `{bad}`")
        ref = node.get("$ref")
        if ref is not None:
            if not isinstance(ref, str) or not ref.startswith("#/"):
                problems.append(f"{where}: `$ref` {ref!r} is not a local reference")
            elif _resolve(schema, ref) is None:
                problems.append(f"{where}: `$ref` {ref!r} does not resolve in this document")
        if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
            problems.append(f"{where}: empty `enum`")
        if "required" in node:
            declared = set((node.get("properties") or {}).keys())
            missing = [name for name in node["required"] if name not in declared]
            if missing:
                problems.append(f"{where}: required but not declared: {missing}")
        if "default" in node and path:
            standalone = {**{k: v for k, v in node.items() if k != "default"}, "$defs": schema.get("$defs", {})}
            if not jsonschema.Draft202012Validator(standalone).is_valid(node["default"]):
                problems.append(f"{where}: default {node['default']!r} does not validate against its own schema")
    return problems


# ---------------------------------------------------------------------------- the walker itself


@pytest.mark.parametrize(
    ("schema", "fragment"),
    [
        ({"type": "object", "properties": {"v": {}}}, "untyped schema"),
        ({"type": "object", "properties": {"v": True}}, "bare `true`"),
        ({"type": "object", "additionalProperties": True}, "bare `true`"),
        ({"type": "object", "properties": {"v": {"description": "anything"}}}, "untyped schema"),
        ({"type": "object", "$defs": {"JsonValue": {}}, "properties": {"v": {"$ref": "#/$defs/JsonValue"}}}, "untyped schema"),
        ({"type": "object", "properties": {"v": {"type": ["string", "null"]}}}, "`type` is an array"),
        ({"type": "object", "properties": {"v": {"$ref": "https://example.com/s.json"}}}, "not a local reference"),
        ({"type": "object", "properties": {"v": {"$ref": "#/$defs/Missing"}}}, "does not resolve"),
        ({"type": "object", "properties": {"v": {"type": "array", "prefixItems": [{"type": "string"}]}}}, "`prefixItems`"),
        ({"type": "object", "properties": {"v": {"type": "string", "nullable": True}}}, "`nullable`"),
        ({"type": "object", "properties": {"v": {"type": "string", "default": 3}}}, "does not validate"),
        ({"type": "object", "required": ["v"], "properties": {}}, "required but not declared"),
        ({"type": "object", "properties": {"v": False}}, "bare `false`"),
        ({"type": "string"}, "root must be an object"),
    ],
)
def test_the_walker_catches_non_portable_constructs(schema: dict[str, Any], fragment: str) -> None:
    assert any(fragment in problem for problem in portability_problems(schema)), portability_problems(schema)


def test_the_walker_accepts_the_portable_spellings() -> None:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "$defs": {
            "JsonData": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "number"},
                    {"type": "array", "items": {"$ref": "#/$defs/JsonData"}},
                    {"type": "object", "additionalProperties": {"$ref": "#/$defs/JsonData"}},
                    {"type": "null"},
                ]
            }
        },
        "properties": {
            "value": {"$ref": "#/$defs/JsonData"},
            "maybe": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
            "kind": {"enum": ["a", "b"], "default": "a"},
        },
        "required": ["value"],
    }
    assert portability_problems(schema) == []


# ---------------------------------------------------------------------------- every tool


async def test_every_tool_schema_is_portable(client: Any) -> None:
    tools = (await client.list_tools()).tools
    assert len(tools) >= 16
    problems = [
        f"{tool.name}.{kind}{problem}"
        for tool in tools
        for kind, schema in (("inputSchema", tool.input_schema), ("outputSchema", tool.output_schema))
        if schema is not None
        for problem in portability_problems(schema)
    ]
    assert problems == []


async def test_no_tool_schema_mentions_pydantic_json_value(client: Any) -> None:
    for tool in (await client.list_tools()).tools:
        for schema in (tool.input_schema, tool.output_schema or {}):
            assert "JsonValue" not in (schema.get("$defs") or {}), tool.name
            for _path, _keyword, node in subschemas(schema, ""):
                assert node != {} and node is not True, tool.name


async def test_free_form_json_is_an_explicit_union(client: Any) -> None:
    tools = {t.name: t for t in (await client.list_tools()).tools}
    output = tools["find_item"].output_schema
    assert output is not None
    json_data = output["$defs"]["JsonData"]
    assert {branch.get("type") for branch in json_data["anyOf"]} == {
        "string", "integer", "number", "boolean", "array", "object", "null"
    }
    assert output["$defs"]["Fact"]["properties"]["value"]["$ref"] == "#/$defs/JsonData"
    # `details` (book, place_custom_order, request_quote) is typed in place: no `$defs` in inputs.
    for name in ("book", "place_custom_order", "request_quote"):
        details = tools[name].input_schema["properties"]["details"]
        values = details["anyOf"][0]["additionalProperties"]
        assert {branch.get("type") for branch in values["anyOf"]} == {"string", "integer", "number", "boolean", "array", "null"}
        assert "$defs" not in tools[name].input_schema


# ---------------------------------------------------------------------------- structured output still validates


async def test_structured_results_validate_against_the_explicit_schemas(client: Any) -> None:
    """Facts carry every JSON shape the profile has (objects, lists of objects, strings, integers,
    booleans); each result validates against the tool's (now explicit) outputSchema."""
    tools = {t.name: t for t in (await client.list_tools()).tools}
    calls = [
        ("get_business_info", {"business_id": BAKERY, "topics": ["hours", "location", "contact"]}),
        ("find_item", {"business_id": BAKERY, "item": "gluten-free cake", "aspects": ["allergens", "dietary", "price", "options"]}),
        ("find_item", {"business_id": SALON, "item": "balayage", "aspects": ["price", "options", "duration"]}),
        ("get_catalog", {"business_id": HOME, "limit": 25}),
        ("ask_policy", {"business_id": BAKERY, "question": "How much is the deposit?"}),
    ]
    shapes: set[type] = set()
    for name, args in calls:
        data = structured(await client.call_tool(name, args))
        jsonschema.validate(data, tools[name].output_schema)
        shapes |= {type(f["value"]) for f in data["facts"]}
    assert {dict, list, str, int} <= shapes


@pytest.mark.parametrize(
    "value",
    ["text", 12, 4.5, True, False, None, [1, "a", None], {"weekly": [{"days": ["tue"], "open": "07:00"}]}],
)
def test_fact_values_keep_their_json_type(value: Any) -> None:
    fact = Fact(fact_ref="menu/x#price", value=value)
    assert fact.value == value and type(fact.value) is type(value)
    assert Fact.model_validate(fact.model_dump(mode="json")).value == value


@pytest.mark.parametrize("value", [object(), {1, 2}, b"bytes"])
def test_fact_values_must_be_json(value: Any) -> None:
    with pytest.raises(ValueError):
        Fact(fact_ref="menu/x#price", value=value)


def test_task_results_are_json_objects() -> None:
    result = {"speak": "Done.", "facts": [], "status": "approved", "commitment": {"choices": {"size": "6in-round"}}}
    view = TaskStatusResult(speak="Approved.", task_id="t1", task_status="completed", result=result)
    assert view.result == result
    with pytest.raises(ValueError):
        TaskStatusResult(speak="Approved.", task_id="t1", task_status="completed", result={"when": object()})


async def test_details_take_typed_values_and_refuse_nested_objects(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        nested = await client.call_tool(
            "request_quote", {"business_id": HOME, "item": "drain cleaning", "details": {"zip": {"code": "80211"}}}
        )
        assert nested.is_error is True
        typed = await client.call_tool(
            "place_custom_order",
            {
                "business_id": BAKERY,
                "item": "custom cake",
                "choices": {"size": "six inch round", "flavor": "vanilla bean", "filling": "lemon curd", "frosting": "vanilla buttercream"},
                "date": "2026-10-18",
                "details": {"servings": 10, "customer_name": "Sam Lee", "customer_phone": "+12175550100", "extras": ["flowers"]},
            },
        )
        assert structured(typed)["status"] in ("confirmed", "pending_owner"), structured(typed)["speak"]
