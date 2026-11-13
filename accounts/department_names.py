"""Canonical display names and uniqueness keys for departments."""

from __future__ import annotations

import unicodedata


def normalize_department_name(name: str) -> tuple[str, str]:
    display = " ".join(unicodedata.normalize("NFKC", name).split())
    folded = unicodedata.normalize("NFKC", display).casefold()
    parts: list[str] = []
    separator = False
    for character in folded:
        if character.isalnum():
            if separator and parts:
                parts.append("-")
            parts.append(character)
            separator = False
        else:
            separator = True
    return display, "".join(parts)
