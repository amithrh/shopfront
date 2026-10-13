# mcp-tasks

The SEP-2663 **Tasks** extension (`io.modelcontextprotocol/tasks`) for the MCP Python SDK 2.2
(protocol `2026-07-28`).

`mcp` 2.2.0 ships the 2025-11-25 task models as types only and never dispatches `tasks/*`. On
2026-07-28, tasks are an extension. This package implements it on the SDK's extension seams:

| Module | What it gives you |
|---|---|
| `mcp_tasks.TasksExtension` | Server half. Pass it to `MCPServer(extensions=[...])`. It serves `tasks/get`, `tasks/update`, `tasks/cancel` and `tasks/list`, and it turns a `tools/call` into a task handle when the tool adopts a task. |
| `mcp_tasks.TaskManager` | The task state machine. Every transition is a compare-and-swap. The clock is injectable. |
| `mcp_tasks.InMemoryTaskStore`, `mcp_tasks.dynamodb.DynamoTaskStore` | Storage behind the `mcp_tasks.TaskStore` protocol. |
| `mcp_tasks.client.TasksClientExtension` | Client half. Pass it to `Client(extensions=[...])`. `call_tool` then polls until the task is terminal and answers `input_required` rounds. |
| `mcp_tasks.client.get_task` / `update_task` / `cancel_task` / `list_tasks` / `answer_input_requests` | Manual control over a task handle. |

Requirements: Python 3.12, `mcp>=2.2.0,<2.3`. License: Apache-2.0.

## Server quick start

A tool decides per call whether to answer with a task. Nothing waits inside the server: the task is
state in a store, and something outside the request (a webhook, a worker, a person) finishes it later.

```python
import mcp_types as types
from pydantic import BaseModel
from mcp.server.mcpserver import MCPServer
from mcp_tasks import InMemoryTaskStore, TaskManager, TasksExtension

manager = TaskManager(InMemoryTaskStore(), default_ttl_ms=24 * 3600 * 1000, default_poll_interval_ms=2000)
tasks = TasksExtension(manager)
server = MCPServer("approvals", extensions=[tasks])


class Outcome(BaseModel):  # one output schema for every path (sync and task)
    status: str
    reference: str


@server.tool()
async def request_approval(summary: str) -> Outcome:
    """Ask a human to approve something."""
    record = await tasks.ensure_task(status_message="Waiting for approval")  # task_id=<HMAC-derived> for idempotency
    await notify_approver(record.task_id, summary)    # your side channel (signed link, queue, ...)
    tasks.adopt(record)  # True: the client receives the task handle instead of the value below
    return Outcome(status="pending", reference=record.task_id)  # sent as is to callers that cannot take tasks


# Later, from the approver's webhook or a worker:
async def approver_said_yes(task_id: str) -> None:
    result = Outcome(status="approved", reference=task_id)
    await manager.complete(
        task_id,
        types.CallToolResult(
            content=[types.TextContent(type="text", text=result.model_dump_json())],
            structured_content=result.model_dump(mode="json"),
        ),
        message="Approved",
    )
```

The tool-side API:

| Call | Behaviour |
|---|---|
| `await tasks.ensure_task(status_message=..., task_id=None, ttl_ms=None, poll_interval_ms=None, labels=None, arguments=None, refresh_ttl=False)` | Creates the task for the current call. With a caller-chosen `task_id` it is an idempotent get-or-create: a retry with the same id, principal and tool returns the stored live record, whatever its status; a record past its TTL is replaced by a fresh `working` one. The same id with another principal or tool raises `DuplicateTask`. It works for every caller, so a synchronous fallback can still hand out a pollable id. `arguments` replaces the raw call arguments stored on the record (pass a minimal dict when they may hold personal data). `refresh_ttl=True` makes a reused *active* record live at least `ttl_ms` from now (one CAS; a TTL is only ever extended, terminal records are untouched): use it when the retry starts the work the task tracks, so it does not inherit an earlier attempt's deadline. |
| `tasks.adopt(record) -> bool` | Answers the current `tools/call` with the record's task handle. It returns `False`, and changes nothing, unless the caller is on 2026-07-28 and advertised the extension. |
| `await tasks.defer(status_message=..., ...)` | `ensure_task` plus `adopt`, for callers that can take a task. It returns `None`, and creates no task, for everyone else. It takes the same `task_id`, `ttl_ms`, `poll_interval_ms`, `labels` and `arguments` as `ensure_task`. |
| `TasksExtension(manager, serve_list=True, list_page_size=50, list_anonymous=False, on_cancel=None, on_input=None, present=None)` | `on_cancel(record)` runs after a client's `tasks/cancel` succeeds, so the application can release whatever the task was holding; its failures are logged, never undo the cancel. `on_input(record)` runs after a `tasks/update` was accepted. `present(record) -> record` maps every record to the view a client is sent (the `tools/call` handle, `tasks/get`, `tasks/list` rows), e.g. an output check against rules that changed after the record was written; the view is never stored. |
| `tasks.current_call()` | The `CallInfo(tool_name, arguments, principal, opted_in)` of the running call, or `None` outside a call. |

