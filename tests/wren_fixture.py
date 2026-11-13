"""Disposable loopback MCP server for metadata and opt-in analytics."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastmcp import FastMCP

PROJECT = {
    "name": "jaffle-wren",
    "catalog": "wren",
    "schema": "public",
    "data_source": "duckdb",
}
MDL = json.loads(
    (Path(__file__).parent / "jaffle_mdl.json").read_text(encoding="utf-8")
)
REVIEWED_TOOLS = (
    "list_models",
    "describe_model",
    "get_instructions",
    "dry_plan",
    "run_sql",
)
DENIED_TOOLS = (
    "recall_queries",
    "get_context",
    "list_knowledge",
    "write_model",
)
DEFAULT_QUERY_RESULT = {
    "columns": ["total_orders"],
    "rows": [{"total_orders": 99}],
    "row_count": 1,
    "truncated": False,
}
QUERY_RELEASE_SECONDS = 20
SchemaVariant = Literal["equivalent", "annotated", "incompatible"]


def _component(mcp: FastMCP, name: str) -> Any:
    return next(
        component
        for component in mcp.local_provider._components.values()
        if getattr(component, "name", None) == name
    )


class WrenFixture:
    """Loopback Wren stand-in.

    Default mode publishes mutable ``wren://project`` and ``get_mdl``.
    ``run_sql`` is present and always refuses. ``analytics=True`` adds the
    reviewed tools. Compatible ``run_sql`` appends ``(sql, limit)`` to
    ``queries``, sets ``query_started``, then waits on ``release`` for at
    most ``release_timeout`` seconds (default 20). ``release`` starts set;
    clear it to hold a query. ``query_result`` defaults to the synthetic
    orders count. A set ``query_error`` is raised after that wait.

    ``schema_variant="annotated"`` adds harmless titles, defaults, and a
    nullable limit, and omits read-only hints. ``"incompatible"`` publishes
    required arguments the reviewed contract cannot call. ``denied_tools``
    adds recall, context, knowledge, and write tools that refuse and never
    record a query. Those flags require ``analytics=True``.
    """

    def __init__(
        self,
        *,
        required_argument: bool = False,
        project_resource: bool = True,
        open_schema: bool = False,
        schema_title: str | None = None,
        analytics: bool = False,
        query_result: dict[str, Any] | None = None,
        query_error: Exception | None = None,
        schema_variant: SchemaVariant = "equivalent",
        denied_tools: bool = False,
    ) -> None:
        if schema_variant not in {"equivalent", "annotated", "incompatible"}:
            raise ValueError(f"unknown analytics schema variant: {schema_variant}")
        if not analytics and (schema_variant != "equivalent" or denied_tools):
            raise ValueError("analytics variants require analytics=True")
        self.analytics = analytics
        self.schema_variant = schema_variant
        self.denied_tools = denied_tools
        self.project = deepcopy(PROJECT)
        self.mdl = deepcopy(MDL)
        self.calls: list[str] = []
        self.queries: list[tuple[str, int | None]] = []
        self.query_result = (
            deepcopy(DEFAULT_QUERY_RESULT)
            if query_result is None
            else deepcopy(query_result)
        )
        self.query_error = query_error
        self.query_started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.release_timeout = QUERY_RELEASE_SECONDS
        self.on_mdl: Callable[[], None] | None = None
        self.ready = threading.Event()
        mcp = FastMCP("test-wren")
        self._register_metadata(
            mcp,
            project_resource=project_resource,
            required_argument=required_argument,
            open_schema=open_schema,
            schema_title=schema_title,
        )
        if analytics:
            self._register_analytics(mcp)
        else:
            self._register_forbidden_query(mcp)

        application = mcp.http_app(path="/mcp")
        ready = self.ready

        class ReadyServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                ready.set()

        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}/mcp"
        self.server = ReadyServer(
            uvicorn.Config(application, log_level="error", lifespan="on")
        )
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True
        )

    def _register_metadata(
        self,
        mcp: FastMCP,
        *,
        project_resource: bool,
        required_argument: bool,
        open_schema: bool,
        schema_title: str | None,
    ) -> None:
        if project_resource:

            @mcp.resource("wren://project", mime_type="application/json")
            def project() -> str:
                self.calls.append("wren://project")
                return json.dumps(self.project)

        if required_argument:

            @mcp.tool(name="get_mdl")
            def get_mdl_with_argument(argument: str) -> dict[str, Any]:
                self.calls.append("get_mdl")
                return self.mdl

        elif open_schema:

            @mcp.tool(name="get_mdl")
            def get_mdl_open(note: str | None = None) -> dict[str, Any]:
                self.calls.append("get_mdl")
                return self.mdl

        else:

            @mcp.tool()
            def get_mdl() -> dict[str, Any]:
                self.calls.append("get_mdl")
                if self.on_mdl is not None:
                    self.on_mdl()
                return self.mdl

            if schema_title is not None:
                _component(mcp, "get_mdl").parameters["title"] = schema_title

    def _register_forbidden_query(self, mcp: FastMCP) -> None:
        @mcp.tool()
        def run_sql(sql: str) -> dict[str, Any]:
            self.calls.append("run_sql")
            raise RuntimeError("query execution forbidden in fixture")

    def _register_analytics(self, mcp: FastMCP) -> None:
        if self.schema_variant == "incompatible":
            self._register_incompatible(mcp)
        else:
            self._register_reviewed(mcp, annotate=self.schema_variant == "annotated")
        if self.denied_tools:
            self._register_denied(mcp)

    def _register_reviewed(self, mcp: FastMCP, *, annotate: bool) -> None:
        annotations = None if annotate else {"read_only_hint": True}

        @mcp.tool(annotations=annotations)
        def list_models() -> dict[str, Any]:
            self.calls.append("list_models")
            return {
                "models": [model["name"] for model in self.mdl["models"]],
            }

        @mcp.tool(annotations=annotations)
        def describe_model(name: str) -> dict[str, Any]:
            self.calls.append("describe_model")
            for model in self.mdl["models"]:
                if model["name"] == name:
                    return model
            raise RuntimeError("unknown model")

        @mcp.tool(annotations=annotations)
        def get_instructions() -> str:
            self.calls.append("get_instructions")
            return "Synthetic Jaffle instructions."

        @mcp.tool(annotations=annotations)
        def dry_plan(sql: str) -> str:
            self.calls.append("dry_plan")
            return sql

        @mcp.tool(annotations=annotations)
        def run_sql(sql: str, limit: int | None = None) -> dict[str, Any]:
            self.calls.append("run_sql")
            self.queries.append((sql, limit))
            self.query_started.set()
            if not self.release.wait(self.release_timeout):
                raise TimeoutError("analytics query release timed out")
            if self.query_error is not None:
                raise self.query_error
            return deepcopy(self.query_result)

        if annotate:
            for name in REVIEWED_TOOLS:
                self._annotate(_component(mcp, name))

    def _annotate(self, tool: Any) -> None:
        tool.annotations = None
        schema = tool.parameters
        schema["title"] = f"{tool.name}Arguments"
        schema["description"] = "Harmless schema annotation."
        properties = schema.setdefault("properties", {})
        for prop in properties.values():
            prop["title"] = "Annotated"
            prop["description"] = "Harmless field annotation."
        limit = properties.get("limit")
        if isinstance(limit, dict):
            limit.pop("type", None)
            limit["anyOf"] = [{"type": "integer"}, {"type": "null"}]
            limit["default"] = None

    def _register_incompatible(self, mcp: FastMCP) -> None:
        @mcp.tool(name="list_models")
        def list_models(token: str) -> dict[str, Any]:
            self._refuse("list_models", "incompatible query contract")

        @mcp.tool(name="describe_model")
        def describe_model(model_id: int) -> dict[str, Any]:
            self._refuse("describe_model", "incompatible query contract")

        @mcp.tool(name="get_instructions")
        def get_instructions(token: str) -> str:
            self._refuse("get_instructions", "incompatible query contract")

        @mcp.tool(name="dry_plan")
        def dry_plan() -> str:
            self._refuse("dry_plan", "incompatible query contract")

        @mcp.tool(name="run_sql")
        def run_sql(statement: int) -> dict[str, Any]:
            self._refuse("run_sql", "incompatible query contract")

    def _register_denied(self, mcp: FastMCP) -> None:
        readonly = {"read_only_hint": True}

        @mcp.tool(annotations=readonly)
        def recall_queries(question: str, limit: int = 3) -> dict[str, Any]:
            self._refuse("recall_queries", "tool execution denied in fixture")

        @mcp.tool(annotations=readonly)
        def get_context(question: str) -> dict[str, Any]:
            self._refuse("get_context", "tool execution denied in fixture")

        @mcp.tool(annotations=readonly)
        def list_knowledge() -> dict[str, Any]:
            self._refuse("list_knowledge", "tool execution denied in fixture")

        @mcp.tool(annotations={"read_only_hint": False, "destructive_hint": True})
        def write_model(name: str) -> str:
            self._refuse("write_model", "tool execution denied in fixture")

    def _refuse(self, name: str, message: str) -> None:
        self.calls.append(name)
        raise RuntimeError(message)

    def __enter__(self) -> WrenFixture:
        self.thread.start()
        if not self.ready.wait(10):
            raise RuntimeError("MCP fixture did not start")
        return self

    def __exit__(self, *args: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("MCP fixture did not stop")
