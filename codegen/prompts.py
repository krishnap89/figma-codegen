"""Prompt construction. Everything the model needs is assembled deterministically here."""
from __future__ import annotations

import json
from pathlib import Path

from .design_system import DesignSystem
from .simplify import SimplifyResult, collect

PLATFORMS = {
    "compose": {
        "label": "Jetpack Compose (Kotlin)",
        "fence": "kotlin",
        "ext": ".kt",
        "banned": "Material components (material3 Button, TextField, ...), raw dp/sp values, Color(0x...) or fontSize",
        "shape": """\
- {package_line}
- `data class {name}State(...)` with exactly these properties: {state_props}
- `@Composable fun {name}(state: {name}State, {params}modifier: Modifier = Modifier)`
  - one `on<Binding>Change: (<Type>) -> Unit` per binding, typed like its state property, \
and one `() -> Unit` per event: {events}
- A private `@Preview` composable `{name}Preview()` at the end of the file.""",
    },
    "swiftui": {
        "label": "SwiftUI",
        "fence": "swift",
        "ext": ".swift",
        "banned": "SwiftUI Button/TextField, raw numbers for padding/spacing/radius, Color literals or .font(.system(...))",
        "shape": """\
- `struct {name}State` with exactly these properties: {state_props}
- `struct {name}: View` with `@Binding var state: {name}State` and one `let <event>: () -> Void` per event: {events}
- Bind inputs with `$state.<binding>`.
- A `#Preview` at the end of the file.""",
    },
}

SYSTEM = """You are a senior mobile engineer generating production UI code in {label} with the company's UIKit design system.

Hard rules:
1. Use ONLY the UIKit components and layout primitives documented in the request. Never use {banned}.
2. Use ONLY design tokens, written exactly as the code expressions in the token table.
3. A spec value starting with "raw:" has no token: use the closest token from the table and add `// TODO(design): <what>` on that line.
4. A node with "type": "Unmapped" has no UIKit equivalent: put `// TODO(design): <name> is not in UIKit` at that position. Do not invent components.
   A node with "type": "Graphic" is hand-drawn artwork: put a placeholder of its `sizePx` there with `// TODO(design): export <name> as an asset`.
   A layout node with "figmaComponent" is an unmapped library component expanded into plain layout: build its children normally.
5. The screen is stateless: data comes in through the state object, user actions go out through callbacks. No ViewModel, navigation, networking or side effects.
6. Keep the layer structure of the spec (Column/Row/Box nesting, order of children).
7. If you are unsure of a component's API, call `get_component_docs`. To see how existing screens are written, use `search_code` / `read_file` if available.
8. Your final answer must be exactly one ```{fence} code block containing the complete file, and nothing else.
"""


# stateType -> (type, default) per platform.
STATE_TYPES = {
    "compose": {"String": ("String", '""'), "Boolean": ("Boolean", "false"), "Int": ("Int", "0")},
    "swiftui": {"String": ("String", '""'), "Boolean": ("Bool", "false"), "Int": ("Int", "0")},
}


def typed_bindings(ds: DesignSystem, spec_root: dict, platform: str) -> list[tuple[str, str, str]]:
    """[(binding, type, default)] in tree order; the type comes from the component's `stateType`."""
    types = STATE_TYPES[platform]
    found: list[tuple[str, str, str]] = []

    def walk(node: dict) -> None:
        if "binding" in node:
            comp = ds.component(node.get("component", "")) or {}
            state_type = comp.get("stateType", "String")
            typ, default = types.get(state_type, types["String"])
            found.append((node["binding"], typ, default))
        for c in node.get("children", []) or []:
            walk(c)

    walk(spec_root)
    return found


def load_examples(examples_dir: str | None, platform: str, limit: int) -> list[tuple[str, str]]:
    """Few-shot pairs: examples/<platform>/<Name>.spec.json + <Name>.kt|.swift"""
    if not examples_dir:
        return []
    d = Path(examples_dir) / platform
    if not d.is_dir():
        return []
    pairs = []
    for spec_file in sorted(d.glob("*.spec.json")):
        code_file = spec_file.with_name(spec_file.name.replace(".spec.json", PLATFORMS[platform]["ext"]))
        if code_file.exists():
            pairs.append((spec_file.read_text(), code_file.read_text()))
        if len(pairs) >= limit:
            break
    return pairs


def system_prompt(platform: str) -> str:
    p = PLATFORMS[platform]
    return SYSTEM.format(label=p["label"], banned=p["banned"], fence=p["fence"])


def user_prompt(
    ds: DesignSystem,
    result: SimplifyResult,
    platform: str,
    screen_name: str,
    package: str | None,
    examples: list[tuple[str, str]],
) -> str:
    p = PLATFORMS[platform]
    spec = result.spec
    bindings = typed_bindings(ds, spec["root"], platform)
    events = collect(spec["root"], "event")

    comp_docs = [ds.component_doc(name, platform) for name in sorted(result.used_components)]
    token_table = "\n".join(f"{t} -> {ds.token_code(t, platform)}" for t in sorted(result.used_tokens))
    primitives = ds.primitives(platform)

    if platform == "compose":
        state_props = [f"`val {b}: {t} = {d}`" for b, t, d in bindings]
        params = "".join(f"on{b[:1].upper() + b[1:]}Change: ({t}) -> Unit, " for b, t, _ in bindings)
        params += "".join(f"{e}: () -> Unit, " for e in events)
    else:
        state_props = [f"`var {b}: {t} = {d}`" for b, t, d in bindings]
        params = ""
    shape = p["shape"].format(
        name=screen_name,
        package_line=f"`package {package}` as the first line" if package else "no package line needed",
        state_props=", ".join(state_props) or "(none)",
        events=", ".join(events) or "(none)",
        params=params,
    )

    parts = [f"## Screen spec (from Figma)\n```json\n{json.dumps(spec, indent=1)}\n```"]
    parts.append("## UIKit components used\n```json\n" + json.dumps(comp_docs, indent=1) + "\n```")
    if primitives:
        parts.append("## Layout primitives\n" + "\n".join(f"- {k}: `{v}`" for k, v in primitives.items()))
    parts.append(f"## Token table (spec token -> code)\n```\n{token_table or '(no tokens)'}\n```")
    if spec.get("warnings"):
        parts.append("## Designer warnings (handle with TODO comments, don't guess)\n" +
                     "\n".join(f"- {w}" for w in spec["warnings"]))
    for i, (ex_spec, ex_code) in enumerate(examples, 1):
        parts.append(f"## Reference example {i}: spec\n```json\n{ex_spec}\n```\n"
                     f"## Reference example {i}: code written by our team\n```{p['fence']}\n{ex_code}\n```")
    parts.append(f"## Task\nGenerate the {p['label']} file for screen `{screen_name}`.\nRequired shape:\n{shape}")
    return "\n\n".join(parts)


def fix_prompt(feedback: str, platform: str) -> str:
    return (
        f"The code failed validation.\n\n{feedback}\n\n"
        "Fix every issue. Keep everything that was correct. "
        f"Reply with the complete corrected file in one ```{PLATFORMS[platform]['fence']} block and nothing else."
    )


def no_code_prompt(platform: str) -> str:
    return f"I could not find a code block. Reply with the complete file in one ```{PLATFORMS[platform]['fence']} block."
