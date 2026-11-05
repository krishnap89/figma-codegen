"""Evaluate Kotlin token expressions (colors, Dp, TextStyle, shapes) to plain values."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..kotlin_source import split_args
from .source import Module

NAMED_COLORS = {"White": "FFFFFF", "Black": "000000", "Red": "FF0000", "Green": "00FF00", "Blue": "0000FF",
                "Yellow": "FFFF00", "Cyan": "00FFFF", "Magenta": "FF00FF", "Gray": "888888",
                "LightGray": "CCCCCC", "DarkGray": "444444", "Transparent": "00000000"}
FONT_WEIGHTS = {"Thin": 100, "ExtraLight": 200, "Light": 300, "Normal": 400, "Medium": 500, "SemiBold": 600,
                "Bold": 700, "ExtraBold": 800, "Black": 900}


@dataclass
class Value:
    type: str               # color | dimension | typography | shape
    value: object


def _call(t: str) -> Optional[tuple[str, str]]:
    """'Foo.bar(a, b)' -> ('Foo.bar', 'a, b') when the whole text is one call."""
    if not t.endswith(")"):
        return None
    depth = 0
    for j in range(len(t) - 1, -1, -1):
        if t[j] == ")":
            depth += 1
        elif t[j] == "(":
            depth -= 1
            if depth == 0:
                head = t[:j].strip()
                return (head, t[j + 1:-1]) if re.fullmatch(r"[A-Za-z_][\w.]*|.+\.copy", head, re.S) else None
    return None


def call_args(text: str) -> tuple[list, dict]:
    pos, named = [], {}
    for piece, _ in split_args(text, text):
        m = re.match(r"\s*([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", piece, re.S)
        if m:
            named[m.group(1)] = m.group(2).strip()
        else:
            pos.append(piece.strip())
    return pos, named


def _num(t: str) -> Optional[float]:
    m = re.fullmatch(r"\(?\s*(-?\d+(?:\.\d+)?)[fFdL]?\s*\)?", t.strip())
    return float(m.group(1)) if m else None


def _unit(t: str, unit: str) -> Optional[float]:
    m = re.fullmatch(rf"\(?\s*(-?\d+(?:\.\d+)?)[fF]?\s*\)?\s*\.{unit}", t.strip())
    return float(m.group(1)) if m else None


def _hex(argb: int) -> str:
    a, rgb = (argb >> 24) & 0xFF, argb & 0xFFFFFF
    return f"#{rgb:06X}" + ("" if a == 0xFF else f"{a:02X}")


def px(v: float) -> str:
    return f"{v:g}px"


class Evaluator:
    def __init__(self, mod: Module):
        self.mod = mod

    def ev(self, text: str, owner: tuple = (), env: Optional[dict] = None, depth: int = 0) -> Optional[Value]:
        if depth > 12 or not text:
            return None
        t = text.strip().rstrip("!").strip()
        env = env or {}
        if t.startswith("(") and t.endswith(")") and len(t) > 1 and _call("x" + t) and _call("x" + t)[0] == "x":
            t = t[1:-1].strip()
        # literals
        dp = _unit(t, "dp")
        if dp is not None:
            return Value("dimension", dp)
        m = re.fullmatch(r"Color\.(\w+)", t)
        if m and m.group(1) in NAMED_COLORS:
            h = NAMED_COLORS[m.group(1)]
            return Value("color", "#" + h)
        m = re.fullmatch(r'"(#[0-9a-fA-F]{6,8})"\.toColorInt\(\)', t)
        if m:
            return self._color_from_css(m.group(1))
        if t in ("CircleShape", "RectangleShape"):
            return None
        call = _call(t)
        if call:
            return self._ev_call(call[0], call[1], owner, env, depth)
        # references
        if re.fullmatch(r"[A-Za-z_][\w.]*", t):
            if t in env:
                f_text, f_owner = env[t]
                return self.ev(f_text, f_owner, {}, depth + 1)
            sym = self.mod.lookup(t, owner)
            if sym:
                return self.ev(sym.text, sym.owner, {}, depth + 1)
        return None

    def _color_from_css(self, h: str) -> Value:
        h = h.lstrip("#").upper()
        return Value("color", "#" + (h[2:] + h[:2] if len(h) == 8 else h))  # #AARRGGBB -> #RRGGBBAA

    def _ev_call(self, head: str, inner: str, owner, env, depth) -> Optional[Value]:
        pos, named = call_args(inner)
        if head.endswith(".copy"):
            base = self.ev(head[:-5], owner, env, depth + 1)
            if base is None:
                return None
            if base.type == "color" and "alpha" in named:
                a = _num(named["alpha"])
                if a is None:
                    return None
                return Value("color", base.value[:7] + ("" if a >= 1 else f"{round(a * 255):02X}"))
            if base.type == "typography":
                merged = dict(base.value)
                merged.update(self._text_style({}, named, owner, env, depth))
                return Value("typography", merged)
            return None
        if head in ("mutableStateOf", "derivedStateOf", "remember") and pos:
            return self.ev(pos[0], owner, env, depth + 1)
        if head == "Color":
            return self._color(pos, named, owner, env, depth)
        if head == "Dp" and pos:
            n = _num(pos[0])
            return Value("dimension", n) if n is not None else None
        if head == "TextStyle":
            return Value("typography", self._text_style(pos, named, owner, env, depth))
        if head == "TextStyle.Default.copy":
            return Value("typography", self._text_style([], named, owner, env, depth))
        if head == "RoundedCornerShape":
            arg = (pos or [named.get("size") or named.get("corner") or ""])[0]
            v = self.ev(arg, owner, env, depth + 1)
            return Value("shape", v.value) if v and v.type == "dimension" else None
        if head.endswith("parseColor") and pos:
            m = re.fullmatch(r'"(#[0-9a-fA-F]{6,8})"', pos[0].strip())
            return self._color_from_css(m.group(1)) if m else None
        return None

    def _color(self, pos, named, owner, env, depth) -> Optional[Value]:
        if len(pos) == 1 and not named:
            a = pos[0].strip()
            m = re.fullmatch(r"0[xX]([0-9a-fA-F]{1,8})[uL]*", a)
            if m:
                return Value("color", _hex(int(m.group(1), 16)))
            if re.fullmatch(r"-?\d+", a):
                return Value("color", _hex(int(a) & 0xFFFFFFFF))
            m = re.search(r'parseColor\(\s*"(#[0-9a-fA-F]{6,8})"\s*\)', a)
            if m:
                return self._color_from_css(m.group(1))
            return None
        vals = pos + [named[k] for k in ("red", "green", "blue", "alpha") if k in named]
        nums = [_num(v) for v in vals]
        if len(nums) not in (3, 4) or any(n is None for n in nums):
            return None
        is_float = any(re.search(r"\.\d|[fF]\b|[fF]$", v) for v in vals)
        chans = [round(n * 255) if is_float else int(n) for n in nums]
        if len(chans) == 3:
            chans.append(255)
        r, g, b, a = chans
        return Value("color", f"#{r:02X}{g:02X}{b:02X}" + ("" if a == 255 else f"{a:02X}"))

    def _text_style(self, pos, named, owner, env, depth) -> dict:
        out = {}
        if "fontFamily" in named:
            family = self._font_family(named["fontFamily"], owner)
            if family:
                out["fontFamily"] = family
        if "fontWeight" in named:
            w = self._font_weight(named["fontWeight"], owner, depth)
            if w:
                out["fontWeight"] = w
        for key in ("fontSize", "lineHeight", "letterSpacing"):
            if key in named:
                v = named[key].strip()
                sp = _unit(v, "sp")
                if sp is not None:
                    out[key] = px(sp)
                elif _unit(v, "em") is not None:
                    out[key] = f"{_unit(v, 'em'):g}em"
                else:
                    sym = self.mod.lookup(v, owner) if re.fullmatch(r"[\w.]+", v) else None
                    sp = _unit(sym.text, "sp") if sym else None
                    if sp is not None:
                        out[key] = px(sp)
        return out

    def _font_weight(self, t: str, owner, depth) -> Optional[int]:
        t = t.strip()
        m = re.fullmatch(r"FontWeight\.(\w+)", t)
        if m:
            name = m.group(1)
            if re.fullmatch(r"W\d00", name):
                return int(name[1:])
            return FONT_WEIGHTS.get(name)
        m = re.fullmatch(r"FontWeight\(\s*(\d+)\s*\)", t)
        if m:
            return int(m.group(1))
        sym = self.mod.lookup(t, owner) if re.fullmatch(r"[\w.]+", t) and depth < 12 else None
        return self._font_weight(sym.text, sym.owner, depth + 1) if sym else None

    def _font_family(self, t: str, owner) -> Optional[str]:
        t = t.strip()
        m = re.fullmatch(r"FontFamily\.(\w+)", t)
        if m:
            return m.group(1)
        sym = self.mod.lookup(t, owner) if re.fullmatch(r"[\w.]+", t) else None
        src = sym.text if sym else t
        m = re.search(r"R\.font\.([a-z0-9]+)", src)
        if m:
            return m.group(1).capitalize()
        return t.split(".")[-1] if sym else None   # unresolved: a local/parameter (e.g. picked per locale)
