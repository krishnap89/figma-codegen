"""ANSI colors for terminal output.

On only when the stream is a terminal. NO_COLOR (https://no-color.org) turns it off,
FORCE_COLOR turns it on (e.g. CI logs that render ANSI), TERM=dumb turns it off.
"""
from __future__ import annotations

import os
from typing import IO

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}


def use_color(stream: IO[str] | None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError):
        return False


class Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    @classmethod
    def for_stream(cls, stream: IO[str] | None) -> "Style":
        return cls(use_color(stream))

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles or not text:
            return text
        return f"\x1b[{';'.join(_CODES[s] for s in styles)}m{text}\x1b[0m"

    # shorthands
    def ok(self, text: str) -> str:
        return self(text, "green")

    def err(self, text: str) -> str:
        return self(text, "red")

    def warn(self, text: str) -> str:
        return self(text, "yellow")

    def dim(self, text: str) -> str:
        return self(text, "dim")

    def mark(self, ok: bool) -> str:
        return self.ok("✓") if ok else self.err("✗")
