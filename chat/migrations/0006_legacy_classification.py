"""Classify legacy conversations that cannot prove they were tool-free.

History written before this release may contain warehouse tool calls whose
access can no longer be verified. Nothing is rewritten here: only
``quarantined`` changes, so conversation bytes, checkpoints, attachments,
thread ids, owners, revisions and leases stay exactly as stored. Missing legacy
``ChatTurn`` rows or NULL checkpoints are not by themselves evidence of
analytics; a complete, structurally valid tool-free transcript stays readable.
"""

from __future__ import annotations

import json

from django.db import migrations

ALLOWED_REQUEST_PARTS = frozenset({"system-prompt", "user-prompt"})
ALLOWED_RESPONSE_PARTS = frozenset({"text", "thinking"})
ACTIVITY_KEYS = frozenset({"version", "turn_id", "steps", "truncated"})
STEP_KEYS = frozenset({"id", "kind", "status", "title", "duration_ms"})
REQUIRED_STEP_KEYS = frozenset({"id", "kind", "status"})
TERMINAL_STATUSES = frozenset({"complete", "failed"})
MAX_ACTIVITY_STEPS = 64


def _prompt_text(content):
    """Return one user prompt's text, or ``None`` when the shape is unknown."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or len(content) != 1:
        return None
    item = content[0]
    if not isinstance(item, dict):
        return None
    text = item.get("content")
    if not isinstance(text, str):
        return None
    if "metadata" in item and not isinstance(item["metadata"], dict):
        return None
    return text


def _parse_history(raw):
    """Return ``(prompts, texts)`` for a clean transcript, or ``None``."""
    if not isinstance(raw, str):
        return None
    try:
        messages = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(messages, list):
        return None

    prompts = []
    texts = []
    for message in messages:
        if not isinstance(message, dict):
            return None
        kind = message.get("kind")
        parts = message.get("parts")
        if kind not in {"request", "response"} or not isinstance(parts, list):
            return None
        allowed = ALLOWED_REQUEST_PARTS if kind == "request" else ALLOWED_RESPONSE_PARTS
        prompt_count = 0
        text_count = 0
        for part in parts:
            if not isinstance(part, dict):
                return None
            part_kind = part.get("part_kind")
            if part_kind not in allowed:
                return None
            content = part.get("content")
            if part_kind == "user-prompt":
                text = _prompt_text(content)
                if text is None:
                    return None
                prompts.append(text)
                prompt_count += 1
                continue
            if not isinstance(content, str):
                return None
            if part_kind == "text":
                texts.append(content)
                text_count += 1
        if prompt_count > 1 or text_count > 1:
            return None
    return prompts, texts


def _display_pairs(display):
    if not isinstance(display, list) or len(display) % 2 != 0:
        return None
    for index, entry in enumerate(display):
        if not isinstance(entry, dict) or set(entry) != {"role", "content"}:
            return None
        expected = "user" if index % 2 == 0 else "assistant"
        if entry["role"] != expected or not isinstance(entry["content"], str):
            return None
    return display


def _activity_is_clean(value):
    """A stored sidecar is safe only when it has no warehouse tool step."""
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != ACTIVITY_KEYS:
        return False
    if value["version"] != 1:
        return False
    if not isinstance(value["turn_id"], str) or not isinstance(
        value["truncated"], bool
    ):
        return False
    steps = value["steps"]
    if not isinstance(steps, list) or len(steps) > MAX_ACTIVITY_STEPS:
        return False
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            return False
        keys = set(step)
        if not REQUIRED_STEP_KEYS.issubset(keys) or not keys.issubset(STEP_KEYS):
            return False
        if step["id"] != index:
            return False
        if step["kind"] != "model":
            return False
        if step["status"] not in TERMINAL_STATUSES:
            return False
        if "title" in step and not isinstance(step["title"], str):
            return False
        duration = step.get("duration_ms")
        if duration is not None:
            if isinstance(duration, bool) or not isinstance(duration, int):
                return False
            if duration < 0:
                return False
    return True


def _thread_is_safe(thread, turns):
    display = _display_pairs(thread.display)
    if display is None:
        return False
    history = _parse_history(thread.history_json)
    if history is None:
        return False
    prompts, texts = history
    pairs = len(display) // 2
    if len(prompts) != pairs or len(texts) != pairs:
        return False
    for index in range(pairs):
        if prompts[index] != display[2 * index]["content"]:
            return False
        if texts[index] != display[2 * index + 1]["content"]:
            return False

    for turn in turns:
        checkpoint = turn["pre_history_json"]
        if checkpoint is not None:
            snapshot = _parse_history(checkpoint)
            if snapshot is None:
                return False
            if len(snapshot[0]) != len(snapshot[1]):
                return False
        if not _activity_is_clean(turn["activity"]):
            return False
    return True


def classify_legacy_threads(apps, schema_editor):
    ChatThread = apps.get_model("chat", "ChatThread")
    ChatTurn = apps.get_model("chat", "ChatTurn")

    flagged = []
    for thread in ChatThread.objects.all().iterator():
        turns = list(
            ChatTurn.objects.filter(thread_id=thread.pk).values(
                "pre_history_json", "activity"
            )
        )
        if not _thread_is_safe(thread, turns):
            flagged.append(thread.pk)
    if flagged:
        ChatThread.objects.filter(pk__in=flagged).update(quarantined=True)


def unlock_nothing(apps, schema_editor):
    """Never unlock automatically: reclassification is a deliberate review."""


class Migration(migrations.Migration):
    dependencies = [
        ("chat", "0005_alter_chatthread_options_chatthread_department_and_more"),
        ("accounts", "0002_seed_roles"),
    ]

    operations = [
        migrations.RunPython(classify_legacy_threads, unlock_nothing),
    ]
