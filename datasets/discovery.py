"""Metadata-only discovery of published Wren semantic models."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.utils.text import slugify
from fastmcp import Client
from mcp.shared.exceptions import MCPError as StreamMCPError
from pydantic_ai.mcp import MCPError, MCPToolset

MAX_METADATA_BYTES = 1024 * 1024


@dataclass(frozen=True)
class DiscoveredModel:
    model_name: str
    code: str
    display_name: str
    description: str
    catalog: str
    schema: str
    physical_table: str
    definition_sha256: str


@dataclass(frozen=True)
class CatalogSnapshot:
    source_key: str
    project_key: str
    models: tuple[DiscoveredModel, ...]
    structure_sha256: str
    snapshot_sha256: str
    execution_metadata_json: str


class DiscoveryError(Exception):
    """Safe, non-sensitive classification of a discovery failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def configured_source_key() -> str | None:
    """Identify the configured endpoint without exposing it."""
    endpoint = settings.WREN_MCP_URL.strip()
    if not endpoint:
        return None
    return "wren-" + hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:59]


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise DiscoveryError("invalid_metadata")
    return value


def _text(value: Any, maximum: int, *, blank: bool = False) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not blank and not value.strip())
    ):
        raise DiscoveryError("invalid_metadata")
    return value


def _structural(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _structural(item) for key, item in value.items() if key != "properties"
        }
    if isinstance(value, list):
        return [_structural(item) for item in value]
    return value


