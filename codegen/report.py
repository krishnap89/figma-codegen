"""Human-readable summary of Figma warnings, grouped by kind.

    Figma: 50 warnings — fixing these in Figma gives better code
      12  colors not bound to a color style      Title, Subtitle, Card +9 more
       4  hand-drawn graphics                    Logo, Illustration, Badge +1 more
      ...
      run with --verbose to list every warning

With verbose=True every warning is listed under its group, with a hint on how to fix it.
"""
from __future__ import annotations

from collections import OrderedDict

from .simplify import FigmaWarning
from .style import Style

# kind -> (singular, plural, how to fix it in Figma)
KINDS: dict[str, tuple[str, str, str]] = {
    "color_unbound": ("color not bound to a color style", "colors not bound to a color style",
                      "apply a color style from the library instead of a raw hex value"),
    "color_style_unmapped": ("color style with no matching token", "color styles with no matching token",
                             "use a library color style, or add the style to manifest/tokens.json"),
    "non_solid_paint": ("gradient/image fill ignored", "gradient/image fills ignored",
                        "use a UIKit Image component or export the artwork as an asset"),
    "dimension": ("spacing/radius value with no token", "spacing/radius values with no token",
                  "snap padding, gaps and corner radii to the spacing/radius scale"),
    "typography": ("text with no typography token", "texts with no typography token",
                   "apply a text style from the library"),
    "graphic": ("hand-drawn graphic", "hand-drawn graphics",
                "replace with a UIKit Icon/Image component, or export as an asset"),
    "skipped_node": ("loose shape skipped", "loose shapes skipped",
                     "replace with a UIKit component (Icon, Divider, Image)"),
    "unknown_component": ("component not in the UIKit manifest", "components not in the UIKit manifest",
                          "use a UIKit component, or add this one to manifest/components.json"),
    "no_auto_layout": ("frame without auto-layout", "frames without auto-layout",
                       "add auto-layout so the layout can be expressed as rows and columns"),
    "prop_value": ("component prop with an unknown value", "component props with an unknown value",
                   "pick a supported variant, or map the value in manifest/components.json"),
    "default_name": ("layer with a default name", "layers with a default name",
                     "rename the layer (e.g. 'EmailField', 'LoginButton') for readable code"),
}
_FALLBACK = ("other warning", "other warnings", "")
MAX_EXAMPLES = 3
MAX_NAME = 24


def group_warnings(issues: list[FigmaWarning]) -> "OrderedDict[str, list[FigmaWarning]]":
    """Groups by kind, largest group first (ties keep first-seen order)."""
    groups: dict[str, list[FigmaWarning]] = {}
    for w in issues:
        groups.setdefault(w.kind, []).append(w)
    return OrderedDict(sorted(groups.items(), key=lambda kv: -len(kv[1])))


def _label(kind: str, n: int) -> str:
    singular, plural, _ = KINDS.get(kind, _FALLBACK)
    return singular if n == 1 else plural


def _short(name: str) -> str:
    return name if len(name) <= MAX_NAME else name[: MAX_NAME - 1] + "…"


def _examples(ws: list[FigmaWarning]) -> str:
    """'Overlay, Frame 7 ×3, Logo +2 more' — counts add up to the group size."""
    counts: dict[str, int] = {}
    for w in ws:
        counts[w.node_name] = counts.get(w.node_name, 0) + 1
    names = list(counts)[:MAX_EXAMPLES]
    shown = ", ".join(_short(n) + (f" ×{counts[n]}" if counts[n] > 1 else "") for n in names)
    rest = len(ws) - sum(counts[n] for n in names)
    return shown + (f" +{rest} more" if rest else "")


def format_warnings(issues: list[FigmaWarning], verbose: bool = False, style: Style | None = None) -> str:
    st = style or Style(False)
    if not issues:
        return st.ok("Figma: no warnings")
    groups = group_warnings(issues)
    n = len(issues)
    lines = [st(f"Figma: {n} warning{'s' if n != 1 else ''}", "bold", "yellow")
             + st.dim(" — fixing these in Figma gives better code")]
    width = max(len(_label(k, len(ws))) for k, ws in groups.items())
    count_w = len(str(max(len(ws) for ws in groups.values())))
    for kind, ws in groups.items():
        label = _label(kind, len(ws))
        count = st(f"{len(ws):>{count_w}}", "bold", "yellow")   # pad before coloring
        if verbose:
            hint = KINDS.get(kind, _FALLBACK)[2]
            lines.append(f"  {count}  {st(label, 'bold')}" + (st(f"  → {hint}", "cyan") if hint else ""))
            lines += [f"      {st.dim(f'[{w.node_name} {w.node_id}]')} {w.message}" for w in ws]
        else:
            lines.append(f"  {count}  {label:<{width}}  {st.dim(_examples(ws))}")
    if not verbose:
        lines.append(st.dim("  run with --verbose to list every warning"))
    return "\n".join(lines)
