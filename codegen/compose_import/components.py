"""Draft manifest component entries from public @Composable functions and their app call sites."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from ..design_system import normalize_name
from ..kotlin_source import KtFile, collapse, is_hidden, match_bracket, split_args
from ..naming import camel, words
from .report import Report
from .source import Module

COMPOSE_BUILTINS = {"Box", "Row", "Column", "Text", "Spacer", "Surface", "Card", "Button", "Icon", "Image",
                    "Scaffold", "Divider", "LazyColumn", "LazyRow", "TextField", "Checkbox", "Switch"}


TEXT_PARAMS = {"text", "label", "title", "subtitle", "placeholder", "hint", "error", "errortext",
               "helpertext", "supportingtext", "description", "caption", "message", "headline", "body"}


@dataclass
class Param:
    name: str
    type: str
    default: Optional[str]
    text: str


def _params(f: KtFile, open_: int) -> tuple[list[Param], int]:
    close = match_bracket(f.masked, open_)
    out = []
    for piece, _ in split_args(f.code[open_ + 1:close], f.masked[open_ + 1:close]):
        m = re.match(r"\s*((?:@[\w.]+(?:\([^)]*\))?\s*)*)(?:vararg\s+|noinline\s+|crossinline\s+)*"
                     r"([A-Za-z_]\w*)\s*:\s*(.+?)\s*(?:=\s*(.+))?\s*$", piece, re.S)
        if m:
            typ = collapse(m.group(3))
            if re.search(r"@Composable", m.group(1) or ""):
                typ = "@Composable " + typ
            out.append(Param(m.group(2), typ, collapse(m.group(4)) if m.group(4) else None, collapse(piece)))
    return out, close


def _fn_arg(typ: str) -> Optional[str]:
    """'(String) -> Unit' -> 'String'; '() -> Unit' -> ''."""
    m = re.fullmatch(r"\(?\s*\(([^()]*)\)\s*->\s*Unit\s*\)?\??", typ.replace("@Composable ", ""))
    return m.group(1).strip() if m else None


def _kind(params: list[Param]) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """-> (kind, stateType, state param, note)."""
    for p in params:
        arg = _fn_arg(p.type)
        if arg and "@Composable" not in p.type and re.fullmatch(r"on\w*Change\w*", p.name):
            base = arg.split(":")[-1].strip().rstrip("?").split(".")[-1]
            state = next((q.name for q in params if q.name in ("value", "text", "checked", "selected", "isChecked",
                                                              "isSelected", "isOn", "state")), None)
            if base in ("String", "Boolean", "Int"):
                return "input", (None if base == "String" else base), state, None
            return None, None, state, f"`{p.name}` passes `{base}`; only String/Boolean/Int bindings are supported"
    if any(re.fullmatch(r"on[A-Z]\w*", p.name) and _fn_arg(p.type) is not None and "@Composable" not in p.type
           for p in params):
        return "action", None, None, None
    return None, None, None, None


def _title(name: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in words(name))


def _draft_props(params: list[Param], state_param: Optional[str], mod: Module) -> dict:
    props = {}
    for p in params:
        if p.name == state_param:
            continue
        base = p.type.rstrip("?").split(".")[-1]
        values = mod.enums.get(base)
        if values:
            props[p.name] = {"figmaProp": _title(p.name), "values": values}
        elif base == "String" and p.name.lower() in TEXT_PARAMS:
            props[p.name] = {"figmaText": _title(p.name)}
    return props


def _synth_example(name: str, params: list[Param], kind: Optional[str], state: Optional[str]) -> str:
    args = []
    for p in params:
        if p.default is not None:
            continue
        t = p.type.rstrip("?")
        if p.name == state:
            args.append(f"{p.name} = state.{camel(name)}")
        elif _fn_arg(p.type) is not None:
            args.append(f"{p.name} = {{ }}" if "@Composable" in p.type else f"{p.name} = {p.name}")
        elif t == "String":
            args.append(f'{p.name} = "{_title(p.name)}"')
        elif t == "Boolean":
            args.append(f"{p.name} = false")
        elif t in ("Int", "Float", "Double", "Long"):
            args.append(f"{p.name} = 0")
        else:
            args.append(f"{p.name} = /* {p.type} */")
    return f"{name}({', '.join(args)})"


def _common_prefix(names: list[str]) -> str:
    firsts = Counter(words(n)[0] for n in names if len(words(n)) > 1)
    if not firsts:
        return ""
    w, n = firsts.most_common(1)[0]
    return w if n >= 2 and n >= 0.4 * len(names) else ""


def _sees(f: KtFile, name: str, pkg: str) -> bool:
    """Does `f` resolve `name` to the UIKit's composable (same package or imported)?"""
    if f.package == pkg:
        return True
    return bool(re.search(rf"^\s*import\s+{re.escape(pkg)}\.(?:{re.escape(name)}|\*)\s*$", f.masked, re.M))


