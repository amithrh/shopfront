"""Architecture smoke tests: the skeleton imports, the frozen contracts hold, and the MCP surface is
what docs/design/server-architecture.md specifies. These run before any work package lands."""

from __future__ import annotations

import importlib
import json
import pkgutil
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import askonce_server
from mcp import Client
from mcp_tasks import InMemoryTaskStore, TaskManager, TasksExtension
from askonce_server.canonical import profile_sha256
from askonce_server.config import DEV_ID_KEY, Settings
from askonce_server.domain.models import (
    ALLOWED_TRANSITIONS,
    OPEN_STATUSES,
    RELEASES_HOLDS,
    SETTLED_STATUSES,
    can_transition,
)
from askonce_server.ids import IdFactory, new_id
from askonce_server.tools.schemas import CommitmentResult, Grounded, check_spoken

from support import SCHEMA_PATH, apps_advertisement, load_raw_profiles, make_live

EXPECTED_TOOLS = [
    "find_business",
    "get_business_info",
    "get_catalog",
    "find_item",
    "ask_policy",
    "check_availability",
    "book",
    "cancel_booking",
    "place_custom_order",
    "check_service_area",
    "request_quote",
    "ask_owner",
    "get_task",
    "reply_to_owner",
    "check_request_status",
    "cancel_request",
]


def test_every_module_imports() -> None:
    for info in pkgutil.walk_packages(askonce_server.__path__, prefix="askonce_server."):
        if info.name.endswith("__main__"):
            continue
        importlib.import_module(info.name)


def test_settings_reject_dev_secrets_in_prod() -> None:
    with pytest.raises(ValueError, match="DEV-ONLY"):
        Settings(env="prod", storage="dynamodb")


def test_settings_parse_comma_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASKONCE_ALLOWED_HOSTS", "mcp.example.com, localhost:*")
    # A public host means "deployed", which refuses the DEV-ONLY keys: supply real-looking ones.
    monkeypatch.setenv("ASKONCE_REQUEST_STATE_KEYS", "arch-request-state-key-0123456789abcdef")
    monkeypatch.setenv("ASKONCE_OWNER_LINK_KEYS", "arch-owner-link-key-0123456789abcdef0123")
    monkeypatch.setenv("ASKONCE_ID_KEY", "arch-id-key-0123456789abcdef0123456789")
    assert Settings().allowed_hosts == ["mcp.example.com", "localhost:*"]


def test_settings_reject_short_keys() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        Settings(request_state_keys=["short"])


def test_derived_ids_are_stable_and_keyed() -> None:
    a = IdFactory(DEV_ID_KEY.encode())
    b = IdFactory(b"another-key-0123456789abcdef0123456789")
    assert a.task_id("idem_x") == a.task_id("idem_x")
    assert a.task_id("idem_x") != b.task_id("idem_x")
    assert a.commitment_id("idem_x").startswith("c_")
    assert new_id("q") != new_id("q")


def test_profile_hash_ignores_certification(raw_profiles: dict[str, dict[str, Any]]) -> None:
    live = make_live(next(iter(raw_profiles.values())))
    assert live["certification"]["runs"][-1]["profile_sha256"] == profile_sha256(live)
    changed = {**live, "certification": {"status": "draft", "runs": []}}
    assert profile_sha256(changed) == profile_sha256(live)
    assert profile_sha256({**live, "revision": live["revision"] + 1}) != profile_sha256(live)


def test_make_live_profiles_are_schema_valid() -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    for pid, doc in load_raw_profiles().items():
        errors = [e.message for e in validator.iter_errors(make_live(doc))]
        assert errors == [], (pid, errors[:3])


def test_spoken_text_rule() -> None:
    assert check_spoken("Thimbleberry Bakehouse is open until 6 pm.")
    for bad in ("see https://x.example", "www.x.example", "a\nb", "**bold**", "x" * 241, "gluten_free"):
        with pytest.raises(ValueError):
            check_spoken(bad)


def test_commitment_result_is_grounded() -> None:
    assert issubclass(CommitmentResult, Grounded)
    result = CommitmentResult(speak="I'll ask Rosa.", status="pending_owner", task_id="t_x")
    assert result.model_dump()["facts"] == []


def test_status_machine_is_closed() -> None:
    for source, targets in ALLOWED_TRANSITIONS.items():
        assert targets <= set(ALLOWED_TRANSITIONS), source
    assert can_transition("pending_owner", "approved")
    assert not can_transition("declined", "approved")
    assert RELEASES_HOLDS <= set(ALLOWED_TRANSITIONS)


