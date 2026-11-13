"""Async chat service bridging Django to the PydanticAI agent graph."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from typing import Any, Final
from uuid import UUID, uuid4

from asgiref.sync import sync_to_async
from django.conf import settings
from pydantic_ai import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelSettings,
)
from pydantic_ai.capabilities import Hooks
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelAuthenticationError,
    ModelContextWindowExceededError,
    ModelNotFoundError,
    ModelRateLimitError,
    ModelRequestRejectedError,
    ModelRequestTimeout,
    ModelTimeoutError,
    UnexpectedModelBehavior,
    UserError,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
)
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext

from accounts.authorization import active_dataset_queryset

from .activity import ActivityCollector, ActivityUpdate, TurnActivity
from .agent import _GRANT_LIMIT, build_agent, granted_dataset_instructions
from .attachments import (
    AttachmentError,
    PreparedAttachment,
    dehydrate_messages,
    has_native_attachments,
    hydrate_history,
    hydrate_user_content,
    serialize_messages,
)
from .context_budget import ContextBudgetExceeded, make_budget_hooks
from .mcp import REVIEWED_TOOLS, WrenMCPError, open_wren_toolset
from .mcp_policy import AnalyticsRun, DevScope, require_scope_current

# Coalesce provider deltas into small groups instead of one event per token.
STREAM_DEBOUNCE_SECONDS = 0.05

# Public model labels are display-only metadata; refuse anything exotic.
MAX_MODEL_LABEL_LENGTH: Final = 200

# The installed OpenAI adapter refuses binary payloads it cannot map with this
# message; only that documented failure is reported as a file-input problem.
UNSUPPORTED_BINARY_PREFIX: Final = "Unsupported binary content type:"

# Reviewed native tools whose calls are worth a public step. Any other tool
# name contributes no public activity. The public step kind stays ``wren_tool``.
WAREHOUSE_ACTIVITY_TOOLS: Final = REVIEWED_TOOLS

_AUTHORIZATION_CODES: Final = (
    "authentication_required",
    "authorization_changed",
    "department_access_revoked",
    "history_quarantined",
)


class ChatError(Exception):
    """Application error carrying only a stable public error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TextDelta:
    """Incremental assistant text produced by the provider stream."""

    text: str
    analytics_scope: DevScope | None = None


@dataclass(frozen=True)
class CompletedReply:
    """A finished turn: the final reply, its history, and public metadata."""

    reply: str
    history_json: str
    requested_model: str | None = None
    resolved_model: str | None = None
    duration_ms: int | None = None
    output_tokens: int | None = None
    activity: TurnActivity | None = None
    # True only when this run's last provider response finished on the output
    # cap; the visible text above is then a deliberate partial answer.
    output_limit_reached: bool = False
    analytics_scope: DevScope | None = None


def validate_provider_settings() -> tuple[str, str, str]:
    """Return the configured gateway settings, raising when incomplete."""
    base_url = settings.LLM_BASE_URL.strip()
    api_key = settings.LLM_API_KEY.strip()
    model_name = settings.LLM_MODEL.strip()
    if not base_url or not api_key or not model_name:
        raise ChatError("provider_not_configured")
    return base_url, api_key, model_name


def load_grant_instructions(
    *,
    owner_id: int,
    department_id: UUID | None,
    analytics_available: bool = False,
) -> str:
    """Load the granted-dataset list this chat may name, and nothing more.

    The list comes from the same eligibility query the management surfaces use,
    so it can only contain datasets the owner may currently see in this
    department. Only display name, code, and description are read. This helper
    does not read settings or open a warehouse. ``analytics_available`` omits
    the unavailable sentence only for a run that already has tools.
    """
    if department_id is None:
        return granted_dataset_instructions(department_bound=False, datasets=())

    rows = list(
        active_dataset_queryset(
            user_id=owner_id,
            department_id=department_id,
        ).values_list("display_name", "code", "description")[: _GRANT_LIMIT + 1]
    )
    return granted_dataset_instructions(
        department_bound=True,
        datasets=rows[:_GRANT_LIMIT],
        truncated=len(rows) > _GRANT_LIMIT,
        analytics_available=analytics_available,
    )


