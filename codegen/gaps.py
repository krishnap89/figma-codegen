"""Design-system gap report: what the frame needs that UIKit doesn't have yet.

A refinement that needs a UIKit change (a new component, a variant, a token) belongs with the
design system team, not hacked into one screen. Built from the simplifier's warnings:

  missing_component    library component not in the manifest (name, key, how often used)
  unknown_variant      component prop value the manifest doesn't cover
  missing_token        color/spacing/radius/typography value with no token (+ nearest token)
  non_library_element  hand-drawn graphic or loose shape standing in for a component
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .design_system import DesignSystem
from .simplify import FigmaWarning

KIND_ORDER = ("missing_component", "unknown_variant", "missing_token", "non_library_element")
OWNERS = {"missing_component": "design system team", "unknown_variant": "design system team",
          "missing_token": "design system team", "non_library_element": "designer"}
TITLES = {
    "missing_component": ("Components missing from UIKit",
                          "Add the component to UIKit and manifest/components.json, or use an existing one."),
    "unknown_variant": ("Variants UIKit does not support",
                        "Add the variant to the UIKit component (and its manifest mapping), or pick a supported one."),
    "missing_token": ("Values without a token",
                      "Add a token, or snap the value in Figma to the nearest existing token."),
    "non_library_element": ("Elements not from the library",
                            "Replace with a UIKit component (Icon, Image, Divider…), or export the artwork as an asset."),
}
COUNT_LABELS = {"missing_component": ("missing component", "missing components"),
                "unknown_variant": ("unsupported variant", "unsupported variants"),
                "missing_token": ("value without a token", "values without a token"),
                "non_library_element": ("non-library element", "non-library elements")}
TOKEN_WARNINGS = {"dimension", "color_unbound", "color_style_unmapped", "typography"}
# Gaps that block an update: the change can't be built until UIKit has the thing.
BLOCKING = {"missing_component", "unknown_variant"}


@dataclass
class Gap:
    kind: str
    name: str                          # "Promo Banner", "Button.Style = Tertiary", "spacing 13px"
    owner: str
    nodes: list[dict] = field(default_factory=list)   # [{"name", "id"}] where it occurs in the frame
    key: str | None = None             # Figma component key (missing_component)
    value: Any = None
    nearest: str | None = None         # nearest existing token (missing_token)
    known: list | None = None          # supported values (unknown_variant)
    message: str = ""

    @property
    def count(self) -> int:
        return len(self.nodes)

    def to_json(self) -> dict:
        return {**asdict(self), "count": self.count}


def _fmt_px(v) -> str:
    return f"{v:g}px" if isinstance(v, (int, float)) else str(v)


def _token_gap(w: FigmaWarning, ds: DesignSystem | None) -> tuple[tuple, dict]:
    d = w.detail
    cat, value = d.get("category"), d.get("value")
    if cat == "color":
        name = f"color {value}" + (f" (style '{d['style']}')" if d.get("style") else "")
        nearest = ds.nearest_color(value) if ds and isinstance(value, str) else None
    elif cat == "typography":
        name = f"typography {_fmt_px(value)}/{d.get('weight')}" + (f" (style '{d['style']}')" if d.get("style") else "")
        nearest = ds.nearest_typography(value) if ds and isinstance(value, (int, float)) else None
    else:
        name = f"{cat} {_fmt_px(value)}"
        nearest = ds.nearest_dimension(value, cat) if ds and isinstance(value, (int, float)) else None
    return ("missing_token", name), {"value": value, "nearest": nearest}


def collect_gaps(warnings: list[FigmaWarning], ds: DesignSystem | None = None) -> list[Gap]:
    """Gaps from SimplifyResult.issues, one per distinct thing, with every node it occurs on.
    `ds` (optional) adds the nearest existing token to missing_token gaps."""
    gaps: dict[tuple, Gap] = {}

    def add(ident: tuple, node: FigmaWarning, **fields) -> None:
        gap = gaps.get(ident)
        if gap is None:
            gap = gaps[ident] = Gap(kind=ident[0], name=ident[1], owner=OWNERS[ident[0]], **fields)
        gap.nodes.append({"name": node.node_name, "id": node.node_id})

    for w in warnings:
        d = w.detail
        if w.kind == "unknown_component" or (w.kind == "graphic" and d.get("component")):
            add(("missing_component", d.get("component") or w.node_name), w, key=d.get("key"),
                message="library component not in the UIKit manifest")
        elif w.kind == "prop_value" and d.get("component"):
            add(("unknown_variant", f"{d['component']}.{d['prop']} = {d['value']}"), w, value=d["value"],
                known=d.get("known"), message=w.message)
        elif w.kind in TOKEN_WARNINGS and d.get("category"):
            ident, fields = _token_gap(w, ds)
            add(ident, w, **fields, message=w.message)
        elif w.kind in ("graphic", "skipped_node"):
            add(("non_library_element", f"{w.node_name} {w.node_id}"), w, message=w.message)
    for g in gaps.values():
        if g.kind == "non_library_element":
            g.name = g.nodes[0]["name"]
    return sorted(gaps.values(), key=lambda g: (KIND_ORDER.index(g.kind), -g.count, g.name))


def blocking_node_ids(gaps: list[Gap]) -> set[str]:
    """Nodes whose design needs a UIKit change first (missing component / unknown variant)."""
    return {n["id"] for g in gaps if g.kind in BLOCKING for n in g.nodes if n.get("id")}


def node_url(file_key: str | None, node_id: str) -> str | None:
    if not file_key or not node_id:
        return None
    return f"https://www.figma.com/design/{file_key}?node-id={node_id.replace(':', '-')}"


def _node_links(nodes: list[dict], file_key: str | None) -> str:
    counts: dict[tuple, int] = {}
    for n in nodes:
        counts[(n["name"], n["id"])] = counts.get((n["name"], n["id"]), 0) + 1
    parts = []
    for (name, nid), k in counts.items():
        url = node_url(file_key, nid)
        link = f"[{name}]({url})" if url else f"{name} (`{nid}`)"
        parts.append(link + (f" ×{k}" if k > 1 else ""))
    return ", ".join(parts)


def render_markdown(gaps: list[Gap], screen: str, file_key: str | None = None, figma_url: str | None = None) -> str:
    lines = [f"# Design-system gaps: {screen}", ""]
    if figma_url or file_key:
        lines += [f"Figma: {figma_url or f'https://www.figma.com/design/{file_key}'}", ""]
    if not gaps:
        return "\n".join(lines + ["No gaps: everything in this frame maps to UIKit components and tokens.", ""])
    counts = {k: sum(1 for g in gaps if g.kind == k) for k in KIND_ORDER}
    lines += [", ".join(f"{n} {COUNT_LABELS[k][n != 1]}" for k, n in counts.items() if n), ""]
    for kind in KIND_ORDER:
        items = [g for g in gaps if g.kind == kind]
        if not items:
            continue
        title, how = TITLES[kind]
        lines += [f"## {title}", "", f"Owner: **{OWNERS[kind]}**. {how}", ""]
        for g in items:
            where = _node_links(g.nodes, file_key)
            if kind == "missing_component":
                key = f" (key `{g.key}`)" if g.key else ""
                lines.append(f"- **{g.name}**{key}, used {g.count}×: {where}")
            elif kind == "unknown_variant":
                known = f" (supported: {', '.join(map(str, g.known))})" if g.known else ""
                lines.append(f"- **{g.name}**{known}: {where}")
            elif kind == "missing_token":
                nearest = f", nearest token `{g.nearest}`" if g.nearest else ""
                lines.append(f"- **{g.name}**{nearest}: {where}")
            else:
                lines.append(f"- {where}: {g.message}")
        lines.append("")
    return "\n".join(lines)


def render_json(gaps: list[Gap], screen: str, file_key: str | None = None, figma_url: str | None = None) -> str:
    out = {"schema": 1, "screen": screen, "figma": {"file_key": file_key, "url": figma_url},
           "gaps": [{**g.to_json(), "nodes": [{**n, "url": node_url(file_key, n["id"])} for n in g.nodes]}
                    for g in gaps]}
    return json.dumps(out, indent=2, ensure_ascii=False) + "\n"
