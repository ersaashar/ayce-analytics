"""Application-owned context and output budgets for one chat run.

Two facts shape this module. The provider's ``max_tokens`` stops output, and a
run can make several model requests, so output is capped per completion rather
than cumulatively. And this app's ``OpenAIChatModel`` cannot count tokens
locally, so input is bounded by a deliberately conservative text/tool estimate
before the request and by provider-reported usage afterwards.

Native PDF/image cost stays provider-authoritative: it is omitted from the
estimate instead of being guessed, and a provider-reported overrun is still
rejected. This is a conservative guard, not an exact tokenizer or a hard
guarantee for multimodal input.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any, Final

from pydantic_ai.capabilities import Hooks
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextContent,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import UsageLimits

from .attachments import TEXT_CONTENT_MEDIA_TYPES

# Request framing, role markers and provider-side prompt scaffolding that the
# serialized message view does not show. Counted as tokens directly.
FRAMING_TOKENS: Final = 1024


class ContextBudgetExceeded(Exception):
    """The configured application budget cannot fit this request's input."""


def effective_context_window(model: Any, configured: int) -> int:
    """The smaller of the configured budget and a known model window.

    An unknown model window never widens the configured budget, and this never
    overrides a provider profile to claim a larger real capacity.
    """
    try:
        known = model.context_window
    except Exception:
        known = None
    if isinstance(known, bool) or not isinstance(known, int) or known <= 0:
        return configured
    return min(configured, known)


def input_allowance(model: Any, *, context_window: int, max_output_tokens: int) -> int:
    """Tokens available to input once this completion's output is reserved."""
    return effective_context_window(model, context_window) - max_output_tokens


def estimate_text_input_tokens(request_context: ModelRequestContext) -> int:
    """Conservative preflight estimate for one request's input.

    Text-like attachments are counted as their decoded text, native PDF/image
    payloads are omitted (never base64-counted), and application-only metadata
    is dropped. One estimated token per UTF-8 byte intentionally overestimates
    ordinary text; a prompt that fits may still be rejected.
    """
    counted = _counting_copy(request_context.messages)
    total = len(ModelMessagesTypeAdapter.dump_json(counted))
    parameters = request_context.model_request_parameters
    total += _instruction_bytes(parameters.instruction_parts)
    total += _tool_bytes(parameters.function_tools)
    total += _tool_bytes(parameters.output_tools)
    return total + FRAMING_TOKENS


def _counting_copy(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    counted: list[ModelMessage] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            counted.append(message)
            continue
        parts = []
        for part in message.parts:
            if isinstance(part, UserPromptPart) and isinstance(part.content, list):
                parts.append(_counted_prompt(part))
                continue
            parts.append(part)
        counted.append(_replace_parts(message, parts))
    return counted


def _counted_prompt(part: UserPromptPart) -> UserPromptPart:
    content: list[Any] = []
    for item in part.content:  # type: ignore[union-attr]
        if isinstance(item, TextContent):
            content.append(TextContent(item.content))
            continue
        if isinstance(item, BinaryContent):
            if item.media_type in TEXT_CONTENT_MEDIA_TYPES:
                content.append(TextContent(item.data.decode("utf-8", "replace")))
            # Native media has no application-side token model; omitting it is
            # honest, whereas guessing would fabricate a number.
            continue
        content.append(item)
    return _replace_content(part, content)


def _replace_content(part: UserPromptPart, content: list[Any]) -> UserPromptPart:
    return replace(part, content=content)


def _replace_parts(message: ModelRequest, parts: list[Any]) -> ModelRequest:
    return replace(message, parts=parts)


def _instruction_bytes(parts: Iterable[Any] | None) -> int:
    total = 0
    for part in parts or ():
        content = getattr(part, "content", "")
        if isinstance(content, str):
            total += len(content.encode("utf-8"))
    return total


def _tool_bytes(definitions: Iterable[Any] | None) -> int:
    total = 0
    for tool in definitions or ():
        total += len(str(getattr(tool, "name", "")))
        total += len(str(getattr(tool, "description", "") or ""))
        try:
            schema = json.dumps(
                getattr(tool, "parameters_json_schema", None),
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            schema = ""
        total += len(schema.encode("utf-8"))
    return total


def make_budget_hooks(*, context_window: int, max_output_tokens: int) -> Hooks[None]:
    """Build the per-run input guard.

    The hooks are eager policy, not decoration: they run on every model round,
    including a round whose input was enlarged by tool results.
    """

    def allowance_for(model: Any) -> int:
        return input_allowance(
            model,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
        )

    def before_model_request(
        ctx: RunContext[None],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        allowance = allowance_for(request_context.model)
        if allowance <= 0:
            raise ContextBudgetExceeded
        if estimate_text_input_tokens(request_context) > allowance:
            raise ContextBudgetExceeded
        return request_context

    def after_model_request(
        ctx: RunContext[None],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        allowance = allowance_for(request_context.model)
        if allowance <= 0:
            raise ContextBudgetExceeded
        usage = response.usage
        if getattr(usage, "unmeasured_requests", 0) == 0:
            try:
                UsageLimits(
                    per_request_input_tokens_limit=allowance
                ).check_per_request_input_tokens(usage.input_tokens)
            except UsageLimitExceeded:
                raise ContextBudgetExceeded from None
        return response

    return Hooks(
        before_model_request=before_model_request,
        after_model_request=after_model_request,
    )