def load_history(history_json: str) -> list:
    """Validate stored canonical history, raising on unusable data."""
    try:
        return ModelMessagesTypeAdapter.validate_json(history_json)
    except Exception:
        raise ChatError("conversation_invalid") from None


def public_model_label(value: object) -> str | None:
    """Return a safe display label for a configured model, or ``None``."""
    if not isinstance(value, str):
        return None
    label = value.strip()
    if not label or len(label) > MAX_MODEL_LABEL_LENGTH:
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in label):
        return None
    return label


def _leaves(error: BaseException) -> list[BaseException]:
    """Flatten an exception group into the individual failures it carries."""
    if isinstance(error, BaseExceptionGroup):
        return [leaf for inner in error.exceptions for leaf in _leaves(inner)]
    return [error]


def _chat_error_code(errors: Sequence[ChatError]) -> str:
    """Prefer an authorization failure over any other application code."""
    for code in _AUTHORIZATION_CODES:
        if any(error.code == code for error in errors):
            return code
    return errors[0].code


def _grouped_run_error(group: BaseExceptionGroup) -> ChatError | None:
    """Map a grouped failure onto one fixed code.

    Authorization wins. A cancellation or other base exception is not mapped.
    A Wren failure becomes ``mcp_error`` only when nothing above it applies.
    """
    leaves = _leaves(group)
    if any(not isinstance(leaf, Exception) for leaf in leaves):
        return None
    chats = [leaf for leaf in leaves if isinstance(leaf, ChatError)]
    if chats:
        return ChatError(_chat_error_code(chats))
    if any(isinstance(leaf, ContextBudgetExceeded) for leaf in leaves):
        return ChatError("context_limit")
    if any(isinstance(leaf, WrenMCPError) for leaf in leaves):
        return ChatError("mcp_error")
    return None


def _captured_scope(guarded: object) -> DevScope | None:
    if guarded is None:
        return None
    scope = getattr(guarded, "used_scope", None)
    if scope is None:
        scope = getattr(guarded, "_scope", None)
    return scope


def _used_scope(guarded: object) -> DevScope | None:
    if guarded is None:
        return None
    return getattr(guarded, "used_scope", None)


async def _recheck_captured(guarded: object, run: AnalyticsRun | None) -> None:
    if run is None:
        return
    scope = _captured_scope(guarded)
    if scope is None:
        return
    await sync_to_async(require_scope_current, thread_sensitive=True)(
        run=run, scope=scope
    )


def _rejection_code(native_input: bool) -> str:
    """Map a rejected provider request onto its fixed public code.

    A chat that carries PDF or image input is most likely rejected because the
    configured model cannot read that payload, so it gets the file-specific
    message; everything else stays a generic provider failure.
    """
    return "attachment_model_error" if native_input else "provider_error"


def _tool_status(event: FunctionToolCallEvent) -> str | None:
    """Map a tool-call event onto a public step status.

    ``None`` means the call was never executed, so no step is recorded.
    """
    if event.part.tool_name not in WAREHOUSE_ACTIVITY_TOOLS:
        return None
    if event.args_valid is True:
        return "running"
    if event.args_valid is False:
        return "failed"
    return None


def _canonical_history(messages: Sequence[ModelMessage]) -> str:
    """Serialize the canonical, attachment-marker-only view of a run."""
    history_json = serialize_messages(dehydrate_messages(messages))
    load_history(history_json)
    return history_json