def _usages(app_files: list[KtFile], packages: dict[str, str]) -> dict[str, list]:
    """name -> [(KtFile, offset, call_text)] for call sites outside the module that import the composable."""
    names = set(packages)
    if not names:
        return {}
    rx = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, sorted(names))) + r")\s*\(")
    out: dict[str, list] = {}
    for f in app_files:
        for m in rx.finditer(f.masked):
            if re.search(r"\bfun\s+(?:[\w.]+\.)?$", f.masked[max(0, m.start() - 60):m.start()]):
                continue
            close = match_bracket(f.masked, m.end() - 1)
            text = f.code[m.start():close + 1]
            nxt = re.match(r"\s*\{", f.masked[close + 1:])
            if nxt:
                lam_open = close + 1 + nxt.end() - 1
                lam_close = match_bracket(f.masked, lam_open)
                lam = f.code[lam_open:lam_close + 1]
                text += " " + (lam if len(collapse(lam)) <= 80 else "{ ... }")
            if _sees(f, m.group(1), packages[m.group(1)]):
                out.setdefault(m.group(1), []).append((f, m.start(), text))
    return out


def collect_components(mod: Module, app_files: list[KtFile], figma: list[dict], rep: Report) -> list[dict]:
    found: dict[str, list] = {}
    for f in mod.files:
        for m in re.finditer(r"\bfun\s+(?:<[^>]*>\s*)?(?:([\w.]+)\.)?([A-Z]\w*)\s*\(", f.masked):
            prefix = f.prefix_before(m.start())
            if "@Composable" not in prefix or "@Preview" in prefix or is_hidden(prefix):
                continue
            name = m.group(2)
            if name.endswith("Preview") or name.endswith("Theme") or f.scope(m.start()):
                continue
            params, _ = _params(f, m.end() - 1)
            receiver = m.group(1)
            sig = f"{receiver + '.' if receiver else ''}{name}({', '.join(p.text for p in params)})"
            kdoc = f.kdoc_before(m.start())
            found.setdefault(name, []).append(dict(file=f, offset=m.start(), params=params, sig=sig, kdoc=kdoc,
                                                   deprecated="@Deprecated" in prefix))

    usages = _usages(app_files, {n: o[0]["file"].package for n, o in found.items()})
    prefix_word = _common_prefix(list(found))
    figma_names = {}
    for row in figma:
        n = row.get("componentSet") or row.get("name")
        if n:
            figma_names.setdefault(normalize_name(n), (n, None if row.get("componentSet") else row.get("key")))
    matched_figma = set()

    comps = []
    for name, overloads in sorted(found.items(), key=lambda kv: (-len(usages.get(kv[0], [])), kv[0])):
        main = next((o for o in overloads if not any("@Composable" in p.type for p in o["params"])), overloads[0])
        params = main["params"]
        kind, state_type, state, note = _kind(params)
        stripped = name[len(prefix_word):] if prefix_word and name.startswith(prefix_word) and \
            len(name) > len(prefix_word) else name
        figma_name, figma_key, figma_src = _title(stripped), None, "guessed"
        for cand in (normalize_name(stripped), normalize_name(name)):
            if cand in figma_names:
                (figma_name, figma_key), figma_src = figma_names[cand], "figma"
                matched_figma.add(cand)
                break
        calls = usages.get(name, [])
        if calls:
            f, off, text = min(calls, key=lambda c: ("=" not in c[2], len(c[2]) > 400, len(collapse(c[2]))))
            example, ex_src = collapse(text), f"usage ({len(calls)} call sites)"
        else:
            example, ex_src = _synth_example(name, params, kind, state), "synthesized"
        desc = re.sub(r"^/\*\*|\*/$", "", main["kdoc"])
        desc = re.sub(r"^[ \t]*\*[ \t]?", "", desc, flags=re.M).strip().split("\n\n")[0].split("\n@")[0]
        desc = collapse(re.sub(r"\[([\w.]+)\]", r"\1", desc))
        if main["deprecated"]:
            desc = ("Deprecated. " + desc).strip()
        sig = "\n".join(o["sig"] for o in overloads) if len(overloads) > 1 else main["sig"]
        entry = {"figma": figma_name}
        if figma_key:
            entry["figmaKey"] = figma_key
        if kind:
            entry["kind"] = kind
        if state_type:
            entry["stateType"] = state_type
        entry["description"] = desc
        entry["props"] = _draft_props(params, state, mod)
        entry["compose"] = {"import": f"{main['file'].package}.{name}" if main["file"].package else name,
                            "signature": sig, "example": example}
        comps.append(entry)
        if name in COMPOSE_BUILTINS:
            note = (note + "; " if note else "") + f"shadows Compose `{name}`; probably not a design component"
        rep.components.append(dict(name=name, figma=figma_name, figma_src=figma_src, kind=kind or "-",
                                   usages=len(calls), example=ex_src, props=len(entry["props"]),
                                   description=bool(desc), note=note, where=mod.where(main["file"], main["offset"])))
    rep.figma_unmatched = sorted(n for k, (n, _) in figma_names.items() if k not in matched_figma)
    return comps