def _named_array(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DiscoveryError("invalid_metadata")
    names: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise DiscoveryError("invalid_metadata")
        name = _text(entry.get("name"), 128)
        if name in names:
            raise DiscoveryError("invalid_metadata")
        names.add(name)
    return sorted(value, key=lambda entry: entry["name"])


def _project_identity(project: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        _text(project.get("name"), 128),
        _text(project.get("catalog"), 128),
        _text(project.get("schema"), 128),
        _text(project.get("data_source"), 128),
    )


def catalog_snapshot(
    source_key: str, first: Any, raw_mdl: Any, second: Any
) -> CatalogSnapshot:
    # Count complete documents, not a selected subset of the metadata.
    size = sum(
        len(value.encode("utf-8"))
        if isinstance(value, str)
        else len(_json_bytes(value))
        for value in (first, raw_mdl, second)
    )
    if (
        size > MAX_METADATA_BYTES
        or not isinstance(first, str)
        or not isinstance(second, str)
    ):
        raise DiscoveryError("invalid_metadata")
    project, mdl, final_project = _object(first), _object(raw_mdl), _object(second)
    name, catalog, schema, source = _project_identity(project)
    if _project_identity(final_project) != (name, catalog, schema, source):
        raise DiscoveryError("invalid_metadata")
    if (mdl.get("catalog"), mdl.get("schema"), mdl.get("dataSource")) != (
        catalog,
        schema,
        source,
    ):
        raise DiscoveryError("invalid_metadata")
    models = _named_array(mdl.get("models"))
    relationships = _named_array(mdl.get("relationships"))
    views = _named_array(mdl.get("views", []))
    cubes = _named_array(mdl.get("cubes", []))
    discovered: list[DiscoveredModel] = []
    codes: set[str] = set()
    for model in models:
        model_name = model["name"]
        properties = model.get("properties", {})
        if not isinstance(properties, dict):
            raise DiscoveryError("invalid_metadata")
        display = properties.get("displayName")
        display = (
            _text(display, 150)
            if isinstance(display, str) and display.strip()
            else _text(model_name, 150)
        )
        description_value = properties.get("description")
        description = (
            _text(description_value, 4000, blank=True)
            if isinstance(description_value, str)
            else ""
        )
        reference = model.get("tableReference")
        ref_sql = model.get("refSql")
        if reference is not None:
            if not isinstance(reference, dict) or ref_sql not in (None, ""):
                raise DiscoveryError("invalid_metadata")
            physical_catalog = _text(reference.get("catalog"), 128)
            physical_schema = _text(reference.get("schema"), 128)
            table = _text(reference.get("table"), 128)
        else:
            _text(ref_sql, MAX_METADATA_BYTES)
            physical_catalog, physical_schema, table = catalog, schema, ""
        columns = model.get("columns")
        if not isinstance(columns, list) or not all(
            isinstance(column, dict) for column in columns
        ):
            raise DiscoveryError("invalid_metadata")
        if any(
            not isinstance(column.get("name"), str) or not column["name"].strip()
            for column in columns
        ):
            raise DiscoveryError("invalid_metadata")
        prefix = slugify(model_name, allow_unicode=False)[:47].rstrip("-") or "model"
        code = f"{prefix}-{_digest([source_key, name, model_name])[:16]}"
        if code in codes:
            raise DiscoveryError("apply_conflict")
        codes.add(code)
        discovered.append(
            DiscoveredModel(
                model_name,
                code,
                display,
                description,
                physical_catalog,
                physical_schema,
                table,
                _digest(_structural(model)),
            )
        )
    normalized_mdl = dict(
        mdl, models=models, relationships=relationships, views=views, cubes=cubes
    )
    structure = {
        "catalog": catalog,
        "schema": schema,
        "dataSource": source,
        "relationships": relationships,
        "views": views,
        "cubes": cubes,
    }
    return CatalogSnapshot(
        source_key,
        name,
        tuple(discovered),
        _digest(_structural(structure)),
        _digest({"project": project, "mdl": normalized_mdl}),
        _json_bytes({"project": project, "mdl": normalized_mdl}).decode("utf-8"),
    )


_SCHEMA_ANNOTATIONS = frozenset({"title", "description", "$schema"})


def _argument_free_schema(schema: object) -> bool:
    """True when get_mdl declares no arguments.

    JSON Schema annotations such as ``title`` are not arguments. Wren OSS MCP
    0.17.1 emits ``title: get_mdlArguments`` on an otherwise empty object.
    """
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return False
    if schema.get("properties") != {}:
        return False
    if schema.get("required", []) != []:
        return False
    if "additionalProperties" in schema and schema["additionalProperties"] is not False:
        return False
    allowed = {"type", "properties", "required", "additionalProperties"} | set(
        _SCHEMA_ANNOTATIONS
    )
    return set(schema).issubset(allowed)


async def read_catalog_snapshot(
    client: MCPToolset, *, source_key: str
) -> CatalogSnapshot:
    """Read and validate metadata through an already-entered connection."""
    tools = await client.list_tools()
    matches = [tool for tool in tools if tool.name == "get_mdl"]
    if len(matches) != 1 or not _argument_free_schema(matches[0].input_schema):
        raise DiscoveryError("unsupported_metadata")
    try:
        first = await client.read_resource("wren://project")
        mdl = await client.direct_call_tool("get_mdl", {})
        second = await client.read_resource("wren://project")
    except MCPError:
        raise DiscoveryError("unsupported_metadata") from None
    except StreamMCPError as exc:
        code = "invalid_metadata" if "byte limit" in str(exc) else "connection_failed"
        raise DiscoveryError(code) from None
    return catalog_snapshot(source_key, first, mdl, second)


async def discover_catalog() -> CatalogSnapshot:
    """Read only project identity and the compiled MDL from the configured MCP."""
    endpoint = settings.WREN_MCP_URL.strip()
    source_key = configured_source_key()
    if source_key is None:
        raise DiscoveryError("connection_failed")
    try:
        async with asyncio.timeout(20):
            async with MCPToolset(
                Client(endpoint, mode="legacy", init_timeout=10, timeout=15),
                tool_error_behavior="error",
                include_instructions=False,
            ) as client:
                return await read_catalog_snapshot(client, source_key=source_key)
    except DiscoveryError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise DiscoveryError("invalid_metadata") from None
    except Exception:
        raise DiscoveryError("connection_failed") from None