def _without_unexecuted_calls(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    """Drop tool calls whose tool node never ran, keeping observed text.

    A response that finished on the output cap cannot be followed by a tool
    node, so its calls were never executed. Persisting them would leave a call
    the conversation never satisfied.
    """
    if not messages:
        return list(messages)
    last = messages[-1]
    if not isinstance(last, ModelResponse):
        return list(messages)
    kept = [part for part in last.parts if not isinstance(part, ToolCallPart)]
    if len(kept) == len(last.parts):
        return list(messages)
    if not kept:
        return list(messages[:-1])
    return [*messages[:-1], replace(last, parts=kept)]


def _format_tool_args_summary(args_json_str: str, max_length: int = 160) -> str:
    """Format tool call arguments into a concise preview string."""
    raw = (args_json_str or "").strip()
    if not raw or raw in {"{}", "null"}:
        return "({})"
    if len(raw) > max_length:
        raw = raw[: max_length - 3] + "..."
    return f"({raw})"


async def stream_answer(
    message: str,
    *,
    history_json: str | None = None,
    turn_id: UUID | None = None,
    attachments: Sequence[PreparedAttachment] = (),
    stored_attachments: Mapping[UUID, PreparedAttachment] | None = None,
    show_reasoning_detail: bool = False,
    grant_instructions: str = "",
    check_authorization: Callable[[], Awaitable[None]] | None = None,
    analytics_run: AnalyticsRun | None = None,
) -> AsyncIterator[TextDelta | ActivityUpdate | CompletedReply]:
    """Stream one model turn, ending with the completed, persistable reply.

    Deltas are real provider output as it arrives, including any text that
    precedes a tool call. The graph is iterated to completion so tool rounds
    always finish, canonical history is serialized from the finished run, and
    the terminal ``CompletedReply`` is yielded exactly once.

    Attachment payloads are hydrated for the model and never serialized: the
    stored prompt keeps only its message text and a marker naming the rows.
    Every model round is checked against the application's context budget, and
    a provider response that finishes on the output cap stops the graph while
    keeping the text already shown.

    Activity updates describe only fixed enums and the app-generated turn id:
    no tool names, arguments, results, provider ids, or reasoning text. A run
    that attaches tools stays on that generic path even when reasoning detail
    was requested.

    An optional authorization callback runs before every model request,
    including every later round. ``None`` is only for direct offline callers.
    ``analytics_run`` is the trusted server context for one synthetic DEV run.
    ``None``, including every regeneration, stays tool-free and does not open
    Wren. After a tool use, the captured scope is rechecked before the next
    model round, protected text, and completion.
    """
    base_url, api_key, model_name = validate_provider_settings()

    history = [] if history_json is None else load_history(history_json)
    stored = {} if stored_attachments is None else dict(stored_attachments)
    used: set[UUID] = set()
    try:
        hydrated_history = hydrate_history(history, stored, used=used)
        prompt: str | list[Any] = (
            hydrate_user_content(
                message,
                [attachment.id for attachment in attachments],
                {attachment.id: attachment for attachment in attachments},
            )
            if attachments
            else message
        )
    except AttachmentError as exc:
        raise ChatError(exc.code) from None

    native_input = has_native_attachments(
        [*attachments, *(stored[attachment_id] for attachment_id in used)]
    )

    context_window = settings.LLM_CONTEXT_WINDOW
    max_output_tokens = settings.LLM_MAX_OUTPUT_TOKENS
    budget_hooks = make_budget_hooks(
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )
    capabilities: list[Any] = [budget_hooks]
    held_guard: dict[str, Any] = {"toolset": None}
    tool_attempted = False
    reviewed_success = False

    async def before_model_request(
        ctx: RunContext[None],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        if check_authorization is not None:
            await check_authorization()
        if tool_attempted:
            await _recheck_captured(held_guard["toolset"], analytics_run)
        return request_context

    if check_authorization is not None or analytics_run is not None:
        capabilities.append(Hooks(before_model_request=before_model_request))
    collector = ActivityCollector(turn_id if turn_id is not None else uuid4())
    running_tools: dict[str, int] = {}
    running_tools_started: dict[str, float] = {}

    chunks: list[str] = []
    updated_history_json = ""
    final_output: str | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    capped = False
    capped_usage: Any = None
    started = time.perf_counter()
    try:
        async with AsyncExitStack() as stack:
            guarded = await stack.enter_async_context(
                open_wren_toolset(run=analytics_run)
            )
            held_guard["toolset"] = guarded
            show_detail = show_reasoning_detail and guarded is None
            active_grant = grant_instructions
            if guarded is not None and analytics_run is not None:
                active_grant = await sync_to_async(
                    load_grant_instructions, thread_sensitive=True
                )(
                    owner_id=analytics_run.owner_id,
                    department_id=analytics_run.department_id,
                    analytics_available=True,
                )
            agent = await stack.enter_async_context(
                build_agent(
                    base_url=base_url,
                    api_key=api_key,
                    model_name=model_name,
                    toolsets=(guarded,) if guarded is not None else (),
                )
            )
            run = await stack.enter_async_context(
                agent.iter(
                    prompt,
                    message_history=hydrated_history,
                    model_settings=ModelSettings(max_tokens=max_output_tokens),
                    capabilities=capabilities,
                    instructions=active_grant or None,
                )
            )
            async for node in run:
                if agent.is_model_request_node(node):
                    node_start = time.perf_counter()
                    update = collector.start(
                        "model",
                        title="Thinking..." if show_detail else None,
                    )
                    if update is not None:
                        yield update
                    step = update.step if update is not None else None

                    async with node.stream(run.ctx) as stream:
                        async for text in stream.stream_text(
                            delta=True,
                            debounce_by=STREAM_DEBOUNCE_SECONDS,
                        ):
                            if not text:
                                continue
                            if tool_attempted:
                                await _recheck_captured(
                                    held_guard["toolset"], analytics_run
                                )
                            used_now = _used_scope(held_guard["toolset"])
                            if reviewed_success and used_now is None:
                                raise ChatError("authorization_changed")
                            chunks.append(text)
                            yield TextDelta(text, analytics_scope=used_now)
                        finish_reason = stream.response.finish_reason
                        request_usage = (
                            stream.usage if finish_reason == "length" else None
                        )

                    if step is not None:
                        node_duration_ms = (
                            int((time.perf_counter() - node_start) * 1000)
                            if show_detail
                            else None
                        )
                        final_title: str | None = None
                        if show_detail:
                            parts = getattr(stream.response, "parts", ())
                            thinking = next(
                                (
                                    p.content.strip()
                                    for p in parts
                                    if isinstance(p, ThinkingPart)
                                    and p.content
                                    and p.content.strip()
                                ),
                                None,
                            )
                            if thinking:
                                final_title = thinking
                            else:
                                has_tools = any(
                                    isinstance(p, ToolCallPart) for p in parts
                                )
                                if has_tools:
                                    text_part = next(
                                        (
                                            p.content.strip()
                                            for p in parts
                                            if isinstance(p, TextPart)
                                            and p.content
                                            and p.content.strip()
                                        ),
                                        None,
                                    )
                                    final_title = (
                                        text_part if text_part else "Analyzing data..."
                                    )
                                else:
                                    final_title = "Response prepared"

                        completed_step = collector.finish(
                            step["id"],
                            "complete",
                            title=final_title,
                            duration_ms=node_duration_ms,
                        )
                        if completed_step is not None:
                            yield completed_step

                    if finish_reason == "length":
                        # The cap is an ordinary partial result: stop before any
                        # tool node and keep the text already delivered.
                        capped = True
                        capped_usage = request_usage
                        break
                    continue

                if not agent.is_call_tools_node(node):
                    continue

                async with node.stream(run.ctx) as events:
                    async for event in events:
                        if isinstance(event, FunctionToolCallEvent):
                            tool_attempted = True
                            status = _tool_status(event)
                            if status is None:
                                continue
                            tool_name = event.part.tool_name
                            if show_detail:
                                args_summary = _format_tool_args_summary(
                                    event.part.args_as_json_str()
                                )
                                tool_title = (
                                    f"Assembling Tool Call: {tool_name}{args_summary}"
                                )
                            else:
                                tool_title = None
                            update = collector.start(
                                "wren_tool", status=status, title=tool_title
                            )
                            if update is not None:
                                yield update
                            if status != "running" or update is None:
                                continue
                            if update.step is not None:
                                running_tools[event.part.tool_call_id] = update.step[
                                    "id"
                                ]
                                running_tools_started[event.part.tool_call_id] = (
                                    time.perf_counter()
                                )
                            continue

                        if isinstance(event, FunctionToolResultEvent):
                            tool_attempted = True
                            succeeded = event.part.outcome == "success"
                            reviewed = (
                                event.part.tool_name in REVIEWED_TOOLS and succeeded
                            )
                            if reviewed:
                                reviewed_success = True
                            step_id = running_tools.pop(event.part.tool_call_id, None)
                            if step_id is not None:
                                tool_start = running_tools_started.pop(
                                    event.part.tool_call_id, None
                                )
                                tool_duration_ms = (
                                    int((time.perf_counter() - tool_start) * 1000)
                                    if (tool_start and show_detail)
                                    else None
                                )
                                update = collector.finish(
                                    step_id,
                                    "complete" if succeeded else "failed",
                                    duration_ms=tool_duration_ms,
                                )
                                if update is not None:
                                    yield update
                            if reviewed and _used_scope(held_guard["toolset"]) is None:
                                raise ChatError("authorization_changed")

            messages = run.all_messages()
            if capped:
                updated_history_json = _canonical_history(
                    _without_unexecuted_calls(messages)
                )
                if getattr(capped_usage, "unmeasured_requests", 0) == 0:
                    output_tokens = capped_usage.output_tokens
            else:
                result = run.result
                if result is not None:
                    updated_history_json = _canonical_history(messages)
                    final_output = result.output
                    usage = result.usage
                    if getattr(usage, "unmeasured_requests", 0) == 0:
                        output_tokens = usage.output_tokens
            duration_ms = int((time.perf_counter() - started) * 1000)
    except ChatError:
        raise
    except BaseExceptionGroup as group:
        mapped = _grouped_run_error(group)
        if mapped is not None:
            raise mapped from None
        raise
    except WrenMCPError:
        raise ChatError("mcp_error") from None
    except (ModelRequestTimeout, ModelTimeoutError):
        raise ChatError("provider_timeout") from None
    except ModelRateLimitError:
        raise ChatError("provider_rate_limited") from None
    except (ContextBudgetExceeded, ModelContextWindowExceededError):
        raise ChatError("context_limit") from None
    except (ModelAuthenticationError, ModelNotFoundError):
        raise ChatError("provider_error") from None
    except (ModelRequestRejectedError, UserError):
        # Rejected-request subclasses are matched above, so this is the general
        # "provider refused the request" case.
        raise ChatError(_rejection_code(native_input)) from None
    except (ModelAPIError, UnexpectedModelBehavior):
        raise ChatError("provider_error") from None
    except RuntimeError as exc:
        # The OpenAI adapter raises a bare RuntimeError for a payload it cannot
        # map; provider API failures never reach this clause.
        if native_input and str(exc).startswith(UNSUPPORTED_BINARY_PREFIX):
            raise ChatError("attachment_model_error") from None
        raise

    guarded = held_guard["toolset"]
    if tool_attempted:
        await _recheck_captured(guarded, analytics_run)
    verified = _used_scope(guarded)
    if reviewed_success and verified is None:
        raise ChatError("authorization_changed")

    reply = "".join(chunks)
    if capped:
        if not reply.strip():
            # Reasoning or tool arguments consumed the whole cap; an empty
            # answer is never invented or committed.
            raise ChatError("output_limit")
        output_limit_reached = True
    else:
        if final_output is None or not final_output.strip() or not reply.strip():
            raise ChatError("provider_error")
        output_limit_reached = False

    try:
        activity = collector.snapshot()
    except Exception:
        activity = None

    yield CompletedReply(
        reply=reply,
        history_json=updated_history_json,
        requested_model=public_model_label(model_name),
        resolved_model=None,
        duration_ms=duration_ms,
        output_tokens=output_tokens,
        activity=activity,
        output_limit_reached=output_limit_reached,
        analytics_scope=verified,
    )
