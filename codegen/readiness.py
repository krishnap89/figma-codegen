"""Readiness score: one shared answer to "is this frame ready for code?", for designers.

Computed from the simplifier's warnings, no AI. Each issue costs its weight (readiness.weights in
codegen.yaml); the total is normalised by the size of the frame (spec nodes):

    score = max(0, 100 − 100 × Σ weights / (6 × nodes))

so a frame where every node has a missing component (weight 6) scores 0, and one missing
component in a 30-node screen costs about 3 points. Levels: ready ≥ 85, almost 60–84, needs work < 60.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

from .config import READINESS_WEIGHTS
from .gaps import node_url
from .simplify import FigmaWarning, SimplifyResult
from .style import Style

NORMALISER = 6          # weight of the worst issue: every node having it scores 0
LEVELS = (("ready", 85), ("almost", 60), ("needs work", 0))

# simplifier warning kind -> readiness issue
ISSUE_OF_KIND = {
    "unknown_component": "missing_component",
    "no_auto_layout": "no_auto_layout",
    "graphic": "hand_drawn_graphic",
    "skipped_node": "hand_drawn_graphic",
    "color_unbound": "unbound_style",
    "typography": "unbound_style",
    "non_solid_paint": "unbound_style",
    "dimension": "value_without_token",
    "color_style_unmapped": "value_without_token",
    "default_name": "default_name",
    "prop_value": "unknown_variant",
}

# issue -> (label, what it means, how to fix it in Figma)
ISSUES: dict[str, tuple[str, str, str]] = {
    "missing_component": (
        "components the app doesn't have",
        "These are library components that the app's UIKit has no equivalent for, so the code can't use them.",
        "Swap them for a component from the UIKit library, or ask the design system team to add the component."),
    "no_auto_layout": (
        "frames without auto-layout",
        "Without auto-layout, layers have fixed positions; code is built from rows and columns.",
        "Select the frame and add auto-layout (Shift+A); set direction, gap and padding."),
    "hand_drawn_graphic": (
        "hand-drawn shapes",
        "Vectors and shapes drawn in the frame instead of an icon or image component.",
        "Use an Icon or Image component from the library, or mark the artwork for export as an asset."),
    "unbound_style": (
        "colors or texts without a library style",
        "Raw colors and font settings can't be matched to the app's theme reliably.",
        "Apply a color style and a text style from the library instead of typing values."),
    "value_without_token": (
        "values off the design scale",
        "Spacing, corner radius or color values that match no design token.",
        "Snap padding, gaps and radii to the spacing/radius scale; pick a library color."),
    "default_name": (
        "layers with default names",
        "Layer names become names in code (state and callbacks); 'Text Field' tells a developer nothing.",
        "Rename the layer for what it is, e.g. 'EmailField', 'LoginButton'."),
    "unknown_variant": (
        "variants the app doesn't support",
        "Component variants (e.g. a button style) that the app's UIKit component doesn't have.",
        "Pick a supported variant, or ask the design system team to add it."),
}


def issue_of(w: FigmaWarning) -> str | None:
    if w.kind == "graphic" and w.detail.get("component"):
        return "missing_component"     # artwork that is an instance of a library component not in UIKit
    return ISSUE_OF_KIND.get(w.kind)


@dataclass
class IssueGroup:
    issue: str
    weight: float
    nodes: list[dict] = field(default_factory=list)   # [{"name", "id"}]
    points: float = 0.0                               # score points this group costs

    @property
    def count(self) -> int:
        return len(self.nodes)

    def to_json(self, file_key: str | None = None) -> dict:
        label, explanation, fix = ISSUES[self.issue]
        return {"issue": self.issue, "label": label, "count": self.count, "weight": self.weight,
                "points": round(self.points, 1), "explanation": explanation, "fix_in_figma": fix,
                "nodes": [{**n, "url": node_url(file_key, n["id"])} for n in self.nodes]}


@dataclass
class Readiness:
    score: int
    level: str
    node_count: int
    groups: list[IssueGroup]                          # most points first
    ignored: list[dict] = field(default_factory=list)

    def to_json(self, file_key: str | None = None) -> dict:
        return {"score": self.score, "level": self.level, "node_count": self.node_count,
                "issues": [g.to_json(file_key) for g in self.groups], "ignored_layers": self.ignored}


def _count_nodes(node: dict) -> int:
    return 1 + sum(_count_nodes(c) for c in node.get("children") or [])


def level_for(score: float) -> str:
    return next(name for name, minimum in LEVELS if score >= minimum)


def compute(result: SimplifyResult, weights: dict[str, float] | None = None) -> Readiness:
    weights = {**READINESS_WEIGHTS, **(weights or {})}
    nodes = max(1, _count_nodes(result.spec["root"]))
    groups: dict[str, IssueGroup] = {}
    for w in result.issues:
        issue = issue_of(w)
        if issue is None or not weights.get(issue):
            continue
        g = groups.setdefault(issue, IssueGroup(issue, weights[issue]))
        g.nodes.append({"name": w.node_name, "id": w.node_id})
    for g in groups.values():
        g.points = 100 * g.weight * g.count / (NORMALISER * nodes)
    score = max(0, round(100 - sum(g.points for g in groups.values())))
    ordered = sorted(groups.values(), key=lambda g: (-g.points, list(ISSUES).index(g.issue)))
    return Readiness(score, level_for(score), nodes, ordered, list(result.ignored))


# ---------- text ----------
LEVEL_STYLE = {"ready": "green", "almost": "yellow", "needs work": "red"}
MAX_NODES = 8


def render_text(r: Readiness, screen: str, file_key: str | None = None, style: Style | None = None,
                verbose: bool = False) -> str:
    st = style or Style(False)
    lines = [st(f"Readiness: {r.level} ({r.score}/100)", "bold", LEVEL_STYLE[r.level])
             + st.dim(f" — {screen}, {r.node_count} layers"
                      + (f", {len(r.ignored)} device layer(s) ignored" if r.ignored else ""))]
    if not r.groups:
        lines.append(st.ok("  No issues: this frame maps cleanly to UIKit components and tokens."))
        return "\n".join(lines)
    for g in r.groups:
        label, explanation, fix = ISSUES[g.issue]
        lines.append("")
        lines.append(f"  {st(str(g.count), 'bold')} {st(label, 'bold')}  " + st.dim(f"(−{g.points:.0f} points)"))
        lines.append(f"    {explanation}")
        lines.append(st("    Fix in Figma: ", "cyan") + fix)
        shown = g.nodes if verbose else g.nodes[:MAX_NODES]
        for n in shown:
            url = node_url(file_key, n["id"])
            lines.append(st.dim(f"    · {n['name']}  {url or n['id']}"))
        if len(g.nodes) > len(shown):
            lines.append(st.dim(f"    · … {len(g.nodes) - len(shown)} more (--verbose lists all)"))
    return "\n".join(lines)


# ---------- HTML ----------
def render_html(r: Readiness, screen: str, file_key: str | None = None, figma_url: str | None = None) -> str:
    esc = html.escape
    sections = []
    for g in r.groups:
        label, explanation, fix = ISSUES[g.issue]
        items = []
        for n in g.nodes:
            url = node_url(file_key, n["id"])
            name = esc(n["name"] or "?")
            items.append(f'<li><a href="{esc(url)}" target="_blank" rel="noopener">{name}</a></li>' if url
                         else f"<li>{name} <code>{esc(n['id'])}</code></li>")
        sections.append(
            f'<section><h2><span class="count">{g.count}</span> {esc(label)}'
            f'<span class="pts">−{g.points:.0f} points</span></h2>'
            f"<p>{esc(explanation)}</p><p class=\"fix\"><strong>Fix in Figma:</strong> {esc(fix)}</p>"
            f'<ul class="nodes">{"".join(items)}</ul></section>')
    if not sections:
        sections.append('<section><p>No issues: this frame maps cleanly to UIKit components and tokens.</p></section>')
    meta = [f"<div><dt>Layers</dt><dd>{r.node_count}</dd></div>"]
    if r.ignored:
        meta.append(f"<div><dt>Ignored device layers</dt><dd>{len(r.ignored)}</dd></div>")
    if figma_url:
        meta.append(f'<div><dt>Figma</dt><dd><a href="{esc(figma_url)}" target="_blank" rel="noopener">'
                    f"open frame</a></dd></div>")
    level_class = r.level.replace(" ", "-")
    return (_TEMPLATE.replace("__TITLE__", esc(screen)).replace("__SCORE__", str(r.score))
            .replace("__LEVEL__", esc(r.level)).replace("__LEVELCLASS__", level_class)
            .replace("__META__", "".join(meta)).replace("__SECTIONS__", "".join(sections)))


# Same look as the compare page (compare.py).
_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · Design readiness</title>
<style>
:root {
  --bg: #f6f6f4; --panel: #ffffff; --text: #1d1d1b; --muted: #6b6b66; --line: #deded8;
  --accent: #2f6fe4; --ready: #1f7a3d; --almost: #8a6100; --needs-work: #b3261e;
  --ready-bg: #e3f4e8; --almost-bg: #fff4d6; --needs-work-bg: #fde7e5;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161616; --panel: #1f1f1f; --text: #ededea; --muted: #9a9a94; --line: #333331;
    --accent: #6c9cff; --ready: #7ed39a; --almost: #f3d98a; --needs-work: #ff9b93;
    --ready-bg: #183223; --almost-bg: #3a3014; --needs-work-bg: #3d1a17;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
header { padding: 16px; border-bottom: 1px solid var(--line); background: var(--panel); }
h1 { margin: 0 0 10px; font-size: 18px; }
.score { display: flex; align-items: baseline; gap: 12px; margin-bottom: 8px; }
.score b { font-size: 40px; line-height: 1; }
.score small { color: var(--muted); }
.badge { padding: 2px 10px; border-radius: 999px; font-weight: 600; }
.badge.ready { color: var(--ready); background: var(--ready-bg); }
.badge.almost { color: var(--almost); background: var(--almost-bg); }
.badge.needs-work { color: var(--needs-work); background: var(--needs-work-bg); }
dl { display: flex; flex-wrap: wrap; gap: 2px 18px; margin: 0; color: var(--muted); font-size: 12px; }
dl div { display: flex; gap: 6px; } dt::after { content: ":"; } dd { margin: 0; color: var(--text); }
main { max-width: 820px; margin: 0 auto; padding: 16px; }
section { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px;
  margin-bottom: 12px; }
h2 { display: flex; align-items: baseline; gap: 8px; margin: 0 0 6px; font-size: 15px; }
h2 .count { font-variant-numeric: tabular-nums; }
h2 .pts { margin-left: auto; color: var(--muted); font-weight: 400; font-size: 12px; white-space: nowrap; }
p { margin: 4px 0; } .fix { color: var(--text); }
ul.nodes { display: flex; flex-wrap: wrap; gap: 6px; list-style: none; margin: 10px 0 0; padding: 0; }
ul.nodes li { border: 1px solid var(--line); border-radius: 6px; padding: 2px 8px; font-size: 13px;
  max-width: 100%; overflow-wrap: anywhere; }
a { color: var(--accent); text-decoration: none; } a:hover { text-decoration: underline; }
code { font: 12px ui-monospace, monospace; color: var(--muted); }
</style>
</head>
<body>
<header>
  <h1>__TITLE__ · Is this frame ready for code?</h1>
  <div class="score"><b>__SCORE__</b><small>/ 100</small><span class="badge __LEVELCLASS__">__LEVEL__</span></div>
  <dl>__META__</dl>
</header>
<main>
__SECTIONS__
</main>
</body>
</html>
"""
