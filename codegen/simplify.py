"""Deterministic Figma node tree -> compact, platform-neutral screen spec.

No LLM involved. Anything that can't be mapped cleanly is reported in `warnings`
instead of being guessed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .design_system import DesignSystem, figma_color_to_hex
from .naming import camel, pascal, strip_suffix

CONTAINER_TYPES = {"FRAME", "COMPONENT", "GROUP", "SECTION"}
ALIGN = {"MIN": "start", "CENTER": "center", "MAX": "end", "SPACE_BETWEEN": "spaceBetween", "BASELINE": "baseline"}
INPUT_SUFFIXES = ["TextField", "Field", "Input"]
ACTION_SUFFIXES = ["Button", "Link", "Action"]


@dataclass
class SimplifyResult:
    spec: dict
    used_components: set[str] = field(default_factory=set)
    used_tokens: set[str] = field(default_factory=set)

    @property
    def warnings(self) -> list[str]:
        return self.spec.get("warnings", [])


class Simplifier:
    def __init__(self, ds: DesignSystem, node_entry: dict):
        self.ds = ds
        self.components = node_entry.get("components", {}) or {}
        self.component_sets = node_entry.get("componentSets", {}) or {}
        self.styles = node_entry.get("styles", {}) or {}
        self.document = node_entry["document"]
        self.warnings: list[str] = []
        self.used_components: set[str] = set()
        self.used_tokens: set[str] = set()
        self._name_counts: dict[str, int] = {}

    # ---------- entry ----------
    def run(self) -> SimplifyResult:
        doc = self.document
        root = self._node(doc, parent_mode=None, is_root=True)
        spec = {
            "screen": pascal(doc.get("name", "Screen")),
            "figmaNode": doc.get("id"),
            "root": root,
            "warnings": self.warnings,
        }
        return SimplifyResult(spec, self.used_components, self.used_tokens)

    # ---------- helpers ----------
    def _warn(self, node: dict, msg: str) -> None:
        self.warnings.append(f"[{node.get('name', '?')} {node.get('id', '')}] {msg}")

    def _tok(self, token: str | None) -> str | None:
        if token:
            self.used_tokens.add(token)
        return token

    def _dim(self, value: float, category: str, node: dict, what: str) -> str:
        token = self.ds.dimension_token(value, category)
        if token:
            return self._tok(token)
        self._warn(node, f"{what}={value:g}px matches no {category} token")
        return f"raw:{value:g}px"

    def _fill_color(self, node: dict, what: str = "fill") -> str | None:
        fills = [f for f in node.get("fills", []) or [] if f.get("visible", True) and f.get("type") == "SOLID"]
        if not fills:
            if any(f.get("visible", True) for f in node.get("fills", []) or []):
                self._warn(node, f"non-solid {what} (gradient/image) ignored")
            return None
        f = fills[-1]
        hex_value = figma_color_to_hex(f.get("color", {}), f.get("opacity", 1.0))
        token = self.ds.color_token(hex_value)
        if token:
            return self._tok(token)
        self._warn(node, f"{what} color {hex_value} matches no color token")
        return f"raw:{hex_value}"

    def _sizing(self, node: dict, out: dict) -> None:
        h = node.get("layoutSizingHorizontal")
        v = node.get("layoutSizingVertical")
        if h == "FILL" and v == "FILL":
            out["fill"] = "maxSize"
        elif h == "FILL":
            out["fill"] = "maxWidth"
        elif v == "FILL":
            out["fill"] = "maxHeight"
        if node.get("layoutGrow") == 1:
            out["weight"] = 1

    def _padding(self, node: dict) -> dict | None:
        vals = {k: node.get(f"padding{k.capitalize()}", 0) or 0 for k in ("left", "right", "top", "bottom")}
        if not any(vals.values()):
            return None
        tok = {k: (self._dim(v, "spacing", node, f"padding{k.capitalize()}") if v else None) for k, v in vals.items()}
        if len(set(vals.values())) == 1:
            return {"all": tok["left"]}
        if vals["left"] == vals["right"] and vals["top"] == vals["bottom"]:
            return {k: t for k, t in (("horizontal", tok["left"]), ("vertical", tok["top"])) if t}
        return {k: t for k, t in tok.items() if t}

    def _unique_name(self, base: str) -> str:
        n = self._name_counts.get(base, 0) + 1
        self._name_counts[base] = n
        return base if n == 1 else f"{base}{n}"

    @staticmethod
    def _find_text(node: dict, layer_name: str) -> str | None:
        stack = list(node.get("children", []) or [])
        while stack:
            c = stack.pop(0)
            if c.get("visible", True) is False:
                continue
            if c.get("type") == "TEXT" and c.get("name") == layer_name:
                return c.get("characters", "")
            stack.extend(c.get("children", []) or [])
        return None

    # ---------- node dispatch ----------
    def _node(self, n: dict, parent_mode: str | None, is_root: bool = False) -> dict | None:
        if n.get("visible", True) is False:
            return None
        t = n.get("type")
        if t == "INSTANCE":
            return self._instance(n)
        if t == "TEXT":
            return self._text(n)
        if t in CONTAINER_TYPES:
            return self._container(n, parent_mode, is_root)
        self._warn(n, f"{t} node skipped; use a UIKit component (e.g. Icon, Divider, Image) instead")
        return None

    def _container(self, n: dict, parent_mode: str | None, is_root: bool) -> dict:
        children = [c for c in n.get("children", []) or [] if c.get("visible", True) is not False]
        mode = n.get("layoutMode", "NONE") if n.get("type") != "GROUP" else "NONE"

        # Empty, unpainted frame inside auto-layout = spacer.
        if not children and not is_root and not self._has_paint(n) and parent_mode in ("VERTICAL", "HORIZONTAL"):
            if n.get("layoutGrow") == 1:
                return {"type": "Spacer", "weight": 1}
            box = n.get("absoluteBoundingBox") or {}
            size = box.get("height" if parent_mode == "VERTICAL" else "width", 0)
            return {"type": "Spacer", "size": self._dim(size, "spacing", n, "spacer")}

        out: dict = {"type": {"VERTICAL": "Column", "HORIZONTAL": "Row"}.get(mode, "Box"), "name": n.get("name")}
        if mode == "NONE" and len(children) > 1:
            self._warn(n, "no auto-layout; children positions are absolute and will be stacked. Add auto-layout in Figma.")

        bg = self._fill_color(n, "background")
        if bg:
            out["background"] = bg
        radius = n.get("cornerRadius")
        if radius:
            out["cornerRadius"] = self._dim(radius, "radius", n, "cornerRadius")
        if mode != "NONE":
            pad = self._padding(n)
            if pad:
                out["padding"] = pad
            gap = n.get("itemSpacing") or 0
            if gap and n.get("primaryAxisAlignItems") != "SPACE_BETWEEN":
                out["gap"] = self._dim(gap, "spacing", n, "itemSpacing")
            if n.get("primaryAxisAlignItems") not in (None, "MIN"):
                out["arrangement"] = ALIGN.get(n["primaryAxisAlignItems"], "start")
            if n.get("counterAxisAlignItems") not in (None, "MIN"):
                out["align"] = ALIGN.get(n["counterAxisAlignItems"], "start")
        if is_root:
            out["fill"] = "maxSize"
        else:
            self._sizing(n, out)

        kids = [k for k in (self._node(c, mode) for c in children) if k]
        if kids:
            out["children"] = kids
        return out

    @staticmethod
    def _has_paint(n: dict) -> bool:
        paints = (n.get("fills") or []) + (n.get("strokes") or [])
        return any(p.get("visible", True) for p in paints)

    def _text(self, n: dict) -> dict:
        style = n.get("style", {}) or {}
        style_id = (n.get("styles") or {}).get("text")
        style_name = self.styles.get(style_id, {}).get("name") if style_id else None
        typo = self.ds.typography_token(style_name, style.get("fontSize"), style.get("fontWeight"))
        out: dict = {"type": "Text", "name": n.get("name"), "text": n.get("characters", "")}
        if typo:
            out["style"] = self._tok(typo)
        else:
            self._warn(n, f"text style (style={style_name}, size={style.get('fontSize')}, weight={style.get('fontWeight')}) matches no typography token")
            out["style"] = f"raw:{style.get('fontSize')}px/{style.get('fontWeight')}"
        color = self._fill_color(n, "text")
        if color:
            out["color"] = color
        self._sizing(n, out)
        return out

    def _instance(self, n: dict) -> dict:
        meta = self.components.get(n.get("componentId"), {}) or {}
        set_meta = self.component_sets.get(meta.get("componentSetId"), {}) or {}
        entry = self.ds.match_component(
            keys=[set_meta.get("key"), meta.get("key")],
            names=[set_meta.get("name"), meta.get("name"), n.get("name")],
        )
        figma_name = set_meta.get("name") or meta.get("name") or n.get("name")
        if entry is None:
            self._warn(n, f"instance of '{figma_name}' is not in the UIKit manifest")
            return {"type": "Unmapped", "name": n.get("name"), "figmaComponent": figma_name}

        self.used_components.add(entry["figma"])
        raw = {k.split("#")[0]: v.get("value") for k, v in (n.get("componentProperties") or {}).items()}
        props: dict = {}
        for prop, spec in (entry.get("props") or {}).items():
            if "figmaProp" in spec:
                val = raw.get(spec["figmaProp"])
                if val is None:
                    continue
                if "map" in spec:
                    if val not in spec["map"]:
                        self._warn(n, f"{entry['figma']}.{spec['figmaProp']}='{val}' has no mapping")
                        continue
                    val = spec["map"][val]
                elif "values" in spec and val not in spec["values"]:
                    self._warn(n, f"{entry['figma']}.{spec['figmaProp']}='{val}' not in {spec['values']}")
                props[prop] = val
            elif "figmaText" in spec:
                text = self._find_text(n, spec["figmaText"])
                if text is not None:
                    props[prop] = text

        out: dict = {"component": entry["figma"], "name": n.get("name")}
        if props:
            out["props"] = props

        layer = n.get("name", "")
        is_default_name = layer.strip().lower() in {(figma_name or "").lower(), entry["figma"].lower()}
        kind = entry.get("kind")
        if kind == "input":
            base = entry["figma"] if is_default_name else strip_suffix(pascal(layer), INPUT_SUFFIXES)
            if is_default_name:
                self._warn(n, "layer has default name; rename it (e.g. 'EmailField') for a meaningful state property")
            out["binding"] = camel(self._unique_name(pascal(base)))
        elif kind == "action":
            base = entry["figma"] if is_default_name else strip_suffix(pascal(layer), ACTION_SUFFIXES)
            if is_default_name:
                self._warn(n, "layer has default name; rename it (e.g. 'LoginButton') for a meaningful callback")
            out["event"] = "on" + self._unique_name(pascal(base))
        self._sizing(n, out)
        return out


def simplify(ds: DesignSystem, node_entry: dict) -> SimplifyResult:
    return Simplifier(ds, node_entry).run()


def collect(spec_root: dict, key: str) -> list[str]:
    """All values of `binding` / `event` in tree order."""
    found: list[str] = []

    def walk(node: dict) -> None:
        if key in node:
            found.append(node[key])
        for c in node.get("children", []) or []:
            walk(c)

    walk(spec_root)
    return found