DEFAULT_PRIMITIVES = {
    "Column": "Column(modifier, verticalArrangement = Arrangement.spacedBy(<gap token>), "
              "horizontalAlignment = Alignment.Start|CenterHorizontally|End)",
    "Row": "Row(modifier, horizontalArrangement = Arrangement.spacedBy(<gap token>) | Arrangement.End | "
           "Arrangement.SpaceBetween, verticalAlignment = Alignment.Top|CenterVertically|Bottom)",
    "Box": "Box(modifier)",
    "Spacer": "Spacer(Modifier.height(<size token>)) inside Column, Spacer(Modifier.width(<size token>)) "
              "inside Row, Spacer(Modifier.weight(1f)) for weight",
    "Text": "Text(text = \"...\", style = <typography token>, color = <color token>)",
    "padding": "Modifier.padding(horizontal = <token>, vertical = <token>)",
    "background": "Modifier.background(<color token>)",
    "fill": "maxSize -> Modifier.fillMaxSize(), maxWidth -> Modifier.fillMaxWidth()",
}


def text_primitive(comps: list[dict]) -> Optional[dict]:
    for c in comps:
        name = c["compose"]["import"].split(".")[-1]
        sig = c["compose"]["signature"].split("\n")[0]
        if name.endswith("Text") and re.search(r"\btext\s*:\s*String\b", sig) and re.search(r"\bstyle\s*:", sig):
            color = ", color = <color token>" if re.search(r"\bcolor\s*:", sig) else ""
            return {"Text": f'{name}(text = "...", style = <typography token>{color})',
                    "import": c["compose"]["import"]}
    return None
