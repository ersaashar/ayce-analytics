"""Operator smoke checks for synthetic Jaffle DEV Wren analytics.

Mutually exclusive actions: ``--diagnose-only`` (default), ``--service-only``,
and ``--execute``. Provisioning and the authenticated chat use only this
worktree's fresh database. Passwords, cookies, endpoints, and raw transport
errors are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit
from uuid import UUID

OPERATOR_USERNAME = "wren-dev-operator"
CONTROL_SQL = "SELECT COUNT(*) AS total_orders FROM orders"
QUESTION = "How many orders are there?"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def fail(message: str) -> NoReturn:
    print(f"smoke: {message}", file=sys.stderr)
    raise SystemExit(1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synthetic Jaffle DEV Wren smoke checks.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--diagnose-only",
        action="store_true",
        help="Verify live identity and inventory, then stop.",
    )
    group.add_argument(
        "--service-only",
        action="store_true",
        help="Run one uncommitted stream_answer against the real provider.",
    )
    group.add_argument(
        "--execute",
        action="store_true",
        help="Run authenticated committed chat against a live ASGI server.",
    )
    parser.add_argument(
        "--provision",
        action="store_true",
        help="Create fresh audited principals in this worktree database.",
    )
    parser.add_argument("--database", help="Must be this worktree's db.sqlite3.")
    parser.add_argument("--base-url", help="Loopback ASGI origin for --execute.")
    parser.add_argument(
        "--session-file",
        help="Private cookie JSON outside the repository. Also writes -partial.json.",
    )
    return parser


def _setup() -> None:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def _worktree() -> Path:
    return Path(__file__).resolve().parent.parent


def _require_database(raw: str) -> None:
    from django.conf import settings

    requested = Path(raw).expanduser().resolve()
    configured = Path(settings.DATABASES["default"]["NAME"]).resolve()
    worktree_db = (_worktree() / "db.sqlite3").resolve()
    if requested != configured or requested != worktree_db or not requested.is_file():
        fail("database path does not match this worktree db.sqlite3")


def _endpoint() -> str:
    from config.settings import dev_jaffle_endpoint

    endpoint = dev_jaffle_endpoint()
    if not endpoint:
        fail(
            "DEV analytics gates are not satisfied. "
            "Check AYCE_ANALYTICS_MODE, DJANGO_DEBUG, AYCE_DEV_JAFFLE_CONFIRMED, "
            "AYCE_DEV_WREN_PROJECT, AYCE_DEV_WREN_MCP_URL, and WREN_MCP_URL."
        )
    return endpoint


def _as_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    return value


def _project_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
        return value[0]
    return None


def _order_count(value: Any) -> int | None:
    payload = _as_object(value)
    if payload is None:
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    row = rows[0]
    cell: Any = None
    if isinstance(row, dict):
        cell = row.get("total_orders")
        if cell is None and len(row) == 1:
            cell = next(iter(row.values()))
    elif isinstance(row, list) and len(row) == 1:
        cell = row[0]
    if isinstance(cell, bool) or cell is None:
        return None
    if isinstance(cell, int):
        return cell
    if isinstance(cell, float) and cell.is_integer():
        return int(cell)
    if isinstance(cell, str) and cell.isdigit():
        return int(cell)
    return None


async def _diagnose(endpoint: str) -> tuple[tuple[str, ...], int]:
    from chat.mcp import _compatible_tools, _native_toolset

    try:
        async with asyncio.timeout(90):
            async with _native_toolset(endpoint) as client:
                admitted = _compatible_tools(await client.list_tools())
                if not admitted or "run_sql" not in admitted:
                    fail("incompatible tool inventory")
                project_raw = _project_text(
                    await client.read_resource("wren://project")
                )
                project = _as_object(project_raw) if project_raw is not None else None
                if project is None or (
                    project.get("name"),
                    project.get("catalog"),
                    project.get("schema"),
                    project.get("data_source"),
                ) != ("jaffle-wren", "wren", "public", "duckdb"):
                    fail("project identity does not match the synthetic Jaffle catalog")
                mdl = _as_object(await client.direct_call_tool("get_mdl", {}))
                models = mdl.get("models") if mdl is not None else None
                names = (
                    {row.get("name") for row in models}
                    if isinstance(models, list)
                    else set()
                )
                if (
                    mdl is None
                    or mdl.get("dataSource") != "duckdb"
                    or names != {"customers", "orders"}
                ):
                    fail("project identity does not match the synthetic Jaffle catalog")
                control = await client.direct_call_tool(
                    "run_sql",
                    {"sql": CONTROL_SQL, "limit": 1},
                )
    except SystemExit:
        raise
    except Exception:
        fail("Wren MCP operation failed.")
    count = _order_count(control)
    if count is None:
        fail("control query returned an unexpected shape")
    return tuple(sorted(admitted)), count


def _print_inventory(tools: tuple[str, ...], count: int) -> None:
    print("status=ok")
    print("project=jaffle-wren")
    print("catalog=wren")
    print("schema=public")
    print("data_source=duckdb")
    print("models=customers,orders")
    print("tools=" + ",".join(tools))
    print(f"control_total_orders={count}")


def _revision() -> int:
    from accounts.services import current_state

    return current_state()["revision"]


def _operator_id() -> int:
    from django.contrib.auth.models import User

    operator = User.objects.filter(username=OPERATOR_USERNAME).first()
    if operator is None or not operator.is_active or not operator.is_superuser:
        fail("wren-dev-operator must be an active native superuser")
    return operator.pk


def _suffix() -> str:
    from django.contrib.auth.models import User

    from accounts.models import Department

    for _ in range(5):
        suffix = secrets.token_hex(4)
        usernames = (f"wren-full-{suffix}", f"wren-partial-{suffix}")
        names = (f"Jaffle Sales {suffix}", f"Jaffle Marketing {suffix}")
        if User.objects.filter(username__in=usernames).exists():
            continue
        if Department.objects.filter(name__in=names).exists():
            continue
        return suffix
    fail("could not allocate a fresh suffix")


def _datasets() -> tuple[UUID, UUID]:
    from datasets.discovery import configured_source_key
    from datasets.models import Dataset

    source_key = configured_source_key()
    rows = {
        row.model_name: row.pk
        for row in Dataset.objects.filter(
            source_key=source_key,
            project_key="jaffle-wren",
            model_name__in=("customers", "orders"),
            is_active=True,
            is_available=True,
        )
    }
    if set(rows) != {"customers", "orders"}:
        fail("synthetic catalog is missing customers or orders")
    return rows["customers"], rows["orders"]


def _provision() -> dict[str, Any]:
    from accounts.services import (
        MembershipInput,
        create_department,
        create_user,
        current_state,
        replace_user_memberships,
    )
    from datasets.services import refresh_catalog, replace_department_datasets

    actor_id = _operator_id()
    try:
        asyncio.run(
            refresh_catalog(actor_id=actor_id, revision=current_state()["revision"])
        )
        customers_id, orders_id = _datasets()
        suffix = _suffix()
        sales = create_department(
            actor_id=actor_id,
            revision=_revision(),
            name=f"Jaffle Sales {suffix}",
            description="",
        )
        marketing = create_department(
            actor_id=actor_id,
            revision=_revision(),
            name=f"Jaffle Marketing {suffix}",
            description="",
        )
        sales_id = UUID(sales["id"])
        marketing_id = UUID(marketing["id"])
        both = (customers_id, orders_id)
        for department_id in (sales_id, marketing_id):
            replace_department_datasets(
                actor_id=actor_id,
                revision=_revision(),
                department_id=department_id,
                dataset_ids=both,
            )
        passwords = {
            "full": secrets.token_urlsafe(32),
            "partial": secrets.token_urlsafe(32),
        }
        users = {}
        for kind, last_name in (("full", "Full"), ("partial", "Partial")):
            created = create_user(
                actor_id=actor_id,
                revision=_revision(),
                username=f"wren-{kind}-{suffix}",
                first_name="Wren",
                last_name=last_name,
                email=f"wren-{kind}-{suffix}@example.invalid",
                password=passwords[kind],
            )
            users[kind] = created
        replace_user_memberships(
            actor_id=actor_id,
            revision=_revision(),
            user_id=users["full"]["id"],
            memberships=(
                MembershipInput(sales_id, "staff", True, both),
                MembershipInput(marketing_id, "staff", True, (orders_id,)),
            ),
        )
        replace_user_memberships(
            actor_id=actor_id,
            revision=_revision(),
            user_id=users["partial"]["id"],
            memberships=(MembershipInput(sales_id, "staff", True, (orders_id,)),),
        )
    except SystemExit:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None)
        fail(code if isinstance(code, str) else "provisioning failed")
    return {
        "suffix": suffix,
        "full_id": users["full"]["id"],
        "partial_id": users["partial"]["id"],
        "full_username": f"wren-full-{suffix}",
        "partial_username": f"wren-partial-{suffix}",
        "full_password": passwords["full"],
        "partial_password": passwords["partial"],
        "sales_id": sales_id,
        "customers_id": customers_id,
        "orders_id": orders_id,
    }


def _require_scope(fixtures: dict[str, Any]) -> Any:
    from chat.mcp_policy import resolve_dev_scope

    scope = resolve_dev_scope(
        user_id=fixtures["full_id"],
        department_id=fixtures["sales_id"],
    )
    if scope is None:
        fail("full principal is not eligible for synthetic analytics")
    partial = resolve_dev_scope(
        user_id=fixtures["partial_id"],
        department_id=fixtures["sales_id"],
    )
    if partial is not None:
        fail("partial principal resolved a synthetic scope")
    return scope


def _tool_parts(history_json: str) -> tuple[list[Any], list[Any]]:
    from pydantic_ai.messages import (
        ModelMessagesTypeAdapter,
        ToolCallPart,
        ToolReturnPart,
    )

    from chat.mcp import REVIEWED_TOOLS

    calls: list[Any] = []
    returns: list[Any] = []
    messages = ModelMessagesTypeAdapter.validate_json(history_json)
    for message in messages:
        for part in getattr(message, "parts", ()):
            if isinstance(part, ToolCallPart):
                if part.tool_name not in REVIEWED_TOOLS:
                    fail("history contains an unreviewed tool call")
                calls.append(part)
            elif isinstance(part, ToolReturnPart):
                returns.append(part)
    return calls, returns


def _sql_text(part: Any) -> str:
    args = part.args
    if isinstance(args, str):
        args = _as_object(args)
    if not isinstance(args, dict):
        return ""
    sql = args.get("sql")
    return sql if isinstance(sql, str) else ""


def _matched_sql(history_json: str, count: int) -> str:
    calls, returns = _tool_parts(history_json)
    by_id = {part.tool_call_id: part for part in returns}
    matched = ""
    saw = False
    for call in calls:
        if call.tool_name != "run_sql":
            continue
        saw = True
        returned = by_id.get(call.tool_call_id)
        if returned is None:
            fail("run_sql call has no matching return")
        sql = _sql_text(call)
        if sql and _order_count(returned.content) == count:
            matched = sql
    if not saw or not matched:
        fail("history is missing a run_sql result")
    return matched


def _line(name: str, value: object) -> None:
    if isinstance(value, str):
        print(f"{name}={json.dumps(value, ensure_ascii=False)}")
        return
    print(f"{name}={value}")


async def _service_only(fixtures: dict[str, Any], count: int) -> None:
    from asgiref.sync import sync_to_async

    from chat import service, threads
    from chat.mcp_policy import AnalyticsRun
    from chat.models import ChatThread, ChatTurn

    owner_id = fixtures["full_id"]
    department_id = fixtures["sales_id"]
    thread = await sync_to_async(threads.create_thread, thread_sensitive=True)(
        owner_id=owner_id,
        department_id=department_id,
    )
    epoch = await sync_to_async(threads.current_epoch, thread_sensitive=True)()
    token = await sync_to_async(threads.acquire_run, thread_sensitive=True)(
        owner_id=owner_id,
        thread_id=thread.pk,
        revision=thread.revision,
    )
    run = AnalyticsRun(
        owner_id=owner_id,
        thread_id=thread.pk,
        department_id=department_id,
        authorization_epoch=epoch,
        run_token=token,
    )

    async def check_authorization() -> None:
        await sync_to_async(threads.require_run_current, thread_sensitive=True)(
            owner_id=owner_id,
            thread_id=thread.pk,
            department_id=department_id,
            authorization_epoch=epoch,
            run_token=token,
        )

    instructions = await sync_to_async(
        service.load_grant_instructions,
        thread_sensitive=True,
    )(owner_id=owner_id, department_id=department_id)
    completed = None
    try:
        async for event in service.stream_answer(
            QUESTION,
            grant_instructions=instructions,
            check_authorization=check_authorization,
            analytics_run=run,
        ):
            if isinstance(event, service.CompletedReply):
                completed = event
    except TypeError as exc:
        if "analytics_run" in str(exc):
            fail("stream_answer does not accept analytics_run")
        fail("Wren MCP operation failed.")
    except service.ChatError as exc:
        fail(exc.code)
    except Exception:
        fail("Wren MCP operation failed.")
    finally:
        await sync_to_async(threads.release_run, thread_sensitive=True)(
            owner_id=owner_id,
            thread_id=thread.pk,
            run_token=token,
        )
    if completed is None:
        fail("service run did not complete")
    fresh = await sync_to_async(ChatThread.objects.get, thread_sensitive=True)(
        pk=thread.pk,
        owner_id=owner_id,
    )
    persisted = await sync_to_async(
        ChatTurn.objects.filter(thread_id=thread.pk).exists,
        thread_sensitive=True,
    )()
    if persisted or fresh.history_json != "[]" or fresh.active_run_token is not None:
        fail("diagnostic turn was persisted")
    sql = _matched_sql(completed.history_json, count)
    print("mode=service-only")
    print(f"principal_id={owner_id}")
    print(f"thread_id={thread.pk}")
    _line("sql", sql)
    print(f"tool_total_orders={count}")
    _line("answer", completed.reply)
    print("committed=false")
    print("lease_released=true")


def _origin(raw: str) -> str:
    parts = urlsplit(raw)
    origin = f"{parts.scheme}://{parts.netloc}"
    if (
        parts.scheme not in {"http", "https"}
        or parts.hostname not in LOOPBACK_HOSTS
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
        or origin != raw.rstrip("/")
    ):
        fail("base URL must be a loopback HTTP origin")
    return origin


def _outside_repo(path: Path) -> None:
    root = _worktree().resolve()
    resolved = path.resolve()
    if resolved == root or root in resolved.parents:
        fail("session file must stay outside the repository")
    if not resolved.parent.is_dir():
        fail("session file directory is missing")


def _write_private(path: Path, payload: list[dict[str, Any]]) -> None:
    _outside_repo(path)
    blob = json.dumps(payload).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, blob)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _cookie_records(client: Any, host: str) -> list[dict[str, Any]]:
    records = []
    for cookie in client.cookies.jar:
        domain = (cookie.domain or host).lstrip(".")
        same_site = cookie.get_nonstandard_attr("SameSite") or "Lax"
        records.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": domain,
                "path": cookie.path or "/",
                "httpOnly": bool(cookie.has_nonstandard_attr("HttpOnly")),
                "secure": bool(cookie.secure),
                "sameSite": same_site,
            }
        )
    names = {record["name"] for record in records}
    if not {"sessionid", "csrftoken"} <= names:
        fail("login did not set a session")
    return records


def _csrf(page: str) -> str:
    import re

    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
    if match is None:
        fail("login form is missing a CSRF token")
    return match.group(1)


def _error_code(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        return f"http_{response.status_code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str):
        return code
    return f"http_{response.status_code}"


def _login(client: Any, origin: str, username: str, password: str) -> None:
    page = client.get("/accounts/login/")
    if page.status_code != 200:
        fail("login page failed")
    response = client.post(
        "/accounts/login/",
        data={
            "username": username,
            "password": password,
            "csrfmiddlewaretoken": _csrf(page.text),
            "next": "/",
        },
        headers={"Referer": f"{origin}/accounts/login/"},
    )
    if response.status_code != 302 or response.headers.get("location") != "/":
        fail("login failed")


def _headers(client: Any, origin: str) -> dict[str, str]:
    token = client.cookies.get("csrftoken")
    if not token:
        fail("login did not set a CSRF cookie")
    return {
        "X-CSRFToken": token,
        "Referer": f"{origin}/",
        "Origin": origin,
    }


def _events(body: bytes) -> list[tuple[str, Any]]:
    text = body.decode("utf-8").replace("\r\n", "\n")
    if not text.startswith(": connected\n\n"):
        fail("SSE stream did not start with the connected comment")
    parsed: list[tuple[str, Any]] = []
    for block in text.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        event = ""
        data: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:") :].lstrip())
        if not data:
            fail("SSE frame is missing data")
        parsed.append((event, json.loads("\n".join(data))))
    names = [name for name, _payload in parsed]
    if names.count("done") != 1 or "error" in names or names[-1] != "done":
        fail("SSE stream did not complete cleanly")
    return parsed


def _activity_kinds(parsed: list[tuple[str, Any]]) -> list[str]:
    kinds = []
    for name, payload in parsed:
        if name != "activity" or not isinstance(payload, dict):
            continue
        step = payload.get("step")
        if isinstance(step, dict) and isinstance(step.get("kind"), str):
            kinds.append(step["kind"])
    return kinds


def _chat(client: Any, origin: str, department_id: UUID) -> tuple[str, str, list[str]]:
    created = client.post(
        "/api/threads/",
        json={"department_id": str(department_id)},
        headers=_headers(client, origin),
    )
    if created.status_code != 201:
        fail(_error_code(created))
    thread = created.json()["thread"]
    if thread.get("revision") != 0:
        fail("new thread revision was not 0")
    response = client.post(
        thread["messages_url"],
        json={"message": QUESTION, "revision": 0},
        headers=_headers(client, origin),
        timeout=300,
    )
    if response.status_code != 200:
        fail(_error_code(response))
    parsed = _events(response.content)
    deltas = [
        payload.get("text", "")
        for name, payload in parsed
        if name == "delta" and isinstance(payload, dict)
    ]
    done = parsed[-1][1]
    reply = done.get("reply") if isinstance(done, dict) else None
    if not deltas or not isinstance(reply, str) or not reply.strip():
        fail("SSE stream did not include an answer")
    return thread["id"], reply, _activity_kinds(parsed)


def _detail(client: Any, thread_id: str) -> dict[str, Any]:
    response = client.get(f"/api/threads/{thread_id}/")
    if response.status_code != 200:
        fail(_error_code(response))
    thread = response.json().get("thread")
    if not isinstance(thread, dict):
        fail("thread detail is missing")
    return thread


def _owned_thread(owner_id: int, thread_id: str) -> Any:
    from chat.models import ChatThread

    return ChatThread.objects.get(pk=thread_id, owner_id=owner_id)


def _provenance(owner_id: int, thread_id: str) -> list[Any]:
    from chat.models import ChatTurnDataset

    return list(
        ChatTurnDataset.objects.filter(
            turn__thread_id=thread_id,
            turn__thread__owner_id=owner_id,
        ).select_related("dataset")
    )


def _execute(
    fixtures: dict[str, Any],
    count: int,
    base_url: str,
    session_file: str | None,
    scope: Any,
) -> None:
    import httpx2
    from django.db import connections

    connections.close_all()
    origin = _origin(base_url)
    host = urlsplit(origin).hostname or "127.0.0.1"
    saved: dict[str, list[dict[str, Any]]] = {}
    for kind in ("full", "partial"):
        with httpx2.Client(
            base_url=origin,
            timeout=300,
            follow_redirects=False,
        ) as client:
            _login(
                client,
                origin,
                fixtures[f"{kind}_username"],
                fixtures[f"{kind}_password"],
            )
            thread_id, reply, kinds = _chat(client, origin, fixtures["sales_id"])
            detail = _detail(client, thread_id)
            saved[kind] = _cookie_records(client, host)
        turns = detail.get("turns")
        if not isinstance(turns, list) or len(turns) != 1:
            fail("thread detail did not include one turn")
        stored = _owned_thread(fixtures[f"{kind}_id"], thread_id)
        if stored.revision != 1 or detail.get("revision") != 1:
            fail("thread revision did not advance once")
        lease_open = (
            stored.active_run_token is not None
            or stored.active_run_expires_at is not None
        )
        if lease_open:
            fail("run lease was not cleared")
        provenance = _provenance(fixtures[f"{kind}_id"], thread_id)
        calls, returns = _tool_parts(stored.history_json)
        if kind == "full":
            if "wren_tool" not in kinds:
                fail("full principal stream did not include generic Wren activity")
            if turns[0].get("can_regenerate") is not False:
                fail("full principal regeneration was not disabled")
            sql = _matched_sql(stored.history_json, count)
            models = {row.dataset.model_name for row in provenance}
            if models != {"customers", "orders"} or len(provenance) != 2:
                fail("full principal provenance is not both synthetic models")
            print("mode=execute")
            print(f"full_principal_id={fixtures['full_id']}")
            print(f"full_thread_id={thread_id}")
            _line("sql", sql)
            print(f"tool_total_orders={count}")
            _line("answer", reply)
            print(f"snapshot_sha256={scope.snapshot_sha256}")
            print(f"structure_sha256={scope.structure_sha256}")
            for row in provenance:
                print(
                    "provenance="
                    f"{row.pk} {row.dataset_id} "
                    f"{row.definition_sha256} {row.structure_sha256}"
                )
            print("full_can_regenerate=false")
            print("full_lease_cleared=true")
            print("full_revision=1")
        else:
            if "wren_tool" in kinds or calls or returns or provenance:
                fail("partial principal recorded a native tool or provenance")
            if turns[0].get("can_regenerate") is not True:
                fail("partial principal regeneration was not available")
            print(f"partial_principal_id={fixtures['partial_id']}")
            print(f"partial_thread_id={thread_id}")
            print("partial_tools=0")
            print("partial_provenance=0")
            _line("partial_answer", reply)
            print("partial_lease_cleared=true")
            print("partial_revision=1")
    if session_file:
        target = Path(session_file).expanduser()
        partial = target.with_name(target.stem + "-partial.json")
        _write_private(target, saved["full"])
        _write_private(partial, saved["partial"])
        print("session_file=saved")


def main() -> None:
    args = _parser().parse_args()
    if args.execute:
        action = "execute"
    elif args.service_only:
        action = "service-only"
    else:
        action = "diagnose-only"
    if action == "diagnose-only" and (
        args.provision or args.database or args.base_url or args.session_file
    ):
        fail("diagnose-only does not accept provisioning or HTTP options")
    if action == "service-only" and (args.base_url or args.session_file):
        fail("service-only does not accept HTTP options")
    if action != "diagnose-only" and (not args.provision or not args.database):
        fail("service-only and execute require --provision and --database")
    if action == "execute" and not args.base_url:
        fail("execute requires --base-url")
    if action == "execute" and args.session_file:
        target = Path(args.session_file).expanduser()
        _outside_repo(target)
        _outside_repo(target.with_name(target.stem + "-partial.json"))
    _setup()
    if args.database:
        _require_database(args.database)
    tools, count = asyncio.run(_diagnose(_endpoint()))
    _print_inventory(tools, count)
    if action == "diagnose-only":
        return
    fixtures = _provision()
    scope = _require_scope(fixtures)
    if action == "service-only":
        asyncio.run(_service_only(fixtures, count))
        return
    _execute(fixtures, count, args.base_url, args.session_file, scope)


if __name__ == "__main__":
    main()