The manager API, for webhooks, workers and schedulers:

| Call | Transition |
|---|---|
| `set_message(id, msg)` | `working` or `input_required` keeps its status. It updates `statusMessage`. |
| `require_input(id, {key: ElicitRequest/...}, msg)` | `working` to `input_required`. Use a fresh key for every question. |
| `submit_input(id, principal, responses)` | `input_required` to `working` once every key is answered. `tasks/update` calls it. |
| `complete(id, CallToolResult, message=)` | Active to `completed`. The result must match the tool's `outputSchema`: `Client.call_tool` re-validates it. |
| `fail(id, message, code=-32603, data=None)` | Active to `failed`. |
| `cancel(id, message)` | Active to `cancelled`. Use it for client cancellation, expiry sweeps and business decisions. |

Any change to a terminal task raises `InvalidTransition`. An unknown, expired or foreign task raises
`TaskNotFound`. A tool that adopts a task and then raises, or returns an `is_error` result, leaves the
task `failed`, never `working`. This includes an `MCPError`, which MCPServer re-raises as a protocol
error. A crash's own text stays on the server: MCPServer only reports `Error executing tool <name>`.
To surface your own message, raise `ToolError`.

Pass `TaskManager(..., now=clock)` to control `createdAt`, `lastUpdatedAt` and TTL expiry. The clock
must return timezone-aware datetimes. `lastUpdatedAt` never moves backwards, even if the clock does.

## Client quick start

```python
from mcp import Client
from mcp_tasks.client import TasksClientExtension

async def show(state):
    print(state.status, state.status_message)

async with Client("https://example.test/mcp", extensions=[TasksClientExtension(on_status=show, max_wait_s=900)],
                  elicitation_callback=my_elicitation_callback) as client:
    result = await client.call_tool("request_approval", {"summary": "refund"})   # polls until terminal
```

`TasksClientExtension` behaves as follows:

- It polls `tasks/get` every `pollIntervalMs`, with a floor of 10 ms. Without a hint it uses
  `default_poll_interval_s`.
- It answers `input_required` through the client's elicitation, sampling and roots callbacks, and
  then calls `tasks/update`.
- A failed or cancelled task resolves to `CallToolResult(is_error=True)`.
- On caller cancellation, or when it cannot answer an input request, it cancels the task
  server-side in a shielded scope. On timeout (`max_wait_s`) it raises `TimeoutError` but leaves
  the task running (an owner may answer hours later); pass `cancel_on_timeout=True` to cancel it.
- A `tasks/update` rejected with `-32602` because the task moved on meanwhile (for example the owner
  decided while the user was answering) is not an error: it keeps polling and returns the outcome.
- It skips stale reads: a state whose `lastUpdatedAt` is older than one it has already seen.
- It answers each input key once while the task stays `input_required`. That set resets when a poll
  shows any other status. Servers should still use a fresh key per question, because a re-ask that
  lands between two polls looks exactly like the old question.

For manual control, use `client.session.call_tool(..., allow_claimed=True)`, which returns a
`TaskHandle`. Then call `get_task`, `answer_input_requests` + `update_task`, `cancel_task` or
`list_tasks` with `client.session`.

## Wire contract (SEP-2663)

