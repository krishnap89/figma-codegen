"""Deterministic Figma node tree -> compact, platform-neutral screen spec.

No LLM involved. Anything that can't be mapped cleanly is reported in `warnings`
instead of being guessed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .design_system import DesignSystem, figma_color_to_hex
from .naming import camel, pascal, strip_suffix

CONTAINER_TYPES = {"FRAME", "COMPONENT", "GROUP", "SECTION"}
# Containers for the "is this only artwork?" check (R1/R2) also include instances.
GRAPHIC_CONTAINER_TYPES = CONTAINER_TYPES | {"INSTANCE"}
SHAPE_TYPES = {"VECTOR", "RECTANGLE", "ELLIPSE", "BOOLEAN_OPERATION", "LINE", "STAR", "POLYGON", "REGULAR_POLYGON"}
ALIGN = {"MIN": "start", "CENTER": "center", "MAX": "end", "SPACE_BETWEEN": "spaceBetween", "BASELINE": "baseline"}
INPUT_SUFFIXES = ["TextField", "Field", "Input"]
ACTION_SUFFIXES = ["Button", "Link", "Action"]
# A wrapper frame carrying any of these is visible on screen and is never collapsed (R3).
VISUAL_KEYS = ("background", "cornerRadius", "border", "padding")
GRAPHIC_WARNING = "hand-drawn graphic; use a UIKit Icon/Image/component, or export it as an asset"


@dataclass
class SimplifyResult:
    spec: dict
    used_components: set[str] = field(default_factory=set)
    used_tokens: set[str] = field(default_factory=set)

    @property
    def warnings(self) -> list[str]:
        return self.spec.get("warnings", [])


def _visible(n: dict) -> bool:
    return n.get("visible", True) is not False


def _visible_children(n: dict) -> list[dict]:
    return [c for c in n.get("children", []) or [] if _visible(c)]


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

    def _style_name(self, node: dict, style_key: str) -> str | None:
        style_id = (node.get("styles") or {}).get(style_key)
        return self.styles.get(style_id, {}).get("name") if style_id else None

    def _paint_color(self, node: dict, paints_key: str, style_key: str, what: str) -> str | None:
        """Color of the top visible SOLID paint: bound style name first, then hex value (R6)."""
        all_paints = node.get(paints_key, []) or []
        paints = [p for p in all_paints if _visible(p) and p.get("type") == "SOLID"]
        if not paints:
            if paints_key == "fills" and any(_visible(p) for p in all_paints):
                self._warn(node, f"non-solid {what} (gradient/image) ignored")
            return None
        p = paints[-1]
        hex_value = figma_color_to_hex(p.get("color", {}), p.get("opacity", 1.0))
        style_name = self._style_name(node, style_key)
        token = self.ds.token_by_name("color", style_name) if style_name else None
        if not token:
            token = self.ds.color_token(hex_value)
        if token:
            return self._tok(token)
        bound = f"style '{style_name}'" if style_name else "no color style bound"
        self._warn(node, f"{what} color {hex_value} ({bound}) matches no color token")
        return f"raw:{hex_value}"

    def _fill_color(self, node: dict, what: str = "fill") -> str | None:
        return self._paint_color(node, "fills", "fill", what)

    def _border(self, node: dict) -> dict | None:
        """R8: {"width", "color"} for a visible SOLID stroke with strokeWeight > 0."""
        strokes = [s for s in node.get("strokes", []) or [] if _visible(s) and s.get("type") == "SOLID"]
        weight = node.get("strokeWeight") or 0
        if not strokes or weight <= 0:
            return None
        if self.ds.has_category("border"):
            width = self._dim(weight, "border", node, "strokeWeight")
        else:
            width = f"{weight:g}px"
        s = strokes[-1]
        alpha = (s.get("color") or {}).get("a", 1.0) * s.get("opacity", 1.0)
        if alpha <= 0.0001:
            color = "transparent"  # space reserved for e.g. a selected state; not a design error
        else:
            color = self._paint_color(node, "strokes", "stroke", "border")
        return {"width": width, "color": color}

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
            if not _visible(c):
                continue
            if c.get("type") == "TEXT" and c.get("name") == layer_name:
                return c.get("characters", "")
            stack.extend(c.get("children", []) or [])
        return None

    # ---------- components ----------
    def _component_meta(self, n: dict) -> tuple[dict, dict]:
        meta = self.components.get(n.get("componentId"), {}) or {}
        set_meta = self.component_sets.get(meta.get("componentSetId"), {}) or {}
        return meta, set_meta

    def _component_name(self, n: dict) -> str:
        meta, set_meta = self._component_meta(n)
        return set_meta.get("name") or meta.get("name") or n.get("name") or "?"

    def _manifest_entry(self, n: dict) -> dict | None:
        meta, set_meta = self._component_meta(n)
        return self.ds.match_component(
            keys=[set_meta.get("key"), meta.get("key")],
            names=[set_meta.get("name"), meta.get("name"), n.get("name")],
        )

    def _raw_props(self, n: dict) -> dict:
        """R5: every property under its exact key and its short name (before '#').
        VARIANTs win short-name clashes; INSTANCE_SWAP ids resolve to component names."""
        props = n.get("componentProperties") or {}

        def value(p: dict):
            v = p.get("value")
            if p.get("type") == "INSTANCE_SWAP" and isinstance(v, str):
                return (self.components.get(v) or {}).get("name", v)
            return v

        raw: dict = {key: value(p) for key, p in props.items()}
        for key, p in props.items():
            if p.get("type") != "VARIANT":
                raw.setdefault(key.split("#")[0], value(p))
        for key, p in props.items():
            if p.get("type") == "VARIANT":
                raw[key.split("#")[0]] = value(p)
        return raw

    # ---------- graphics ----------
    def _is_graphic(self, n: dict) -> bool:
        """R2: a shape, or a container whose (>= 1) visible children are all graphics.
        Instances mapped to UIKit components are never graphics."""
        t = n.get("type")
        if t in SHAPE_TYPES:
            return True
        if t not in GRAPHIC_CONTAINER_TYPES:
            return False
        if t == "INSTANCE" and self._manifest_entry(n) is not None:
            return False
        kids = _visible_children(n)
        return bool(kids) and all(self._is_graphic(c) for c in kids)

    def _graphic(self, n: dict) -> dict:
        box = n.get("absoluteBoundingBox") or {}
        out: dict = {"type": "Graphic", "name": n.get("name"),
                     "sizePx": f"{box.get('width', 0):g}x{box.get('height', 0):g}"}
        self._sizing(n, out)
        self._warn(n, GRAPHIC_WARNING)
        return out

    # ---------- node dispatch ----------
    def _node(self, n: dict, parent_mode: str | None, is_root: bool = False) -> dict | None:
        if not _visible(n):
            return None
        t = n.get("type")
        if t == "INSTANCE":
            entry = self._manifest_entry(n)
            if entry is not None:
                return self._instance(n, entry)
            return self._unmapped_instance(n, parent_mode, is_root)
        if not is_root and self._is_graphic(n):
            return self._graphic(n)
        if t == "TEXT":
            return self._text(n)
        if t in CONTAINER_TYPES:
            return self._container(n, parent_mode, is_root)
        self._warn(n, f"{t} node skipped; use a UIKit component (e.g. Icon, Divider, Image) instead")
        return None

    def _unmapped_instance(self, n: dict, parent_mode: str | None, is_root: bool) -> dict:
        """R1: keep the content of library components that are not in the manifest."""
        name = self._component_name(n)
        if not is_root and self._is_graphic(n):
            out = self._graphic(n)
            out["figmaComponent"] = name
            return out
        if not _visible_children(n):
            self._warn(n, f"instance of '{name}' is not in the UIKit manifest")
            return {"type": "Unmapped", "name": n.get("name"), "figmaComponent": name}
        self._warn(n, f"instance of '{name}' is not in the UIKit manifest; expanded as plain layout")
        out = self._container(n, parent_mode, is_root)
        out.setdefault("figmaComponent", name)
        return out

    def _container(self, n: dict, parent_mode: str | None, is_root: bool) -> dict:
        children = _visible_children(n)
        mode = n.get("layoutMode", "NONE") if n.get("type") != "GROUP" else "NONE"
        mode = mode or "NONE"

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
        border = self._border(n)
        if border:
            out["border"] = border
        if mode != "NONE":
            pad = self._padding(n)
            if pad:
                out["padding"] = pad
            gap = n.get("itemSpacing") or 0
            # R4: a gap only matters between two or more children.
            if gap and len(children) >= 2 and n.get("primaryAxisAlignItems") != "SPACE_BETWEEN":
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

        # R3: a wrapper that only holds one child and draws nothing is replaced by that child.
        if not is_root and len(kids) == 1 and not any(k in out for k in VISUAL_KEYS):
            child = kids[0]
            for key in ("fill", "weight"):
                if key in out and key not in child:
                    child[key] = out[key]
            return child

        if kids:
            out["children"] = kids
        return out

    @staticmethod
    def _has_paint(n: dict) -> bool:
        paints = (n.get("fills") or []) + (n.get("strokes") or [])
        return any(_visible(p) for p in paints)

    def _text(self, n: dict) -> dict:
        style = n.get("style", {}) or {}
        style_name = self._style_name(n, "text")
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

    def _instance(self, n: dict, entry: dict) -> dict:
        figma_name = self._component_name(n)
        self.used_components.add(entry["figma"])
        raw = self._raw_props(n)
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
