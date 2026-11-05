"""Loads the component manifest and design tokens; answers mapping questions."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .naming import camel

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def normalize_name(name: str) -> str:
    """'Carbon / Darkest' -> 'carbondarkest'; 'carbon.darkest' -> 'carbondarkest'."""
    return _NON_ALNUM.sub("", (name or "").lower())


def normalize_hex(value: str) -> str:
    v = value.strip().upper()
    if not v.startswith("#"):
        v = "#" + v
    if len(v) == 9 and v.endswith("FF"):
        v = v[:7]
    return v


def figma_color_to_hex(color: dict, opacity: float = 1.0) -> str:
    r, g, b = (round(color.get(k, 0) * 255) for k in ("r", "g", "b"))
    a = color.get("a", 1.0) * opacity
    h = f"#{r:02X}{g:02X}{b:02X}"
    if a < 0.999:
        h += f"{round(a * 255):02X}"
    return h


def parse_px(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        v = value.strip().lower().removesuffix("px")
        try:
            return float(v)
        except ValueError:
            return None
    return None


class DesignSystem:
    def __init__(self, manifest: dict, tokens: dict):
        self.manifest = manifest
        self.components: list[dict] = manifest.get("components", [])
        self.tokens: dict[str, dict] = {}
        self._flatten(tokens, [])
        self._colors: dict[str, str] = {}
        self._dims: dict[str, dict[float, str]] = {}
        for path, t in self.tokens.items():
            cat = path.split(".", 1)[0]
            if t["type"] == "color" and isinstance(t["value"], str):
                self._colors.setdefault(normalize_hex(t["value"]), path)
            elif t["type"] == "dimension":
                px = parse_px(t["value"])
                if px is not None:
                    self._dims.setdefault(cat, {}).setdefault(px, path)

    @classmethod
    def load(cls, manifest_path: str, tokens_path: str) -> "DesignSystem":
        return cls(json.loads(Path(manifest_path).read_text()), json.loads(Path(tokens_path).read_text()))

    def _flatten(self, node: dict, prefix: list[str]) -> None:
        for k, v in node.items():
            if k.startswith("$") or not isinstance(v, dict):
                continue
            if "$value" in v:
                self.tokens[".".join(prefix + [k])] = {"type": v.get("$type"), "value": v["$value"]}
            else:
                self._flatten(v, prefix + [k])

    # ---------- token lookup ----------
    def color_token(self, hex_value: str) -> str | None:
        return self._colors.get(normalize_hex(hex_value))

    def dimension_token(self, px: float, category: str) -> str | None:
        for value, path in self._dims.get(category, {}).items():
            if abs(value - px) < 0.01:
                return path
        return None

    def token_by_name(self, category: str, style_name: str | None) -> str | None:
        """Match a Figma style name to a token path in `category`, ignoring case and punctuation:
        'Carbon / Darkest' -> 'color.carbon.darkest', 'Heading/L' -> 'typography.heading/l'."""
        wanted = normalize_name(style_name or "")
        if not wanted:
            return None
        for path in self.tokens:
            cat, _, rest = path.partition(".")
            if cat == category and normalize_name(rest) == wanted:
                return path
        return None

    def has_category(self, category: str) -> bool:
        return any(p.partition(".")[0] == category for p in self.tokens)

    def typography_token(self, style_name: str | None, font_size: float | None, font_weight: float | None) -> str | None:
        typo = {p: t for p, t in self.tokens.items() if p.startswith("typography.")}
        by_name = self.token_by_name("typography", style_name)
        if by_name:
            return by_name
        if font_size is not None:
            for path, t in typo.items():
                v = t["value"] if isinstance(t["value"], dict) else {}
                size = parse_px(v.get("fontSize"))
                weight = v.get("fontWeight")
                if size is not None and abs(size - font_size) < 0.01 and (
                    font_weight is None or weight is None or int(weight) == int(font_weight)
                ):
                    return path
        return None

    def tokens_in(self, category: str) -> list[str]:
        return [p for p in self.tokens if p.split(".", 1)[0] == category]

    def token_code(self, path: str, platform: str) -> str:
        """'spacing.m' -> 'AppTheme.spacing.m'; 'typography.heading/l' -> 'AppTheme.typography.headingL'."""
        cat, _, rest = path.partition(".")
        cat_code = self.manifest.get("categoryCode", {}).get(cat, cat)
        template = self.manifest.get("tokenCode", {}).get(platform, "{category}.{name}")
        return template.format(category=cat_code, name=camel(rest))

    # ---------- components ----------
    def match_component(self, keys: list[str | None], names: list[str | None]) -> dict | None:
        keys = [k for k in keys if k]
        for c in self.components:
            if c.get("figmaKey") and c["figmaKey"] in keys:
                return c
        lowered = [n.strip().lower() for n in names if n]
        for c in self.components:
            if c.get("figma", "").lower() in lowered:
                return c
        return None

    def component(self, figma_name: str) -> dict | None:
        for c in self.components:
            if c.get("figma", "").lower() == figma_name.strip().lower():
                return c
        return None

    def component_doc(self, figma_name: str, platform: str | None = None) -> dict | None:
        c = self.component(figma_name)
        if not c:
            return None
        doc = {k: c[k] for k in ("figma", "description", "props") if k in c}
        for p in ("compose", "swiftui"):
            if p in c and (platform is None or platform == p):
                doc[p] = c[p]
        return doc

    def primitives(self, platform: str) -> dict:
        return self.manifest.get("primitives", {}).get(platform, {})
