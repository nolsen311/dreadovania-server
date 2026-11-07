# SPDX-License-Identifier: GPL-3.0-or-later
"""Validation of user input before it reaches Randovania (ADR 0011 §3)."""

from __future__ import annotations

import re

# Randovania encodes permalinks as base64 with the "-_" alphabet; padding is optional.
PERMALINK_RE = re.compile(r"[A-Za-z0-9_-]{4,4096}={0,2}")
# Randovania accepts seed numbers in [0, 2**31).
MAX_SEED_NUMBER = 2**31


class InvalidInput(ValueError):
    """The message is safe to show to the user."""


def check_permalink(value: str) -> str:
    value = value.strip()
    if not PERMALINK_RE.fullmatch(value):
        raise InvalidInput("that doesn't look like a Randovania permalink")
    return value


def check_seed_number(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInput("the seed number must be a whole number")
    if not 0 <= value < MAX_SEED_NUMBER:
        raise InvalidInput(f"the seed number must be between 0 and {MAX_SEED_NUMBER - 1}")
    return value