| Message | Shape |
|---|---|
| Capability | The client advertises `capabilities.extensions["io.modelcontextprotocol/tasks"]`. The server advertises the same key. There is no per-tool `taskSupport` on 2026-07-28. |
| `tools/call` result (handle) | `{resultType:"task", taskId, status, statusMessage?, createdAt, lastUpdatedAt, ttlMs, pollIntervalMs?}`. It carries no status payload. |
| `tasks/get {taskId}` | The same task fields with `resultType:"complete"`, plus exactly one payload: `inputRequests` when `input_required`, `result` (a `CallToolResult`) when `completed`, or `error {code, message, data?}` when `failed`. There is none for `working` or `cancelled`. There is no `tasks/result`. |
| `tasks/update {taskId, inputResponses}` | An empty acknowledgement. Keys match the task's `inputRequests`. |
| `tasks/cancel {taskId}` | An empty acknowledgement. |
| `tasks/list {cursor?}` | `{tasks: [task fields...], nextCursor?}`, oldest first, for the caller's principal. This is a convenience that strands does not call. It requires an authenticated principal: without auth every caller shares `anonymous`, so it answers `-32602` unless the server opts in with `TasksExtension(list_anonymous=True)` (single-user development only). |
| Field rules | `ttlMs` is always present and nullable (`null` means unlimited), and it is never negative. Timestamps are RFC 3339 UTC with a `Z` suffix. `lastUpdatedAt >= createdAt`. `taskId` is non-empty. |
| Answers | An accepted answer to a form question must match that form's `requestedSchema` (types, `maxLength`/`minLength`, enums, `required`, no extra fields; strings without `maxLength` are capped at 10,000 characters) and the whole payload must stay under 64 KB. The record keeps only an elicitation answer's `action` and, for an accepted form, its validated `content`: MCP defines `content` on accept only, so a decline or cancel is stored as `{"action": ...}` whatever it carried. |
| Errors | An unknown, expired or foreign task, an invalid transition, malformed or schema-violating `inputResponses` or a malformed cursor all give `-32602`. Any `tasks/*` call on a pre-2026 wire gives `-32601`. |
| `Mcp-Name` | Clients mirror `taskId` into `Mcp-Name` for `tasks/*`. The SDK server does not enforce it. |

Status updates are pull-only. Progress notifications (`ctx.report_progress`) only reach the client
during the create round, before the handle is returned. After that, change `statusMessage`.

## Degradation rules

A task handle is returned **only** to a 2026-07-28 request whose client advertised the extension.
Everyone else gets the tool's ordinary synchronous result:

- **A 2026-07-28 client without the extension** cannot parse a handle; it would raise a
  `ValidationError`. `adopt()` returns `False` and the sync result is sent.
- **A 2025-11-25 (legacy) client** gets the same treatment. Legacy wires sieve unknown result types,
  so a handle would become `-32603`. The `tasks/*` methods answer `-32601` there.
- **`defer()`** creates no task for these callers. **`ensure_task()`** does, so a sync result can
  carry a `task_id` that the caller polls through a tool of your own.
- **Task state** lives in the store, keyed by `taskId` (plus principal). 2026-07-28 HTTP has no
  sessions, so tasks survive reconnects, replicas and restarts if the store does.

## Store contract

Implement `mcp_tasks.TaskStore` over any store with a conditional write:

| Method | Contract |
|---|---|
| `put_new(record)` | A conditional create. It raises `DuplicateTask` if the id exists. |
| `get(task_id)` | Strongly consistent: a write is visible to the next `get` on any replica. It returns `None` when the task is missing. |
| `replace(record, *, expected_version)` | A compare-and-swap on `version`. It writes only if the stored version matches, returns `False` otherwise (including when the record is missing), and never upserts. |
| `list_for(principal, *, cursor, limit)` | A page of the principal's tasks, oldest first, with an opaque cursor. A malformed cursor raises `ValueError`. Results may lag writes slightly and may include expired rows; `TaskManager.list_for` filters those out. |

`TaskRecord` is the only persisted shape. Each record carries:

- the id, principal, tool, arguments and status;
- the timestamps, `ttl_ms` and `poll_interval_ms`;
- the wire-shaped `input_requests`, `input_responses`, `result` and `error`;
- opaque `labels`;
- the CAS `version`.

