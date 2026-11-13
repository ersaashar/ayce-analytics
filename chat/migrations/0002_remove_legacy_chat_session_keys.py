"""Discard the two anonymous chat keys from stored sessions.

Accounts own conversations now, so a session carries login only. This removes
exactly ``chat_history_json`` and ``chat_display`` from decodable session
payloads and leaves every other key, the session key, and the expiry untouched.
It never reads or writes a chat thread and never deletes a session or user.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import migrations
from django.utils.module_loading import import_module

LEGACY_CHAT_KEYS = ("chat_history_json", "chat_display")


def _session_store() -> Any:
    """Build the configured backend so encoding matches how rows were written."""
    return import_module(settings.SESSION_ENGINE).SessionStore()


def strip_legacy_chat_session_keys(apps: Any, schema_editor: Any) -> None:
    session_model = apps.get_model("sessions", "Session")
    store = _session_store()

    for session in session_model.objects.all().iterator():
        try:
            data = store.decode(session.session_data)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if not any(key in data for key in LEGACY_CHAT_KEYS):
            continue

        for key in LEGACY_CHAT_KEYS:
            data.pop(key, None)
        session.session_data = store.encode(data)
        session.save(update_fields=["session_data"])


class Migration(migrations.Migration):
    dependencies = [
        ("sessions", "0001_initial"),
        ("chat", "0001_initial"),
    ]

    operations = [
        # Reversing would have to invent transcript content the user chose to
        # discard, so it is intentionally a no-op.
        migrations.RunPython(
            strip_legacy_chat_session_keys,
            migrations.RunPython.noop,
        ),
    ]
