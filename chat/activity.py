"""Bounded, sanitized execution-activity timelines for completed chat turns.

The public schema is deliberately tiny: one app-generated turn id, at most
``MAX_ACTIVITY_STEPS`` ordered steps, and a sticky truncation flag. Provider
text, reasoning parts, tool names, arguments, results and provider ids never
enter it, so a sidecar can be shown to the visitor as-is.

Activity is auxiliary. An unusable sidecar is dropped rather than making an
otherwise valid canonical conversation unreadable, and stored entries are
rebuilt field by field here so unknown keys are never re-exposed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, Literal, TypedDict
from uuid import UUID

ACTIVITY_VERSION: Final = 1
MAX_ACTIVITY_STEPS: Final = 64
MAX_ACTIVITY_BYTES: Final = 65536
MAX_TITLE_LENGTH: Final = 500

ActivityKind = Literal["model", "wren_tool"]
ActivityStatus = Literal["running", "complete", "failed"]

KINDS: Final = frozenset({"model", "wren_tool"})
STATUSES: Final = frozenset({"running", "complete", "failed"})

_ACTIVITY_KEYS: Final = frozenset({"version", "turn_id", "steps", "truncated"})
_REQUIRED_STEP_KEYS: Final = frozenset({"id", "kind", "status"})
_ALLOWED_STEP_KEYS: Final = frozenset({"id", "kind", "status", "title", "duration_ms"})

# Terminal statuses are compared for ordering only; a running step may become
# either of them, and a terminal step never changes again.
_TERMINAL: Final = frozenset({"complete", "failed"})


class ActivityStep(TypedDict, total=False):
    """One ordered unit of work in a turn, identified by its position."""

    id: int
    kind: str
    status: str
    title: str
    duration_ms: int


class TurnActivity(TypedDict):
    """The public activity timeline for exactly one completed turn."""

    version: int
    turn_id: str
    steps: list[ActivityStep]
    truncated: bool


@dataclass(frozen=True)
class ActivityUpdate:
    """One streamed change to the running turn's activity timeline.

    ``step`` is ``None`` only for the single truncation notice, which is sent
    once when work was omitted. Every serialized field is public by design.
    """

    turn_id: str
    step: ActivityStep | None
    truncated: bool


def _valid_step(value: Any, expected_id: int) -> ActivityStep | None:
    """Rebuild one step, requiring its id to match its position."""
    if not isinstance(value, dict):
        return None
    keys = set(value.keys())
    if not _REQUIRED_STEP_KEYS.issubset(keys) or not keys.issubset(_ALLOWED_STEP_KEYS):
        return None

    step_id = value["id"]
    if isinstance(step_id, bool) or not isinstance(step_id, int):
        return None
    if step_id != expected_id or not 0 <= step_id < MAX_ACTIVITY_STEPS:
        return None

    kind = value["kind"]
    status = value["status"]
    if not isinstance(kind, str) or kind not in KINDS:
        return None
    if not isinstance(status, str) or status not in STATUSES:
        return None

    step: ActivityStep = {"id": step_id, "kind": kind, "status": status}
    if "title" in value and value["title"] is not None:
        raw_title = value["title"]
        if not isinstance(raw_title, str):
            return None
        cleaned_title = raw_title.strip()
        if cleaned_title:
            step["title"] = cleaned_title[:MAX_TITLE_LENGTH]

    if "duration_ms" in value and value["duration_ms"] is not None:
        duration_ms = value["duration_ms"]
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or duration_ms < 0
        ):
            return None
        step["duration_ms"] = duration_ms

    return step


def strip_activity_detail(activity: TurnActivity) -> TurnActivity:
    """Strip optional detail (title, duration_ms) returning only core enum keys."""
    steps: list[ActivityStep] = [
        {"id": step["id"], "kind": step["kind"], "status": step["status"]}
        for step in activity["steps"]
    ]
    return {
        "version": activity["version"],
        "turn_id": activity["turn_id"],
        "steps": steps,
        "truncated": activity["truncated"],
    }


def _envelope_bytes(turn_id: str, steps: list[ActivityStep], truncated: bool) -> int:
    """Measure one serialized envelope the way the public contract encodes it."""
    payload = {
        "version": ACTIVITY_VERSION,
        "turn_id": turn_id,
        "steps": steps,
        "truncated": truncated,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return len(encoded.encode("utf-8"))


def valid_activity(value: Any, *, completed: bool) -> TurnActivity | None:
    """Validate and rebuild one sidecar, or return ``None`` when unusable.

    ``completed`` rejects a persisted or terminal snapshot that still claims
    running work. Failed steps stay valid because a turn can recover from one.
    """
    if not isinstance(value, dict) or set(value) != _ACTIVITY_KEYS:
        return None

    version = value["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    if version != ACTIVITY_VERSION:
        return None

    turn_id = value["turn_id"]
    if not isinstance(turn_id, str):
        return None
    try:
        normalized_id = str(UUID(turn_id))
    except (AttributeError, TypeError, ValueError):
        return None

    raw_steps = value["steps"]
    if not isinstance(raw_steps, list) or len(raw_steps) > MAX_ACTIVITY_STEPS:
        return None

    steps: list[ActivityStep] = []
    for expected_id, entry in enumerate(raw_steps):
        step = _valid_step(entry, expected_id)
        if step is None:
            return None
        if completed and step["status"] == "running":
            return None
        steps.append(step)

    truncated = value["truncated"]
    if not isinstance(truncated, bool):
        return None

    if _envelope_bytes(normalized_id, steps, truncated) > MAX_ACTIVITY_BYTES:
        return None

    return {
        "version": ACTIVITY_VERSION,
        "turn_id": normalized_id,
        "steps": steps,
        "truncated": truncated,
    }


class ActivityCollector:
    """Ordered activity for one in-flight turn, bounded by count and bytes.

    Steps are allocated only when an observed event actually happens. Once the
    cap is reached every further admission is refused and exactly one truncation
    notice is emitted; already-admitted running steps can still finish, because
    admission reserves the space a terminal status needs.
    """

    def __init__(self, turn_id: UUID | str) -> None:
        self.turn_id = str(turn_id)
        self._steps: list[ActivityStep] = []
        self._truncated = False

    def _fits(self, steps: list[ActivityStep]) -> bool:
        """Measure with the longest terminal status so finishing cannot overflow."""
        worst: list[ActivityStep] = []
        for s in steps:
            item: ActivityStep = {
                "id": s["id"],
                "kind": s["kind"],
                "status": "complete",
            }
            if "title" in s and s["title"] is not None:
                item["title"] = s["title"]
            if "duration_ms" in s and s["duration_ms"] is not None:
                item["duration_ms"] = s["duration_ms"]
            worst.append(item)
        return _envelope_bytes(self.turn_id, worst, True) <= MAX_ACTIVITY_BYTES

    def _truncation_notice(self) -> ActivityUpdate | None:
        if self._truncated:
            return None
        self._truncated = True
        if not self._steps:
            return None
        return ActivityUpdate(turn_id=self.turn_id, step=None, truncated=True)

    def start(
        self,
        kind: ActivityKind,
        *,
        status: ActivityStatus = "running",
        title: str | None = None,
        duration_ms: int | None = None,
    ) -> ActivityUpdate | None:
        """Admit one new step, or emit the single truncation notice instead."""
        if kind not in KINDS or status not in STATUSES:
            return None
        if self._truncated or len(self._steps) >= MAX_ACTIVITY_STEPS:
            return self._truncation_notice()

        step: ActivityStep = {
            "id": len(self._steps),
            "kind": kind,
            "status": status,
        }
        if title:
            cleaned = title.strip()[:MAX_TITLE_LENGTH]
            if cleaned:
                step["title"] = cleaned
        if (
            duration_ms is not None
            and not isinstance(duration_ms, bool)
            and duration_ms >= 0
        ):
            step["duration_ms"] = duration_ms

        candidate = [*self._steps, step]
        if not self._fits(candidate):
            return self._truncation_notice()

        self._steps = candidate
        return ActivityUpdate(turn_id=self.turn_id, step=step, truncated=False)

    def finish(
        self,
        step_id: int,
        status: ActivityStatus,
        *,
        title: str | None = None,
        duration_ms: int | None = None,
    ) -> ActivityUpdate | None:
        """Move one admitted running step to a terminal status, once."""
        if status not in _TERMINAL:
            return None
        if isinstance(step_id, bool) or not isinstance(step_id, int):
            return None
        if not 0 <= step_id < len(self._steps):
            return None

        current = self._steps[step_id]
        if current["status"] != "running":
            return None

        step: ActivityStep = {
            "id": step_id,
            "kind": current["kind"],
            "status": status,
        }
        step_title = title if title is not None else current.get("title")
        if step_title:
            cleaned = step_title.strip()[:MAX_TITLE_LENGTH]
            if cleaned:
                step["title"] = cleaned

        step_duration = (
            duration_ms if duration_ms is not None else current.get("duration_ms")
        )
        if (
            step_duration is not None
            and not isinstance(step_duration, bool)
            and step_duration >= 0
        ):
            step["duration_ms"] = step_duration

        self._steps = [*self._steps[:step_id], step, *self._steps[step_id + 1 :]]
        return ActivityUpdate(
            turn_id=self.turn_id, step=step, truncated=self._truncated
        )

    def snapshot(self) -> TurnActivity | None:
        """Return the finished timeline, or ``None`` when nothing is publishable."""
        if not self._steps and not self._truncated:
            return None
        return valid_activity(
            {
                "version": ACTIVITY_VERSION,
                "turn_id": self.turn_id,
                "steps": self._steps,
                "truncated": self._truncated,
            },
            completed=True,
        )
