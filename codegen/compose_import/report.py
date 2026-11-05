"""What the importer could not decide on its own, rendered as import-report.md."""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Report:
    module: str
    files: int = 0
    unparsed: list = field(default_factory=list)       # (where, name, expr)
    skipped_themes: list = field(default_factory=list)  # (what, why)
    no_accessor: list = field(default_factory=list)     # class names
    notes: list = field(default_factory=list)
    components: list = field(default_factory=list)      # dict rows
    figma_unmatched: list = field(default_factory=list)


def format_report(rep: Report, manifest: dict, tokens: dict) -> str:
    L = [f"# Compose import report", "", f"Module: `{rep.module}` ({rep.files} Kotlin files)", ""]
    n_tokens = sum(len(v) for v in tokens.values())
    L += ["## Tokens", "",
          f"{n_tokens} tokens: " + ", ".join(f"{c} {len(v)}" for c, v in tokens.items()), "",
          f"- `tokenCode.compose`: `{manifest['tokenCode']['compose']}`",
          f"- `categoryCode`: `{json.dumps(manifest['categoryCode'])}`",
          f"- imports: `{manifest['primitives']['compose']['imports']}`", ""]
    if rep.no_accessor:
        L += ["**Theme classes skipped: no accessor found** (no `val x: Class` in an object, no "
              "CompositionLocal providing it). Add an accessor or write these tokens by hand:", ""]
        L += [f"- `{c}`" for c in rep.no_accessor] + [""]
    if rep.unparsed:
        L += ["**Values the importer could not evaluate** — add these tokens by hand if they matter:", ""]
        L += [f"- `{n}` = `{e}` ({w})" for w, n, e in rep.unparsed] + [""]
    if rep.skipped_themes:
        L += ["**Skipped** (one theme is enough for matching):", ""]
        L += [f"- {w} — {why}" for w, why in rep.skipped_themes] + [""]
    if rep.notes:
        L += ["## Notes", ""] + [f"- {n}" for n in rep.notes] + [""]
    L += ["## Components", "",
          "Sorted by number of call sites in the app. Every row needs a look; the columns say where.", "",
          "| composable | figma name | kind | call sites | example | drafted props | KDoc | note |",
          "|---|---|---|---|---|---|---|---|"]
    for c in rep.components:
        figma = c["figma"] + (" ✓" if c["figma_src"] == "figma" else " (guess)")
        L.append(f"| `{c['name']}` ({c['where']}) | {figma} | {c['kind']} | {c['usages']} | {c['example']} | "
                 f"{c['props'] or '-'} | {'yes' if c['description'] else '**missing**'} | {c['note'] or ''} |")
    L.append("")
    if rep.figma_unmatched:
        L += ["**Figma components with no composable** (map them by hand, or they become raw layouts):", ""]
        L += [f"- {n}" for n in rep.figma_unmatched] + [""]
    L += ["## Checklist", "",
          "1. Figma names marked *(guess)*: replace with the exact component (set) name from "
          "`figma-codegen components <library-url>`, or rerun with `--figma-components`.",
          "2. `props`: drafted from enum and text parameters. `figmaProp` must be the Figma property name, "
          "`values` the Figma variant values, `figmaText` the text layer name. Add `map` where Figma and "
          "code values differ, and Boolean props like `enabled` (`State` → `map`).",
          "3. `kind`: `input` creates a state binding, `action` a callback, none = display only.",
          "4. Write a one-line `description` where KDoc is missing; the model reads it.",
          "5. Point `paths.manifest` / `paths.tokens` at these files, then run `figma-codegen spec <frame>` on "
          "a few real screens: each warning is a missing token, a wrong name or an unmapped prop.",
          "6. Delete components the generator should never use (internal building blocks, deprecated ones).",
          ""]
    return "\n".join(L)