def test_round2_idempotency_replay_settled_statuses_are_exactly_the_terminal_ones() -> None:
    """Replays only return live commitments: every terminal status (including `quoted` and `answered`,
    which keep no capacity to release) lets an identical request be made afresh."""
    assert SETTLED_STATUSES == {status for status, targets in ALLOWED_TRANSITIONS.items() if not targets}
    assert RELEASES_HOLDS <= SETTLED_STATUSES
    assert SETTLED_STATUSES - RELEASES_HOLDS == {"quoted", "answered"}
    assert not SETTLED_STATUSES & (OPEN_STATUSES | {"confirmed", "approved"})


def _standin_deps() -> Any:
    """Just enough of AskOnceDeps for build_mcp_server (tools are not called)."""
    return SimpleNamespace(
        settings=Settings(env="test"), tasks=TasksExtension(TaskManager(InMemoryTaskStore())), profiles=None
    )


@pytest.mark.anyio
async def test_mcp_surface_matches_design() -> None:
    from askonce_server.tools.server import build_mcp_server

    server = build_mcp_server(_standin_deps())
    async with Client(server, extensions=[apps_advertisement()]) as client:
        assert client.protocol_version == "2026-07-28"
        extensions = client.server_capabilities.extensions or {}
        assert {"io.modelcontextprotocol/tasks", "io.modelcontextprotocol/ui"} <= set(extensions)
        tools = (await client.list_tools()).tools
        assert [t.name for t in tools] == EXPECTED_TOOLS
        for tool in tools:
            assert tool.output_schema is not None, tool.name
            assert "speak" in tool.output_schema["properties"], tool.name
            assert tool.meta and tool.meta["ui"]["resourceUri"].startswith("ui://askonce/"), tool.name
            hidden = {"ctx", "plan", "confirmation", "intake"}
            assert not hidden & set(tool.input_schema.get("properties", {})), tool.name
        resources = (await client.list_resources()).resources
        assert {r.mime_type for r in resources} == {"text/html;profile=mcp-app"}


# --------------------------------------------------------------------------- layering (design §2)

_FORBIDDEN_IMPORTS: dict[str, tuple[str, ...]] = {
    "askonce_server.profile": ("askonce_server.domain", "askonce_server.tools", "askonce_server.app"),
    "askonce_server.domain": ("askonce_server.tools", "askonce_server.app"),
    "askonce_server.tools": ("askonce_server.app",),
}


def _imported_modules(path: Any) -> set[str]:
    """Every module named by an import statement in `path` (top-level and function-local)."""
    import ast

    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_layering_is_respected() -> None:
    """profile <- domain <- tools <- app: lower layers never import higher ones (the domain reaches the
    tools and app layers only through its ports)."""
    from pathlib import Path

    root = Path(askonce_server.__file__).parent
    violations: list[str] = []
    for package, forbidden in _FORBIDDEN_IMPORTS.items():
        for path in sorted((root / package.rsplit(".", 1)[1]).rglob("*.py")):
            for name in _imported_modules(path):
                if name.startswith(forbidden):
                    violations.append(f"{path.relative_to(root)} imports {name}")
    assert violations == []


def test_mcp_tasks_is_independent_of_askonce() -> None:
    from pathlib import Path

    import mcp_tasks

    for path in Path(mcp_tasks.__file__).parent.rglob("*.py"):
        assert not any(n.startswith("askonce_server") for n in _imported_modules(path)), path.name


_ELICITATION_BUILDERS: dict[str, set[tuple[str, str]]] = {
    # name called -> every (module, enclosing function) allowed to call it
    "Elicit": {("tools/confirm.py", "guarded_elicit")},
    "ElicitRequest": {("domain/services/owner.py", "owner_question_request")},
    "elicit": set(),
    "elicit_form": set(),
    "elicit_url": set(),
    "elicit_with_validation": set(),
    "send_request": set(),
}
"""Every question the server sends the customer (MRTR rounds, legacy elicitation/create, task input
requests) carries profile or owner text outside any tool result, where the output middleware never sees
it. Each path is canary-checked in exactly one builder: `confirm.guarded_elicit` (the resolvers' `Elicit`)
and `owner_question_request` (checked by `OwnerService.decide` before anything is stored)."""


def test_round2_mrtr_guard_every_elicitation_is_built_by_a_guarded_builder() -> None:
    """F8f: nothing enforced that a new `Elicit(...)` went through the canary check."""
    import ast
    from pathlib import Path

    root = Path(askonce_server.__file__).parent
    found: dict[str, set[tuple[str, str]]] = {name: set() for name in _ELICITATION_BUILDERS}

    class Calls(ast.NodeVisitor):
        def __init__(self, module: str) -> None:
            self.module = module
            self.scope = ["<module>"]

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name in found:
                found[name].add((self.module, self.scope[-1]))
            self.generic_visit(node)

    for path in sorted(root.rglob("*.py")):
        Calls(path.relative_to(root).as_posix()).visit(ast.parse(path.read_text(encoding="utf-8")))
    assert found == _ELICITATION_BUILDERS
