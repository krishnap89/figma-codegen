"""Small helpers to turn Figma layer names into code identifiers."""
from __future__ import annotations

import re

_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def words(s: str) -> list[str]:
    return [w for w in _SPLIT.split(s or "") if w]


def pascal(s: str, fallback: str = "Screen") -> str:
    out = "".join(w[:1].upper() + w[1:] for w in words(s))
    if not out:
        return fallback
    if out[0].isdigit():
        out = "_" + out
    return out


def camel(s: str, fallback: str = "value") -> str:
    p = pascal(s, fallback="")
    if not p:
        return fallback
    return p[:1].lower() + p[1:]


def strip_suffix(name: str, suffixes: list[str]) -> str:
    for suf in suffixes:
        if name.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return name
