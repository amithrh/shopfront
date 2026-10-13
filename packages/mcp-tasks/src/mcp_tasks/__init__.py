"""SEP-2663 Tasks extension (`io.modelcontextprotocol/tasks`) for the MCP Python SDK 2.2.

Server::

    from mcp.server.mcpserver import MCPServer
    from mcp_tasks import InMemoryTaskStore, TaskManager, TasksExtension

    manager = TaskManager(InMemoryTaskStore())   # or mcp_tasks.dynamodb.DynamoTaskStore(...)
    tasks = TasksExtension(manager)
    server = MCPServer("my-server", extensions=[tasks])

Client::

    from mcp import Client
    from mcp_tasks.client import TasksClientExtension

    async with Client(url, extensions=[TasksClientExtension()]) as client:
        result = await client.call_tool("slow_tool", {})   # polls tasks/get until terminal

The client module is imported lazily so servers do not pay for it.
"""

from mcp_tasks.manager import InvalidInput, InvalidTransition, TaskManager, TaskNotFound
from mcp_tasks.models import (
    ACTIVE,
    TASKS_EXTENSION_ID,
    TASKS_PROTOCOL_VERSIONS,
    TERMINAL,
    Clock,
    TaskRecord,
    TaskStatus,
    iso_utc,
    utc_now,
)
from mcp_tasks.server import (
    ANONYMOUS_PRINCIPAL,
    CallInfo,
    CancelHook,
    CancelTaskParams,
    GetTaskParams,
    InputHook,
    PresentHook,
    TasksExtension,
    UpdateTaskParams,
    client_opted_in,
    principal_of,
)
from mcp_tasks.store import DuplicateTask, InMemoryTaskStore, TaskStore

__version__ = "0.1.0"

__all__ = [
    "ACTIVE",
    "ANONYMOUS_PRINCIPAL",
    "TASKS_EXTENSION_ID",
    "TASKS_PROTOCOL_VERSIONS",
    "TERMINAL",
    "CallInfo",
    "CancelHook",
    "CancelTaskParams",
    "Clock",
    "DuplicateTask",
    "GetTaskParams",
    "InMemoryTaskStore",
    "InputHook",
    "InvalidInput",
    "InvalidTransition",
    "PresentHook",
    "TaskManager",
    "TaskNotFound",
    "TaskRecord",
    "TaskStatus",
    "TaskStore",
    "TasksExtension",
    "UpdateTaskParams",
    "__version__",
    "client_opted_in",
    "iso_utc",
    "principal_of",
    "utc_now",
]
