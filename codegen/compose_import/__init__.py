"""Draft manifest/components.json + manifest/tokens.json from an existing Jetpack Compose UIKit module.

    figma-codegen import-compose ../android-app/uikit --app ../android-app --figma-components figma.json

Tokens come from the Kotlin theme (so token paths match what the code can reference); components
from public @Composable functions. Everything that needs a human is listed in import-report.md.

    source.py      Kotlin module index (symbols, classes, enums)
    evaluator.py   Kotlin expression -> Value
    tokens.py      token collection and tokens.json layout
    components.py  component drafting
    report.py      Report and import-report.md
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..kotlin_source import KtFile, kotlin_files
from .components import DEFAULT_PRIMITIVES, collect_components, text_primitive
from .evaluator import Evaluator
from .report import Report, format_report
from .source import Module
from .tokens import build_tokens, collect_tokens

__all__ = ["run_import", "format_report", "Report"]


def run_import(module: Path, app: Optional[Path], figma_components: Optional[Path],
               base_primitives: Optional[dict]) -> tuple[dict, dict, Report]:
    mod = Module(module)
    rep = Report(module=str(module), files=len(mod.files))
    ev = Evaluator(mod)
    tokens, category_code, template, imports = build_tokens(collect_tokens(mod, ev, rep), rep)

    app_files: list[KtFile] = []
    if app:
        mod_root = module.resolve()
        for p in kotlin_files(app):
            rp = p.resolve()
            if rp == mod_root or mod_root in rp.parents:
                continue
            app_files.append(KtFile.load(p))
    figma = json.loads(figma_components.read_text()) if figma_components else []
    comps = collect_components(mod, app_files, figma, rep)

    primitives = dict(base_primitives or DEFAULT_PRIMITIVES)
    text = text_primitive(comps)
    if text:
        primitives["Text"] = text["Text"]
        imports.add(text["import"])
    primitives["imports"] = ", ".join(sorted(imports))
    manifest = {
        "version": "0.1.0",
        "tokenCode": {"compose": template},
        "categoryCode": category_code,
        "primitives": {"compose": primitives},
        "components": comps,
    }
    if app is None:
        rep.notes.append("No `--app` given: every example is synthesized from the signature. Pass the app repo "
                         "to use real call sites.")
    if not text:
        rep.notes.append("No text composable with `text: String` and `style: TextStyle` found; "
                         "`primitives.compose.Text` uses Material `Text`. Edit it if your UIKit has its own.")
    return manifest, tokens, rep