`InMemoryTaskStore` is process-local. Use it for tests and a single replica.

## DynamoDB

`DynamoTaskStore(client, table, principal_index="byPrincipal", key_attr="taskId", key_prefix="")`:

- The boto3 client is injected. The package does not import boto3; install the `dynamodb` extra if
  you build a client.
- Calls are synchronous and run in a worker thread.
- `put_new` writes on `attribute_not_exists`. `replace` is conditional on `version`. `get` uses
  `ConsistentRead`.
- `list_for` queries the GSI. GSIs are eventually consistent, and the cursor is bound to its
  principal.

Table definition (`mcp_tasks.dynamodb.table_definition(name)` and `ttl_definition(name)` build the
requests):

| Item | Definition |
|---|---|
| Table | On-demand (`PAY_PER_REQUEST`). |
| Hash key | `taskId` (S): `key_prefix` + task id. |
| Attributes | `principal` (S), `createdAt` (S, fixed-width UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`), `status` (S), `version` (N), `doc` (S, the full JSON record), `expiresAt` (N, epoch seconds, rounded up; absent for unlimited TTL). |
| GSI `byPrincipal` | Hash `principal`, range `createdAt`, projection `ALL`. Used by `tasks/list`. |
| TTL | Enabled on `expiresAt`. DynamoDB deletes lazily; the manager already treats a task as gone once `createdAt + ttlMs` has passed. |

```python
client.create_table(**table_definition("my-tasks"))
client.update_time_to_live(**ttl_definition("my-tasks"))
```

In a shared single-table design, set `key_attr`/`key_prefix` (for example `pk` and `TASK#`) and give
tasks their own principal index, since every item in that index must be a task document.

## Security notes

- **A task id is a bearer capability.** Without auth, every caller is the principal `anonymous`, so
  anyone holding an id can poll, answer or cancel it. Random ids are 128-bit
  (`secrets.token_urlsafe(16)`). Derive caller-chosen ids with a keyed hash (HMAC over your
  idempotency key), never from guessable input alone.
- **Principal binding.** With SDK auth enabled, `principal_of(ctx)` is the authenticated
  (client, issuer, subject) binding. `tasks/*` then hide other principals' tasks: they look exactly
  like unknown ids (`-32602`). A reused id with another principal raises `DuplicateTask`. Enable
  auth before real traffic.
- **Sign anything that completes a task.** `MCPServer.custom_route` endpoints are unauthenticated by
  design. An approver webhook must verify an HMAC-signed, expiring link, or sit behind your own auth,
  before it calls `complete`, `fail` or `require_input`.
- **Treat answers as untrusted.** `input_responses` and anything a person typed are untrusted input.
  Validate them before acting.
- **Never return a task to a client that did not opt in.** `adopt()` enforces this. The SDK runner
  has a TODO to reject such results.

## Interop

The wire shapes come from the independent SEP-2663 client in strands-agents 1.57.1
(`strands/tools/mcp/mcp_tasks.py`), the only installed definition; the SEP text itself is not
vendored. The following were verified against this server:

- a strands `MCPClient` over Streamable HTTP, covering `working`, `completed`, `input_required`
  answered through strands' own elicitation path, and `cancel_task_sync`;
- `TaskRecord.wire()` bodies for all five statuses and the handle, which pass strands'
  `MCPGetTaskResult` and `MCPCreateTaskResult` validators.

`tasks/list` and the `-32602` error code are this package's choices. The final SEP may differ.

Not supported:

- the 2025-11-25 task protocol (`params.task`, `CreateTaskResult{task}`, `tasks/result`,
  `notifications/tasks/status`);
- server-pushed status on 2026-07-28.

## Development

```bash
cd packages/mcp-tasks
python -m pytest -q                  # everything, including the loopback HTTP tests
python -m pytest -q -m "not http"    # skip the uvicorn tests
```

The tests use anyio on asyncio. The HTTP tests serve `MCPServer.streamable_http_app()` under
uvicorn on a free `127.0.0.1` port; nothing else touches the network. The DynamoDB tests use
botocore's `Stubber`, with region `ap-southeast-2` and dummy credentials, and never call AWS.
