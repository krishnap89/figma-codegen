"""Collect design tokens from the Kotlin theme and lay them out as tokens.json + tokenCode/categoryCode."""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from ..kotlin_source import KtFile, collapse, expr_end, is_hidden, match_bracket
from ..naming import camel
from .evaluator import Evaluator, Value, call_args, px
from .report import Report
from .source import Module

TOKEN_TYPES = {"Color": "color", "Dp": "dimension", "TextStyle": "typography", "Shape": "shape",
               "RoundedCornerShape": "shape", "CornerBasedShape": "shape"}


MATERIAL = {  # builder -> (accessor, material package)
    "lightColorScheme": ("MaterialTheme.colorScheme", "material3"),
    "darkColorScheme": ("MaterialTheme.colorScheme", "material3"),
    "lightColors": ("MaterialTheme.colors", "material"),
    "darkColors": ("MaterialTheme.colors", "material"),
    "Typography": ("MaterialTheme.typography", None),
    "Shapes": ("MaterialTheme.shapes", None),
}
DARK = re.compile(r"dark|night", re.I)
LIGHT = re.compile(r"light|day|default", re.I)
RADIUS = re.compile(r"radius|radii|corner|round", re.I)
BORDER = re.compile(r"border|stroke|outline|divider", re.I)
SPACING = re.compile(r"spac|padding|gap|margin|inset|gutter|space", re.I)
SIZE = re.compile(r"size|height|width|icon|elevation|avatar", re.I)


THEMEISH = re.compile(r"colou?r|typograph|spacing|dimen|shape|radius|palette|theme|font", re.I)


@dataclass
class Token:
    category: str
    name: str
    value: Value
    code: str
    imports: set
    where: str
    priority: int           # 0 theme instance, 1 object member, 2 top-level


def _category(value: Value, prop: str, container: str) -> str:
    if value.type in ("color", "typography", "shape"):
        return value.type
    for rx, cat in ((RADIUS, "radius"), (BORDER, "border"), (SPACING, "spacing"), (SIZE, "size")):
        if rx.search(prop):
            return cat
    for rx, cat in ((RADIUS, "radius"), (BORDER, "border"), (SPACING, "spacing")):
        if rx.search(container):
            return cat
    return "size"


def _token_type(value: Value) -> str:
    return {"color": "color", "dimension": "dimension", "typography": "typography", "shape": "dimension"}[value.type]


def _token_value(value: Value):
    if value.type in ("dimension", "shape"):
        return px(value.value)
    return value.value


def _declared_token_type(typ: str) -> Optional[str]:
    base = re.sub(r"[?\s]", "", typ or "").split(".")[-1]
    return TOKEN_TYPES.get(base)


def _material_package(mod: Module) -> str:
    return "material3" if any("androidx.compose.material3" in f.masked for f in mod.files) else "material"


def _class_accessor(mod: Module, cls: str) -> Optional[tuple[str, set]]:
    """'AppColors' -> ('AppTheme.colors', {'com.x.AppTheme'})."""
    for f in mod.files:
        for m in re.finditer(rf"(?<![\w.])val\s+([A-Z]\w*)\.([A-Za-z_]\w*)\s*:\s*{re.escape(cls)}\b", f.masked):
            if is_hidden(f.prefix_before(m.start())) or f.scope(m.start()):
                continue
            recv, prop = m.group(1), m.group(2)
            imports = {f"{f.package}.{prop}" if f.package else prop}
            if recv == "MaterialTheme":
                imports.add(f"androidx.compose.{_material_package(mod)}.MaterialTheme")
            elif mod.object_package(recv):
                imports.add(f"{mod.object_package(recv)}.{recv}")
            return f"{recv}.{prop}", imports
    best = None
    for f in mod.files:
        for m in re.finditer(rf"(?<![\w.])va[lr]\s+([A-Za-z_]\w*)\s*:\s*{re.escape(cls)}\b(?!\s*\()", f.masked):
            if is_hidden(f.prefix_before(m.start())):
                continue
            path = mod.owner_path(f, m.start())
            if not path:
                continue
            code = ".".join(path + [m.group(1)])
            score = 0 if re.search(r"theme", path[0], re.I) else 1
            if best is None or score < best[0]:
                best = (score, code, {f"{f.package}.{path[0]}" if f.package else path[0]})
    if best:
        return best[1], best[2]
    for f in mod.files:
        for m in re.finditer(r"(?<![\w.])val\s+(Local\w+)\s*(?::[^=]+)?=\s*(?:static)?[cC]ompositionLocalOf\s*"
                             r"(?:<\s*([\w.]+)\s*>)?", f.masked):
            body = f.masked[m.end():expr_end(f.masked, m.end())]
            if m.group(2) == cls or re.search(rf"\b{re.escape(cls)}\s*\(", body):
                return f"{m.group(1)}.current", {f"{f.package}.{m.group(1)}" if f.package else m.group(1)}
    return None


_THEME_DIR = re.compile(r"^(theme|themes|tokens?|designsystem|design|styles?|foundation)$", re.I)


def _is_theme_file(mod: Module, f: KtFile) -> bool:
    try:
        dirs = f.path.relative_to(mod.root).parts[:-1]
    except ValueError:
        dirs = ()
    return any(_THEME_DIR.match(p) for p in list(dirs) + f.package.split("."))


def collect_tokens(mod: Module, ev: Evaluator, rep: Report) -> list[Token]:
    tokens: list[Token] = []
    mat_pkg = _material_package(mod)
    instance_names: set[str] = set()

    # 1. instances of theme classes (custom data classes and Material builders)
    builders = {c for c in mod.classes} | {b for b in MATERIAL if b not in mod.classes}
    calls: dict[str, list] = {}
    for f in mod.files:
        for m in re.finditer(r"(?<![\w.])(" + "|".join(map(re.escape, sorted(builders))) + r")\s*\(", f.masked):
            before = f.masked[max(0, m.start() - 40):m.start()]
            if re.search(r"\b(class|fun)\s+$", before):
                continue
            open_ = m.end() - 1
            close = match_bracket(f.masked, open_)
            line_start = f.masked.rfind("\n", 0, m.start()) + 1
            nm = re.search(r"\b(?:va[lr]|fun)\s+([A-Za-z_]\w*)", f.masked[line_start:m.start()])
            inst = nm.group(1) if nm else ""
            if nm:
                instance_names.add(inst)
            calls.setdefault(m.group(1), []).append((f, m.start(), inst, f.code[open_ + 1:close]))

    for cls, sites in calls.items():
        def rank(site):
            f, off, inst, args = site
            return (bool(DARK.search(inst)), not LIGHT.search(inst), -len(args))
        sites.sort(key=rank)
        f, off, inst, args_text = sites[0]
        if cls in mod.classes:
            info = mod.classes[cls]
            acc = _class_accessor(mod, cls)
            if acc is None:  # component colors/defaults classes etc.; only theme-looking ones are worth a note
                if THEMEISH.search(cls) and any(_declared_token_type(t) for _, t, _, _ in info.params):
                    rep.no_accessor.append(cls)
                continue
        else:
            accessor, pkg = MATERIAL[cls]
            pkg = pkg or mat_pkg
            acc = (accessor, {f"androidx.compose.{pkg}.MaterialTheme"})
            info = None
        for other in sites[1:]:
            if other[2] and other[2] != inst and collapse(other[3]) != collapse(args_text):
                rep.skipped_themes.append((f"{cls}: `{other[2]}` at {mod.where(other[0], other[1])}",
                                           f"used `{inst or '(anonymous)'}` instead"))
        pos, named = call_args(args_text)
        where = mod.where(f, off)
        owner = tuple(mod.owner_path(f, off) or ())
        if info is None:
            props = [(k, "", v) for k, v in named.items() if k != "defaultFontFamily"]
            env = {}
        else:
            env = {}
            for i, (pname, ptype, default, _) in enumerate(info.params):
                if pname in named:
                    env[pname] = (named[pname], owner)
                elif i < len(pos):
                    env[pname] = (pos[i], owner)
                elif default is not None:
                    env[pname] = (default, ())
            props = [(p, t, None) for p, t, _, is_prop in info.params if is_prop] + list(info.body)
        for pname, ptype, text in props:
            expr = text if text is not None else (env.get(pname) or ("", ()))[0]
            val = ev.ev(expr, owner, env)
            if val is None:
                if _declared_token_type(ptype) or info is None:
                    rep.unparsed.append((where, f"{cls}.{pname}", collapse(expr)[:80] or "(no value)"))
                continue
            cat = _category(val, pname, cls)
            tokens.append(Token(cat, pname, val, f"{acc[0]}.{pname}", set(acc[1]), where, 0))

    # 2. object members and top-level vals. Component files hold component-local values
    #    (ButtonDefaults.Height, a card's text color), so prefer the theme package(s) when there are any.
    theme_files = {f.path for f in mod.files if _is_theme_file(mod, f)}
    if theme_files:
        rep.notes.append(f"Palette / object tokens are read only from {len(theme_files)} theme file(s) "
                         f"(package or folder named theme/tokens/design/style).")
    for q, sym in mod.symbols.items():
        name = q.split(".")[-1]
        if sym.hidden or name in instance_names or name.startswith("Local"):
            continue
        if (theme_files and sym.file.path not in theme_files) or any(o.endswith("Defaults") for o in sym.owner):
            continue
        val = ev.ev(sym.text, sym.owner)
        if val is None:
            decl = re.search(rf"\bva[lr]\s+{re.escape(name)}\s*:\s*([\w.?]+)", sym.file.masked)
            if decl and _declared_token_type(decl.group(1)):
                rep.unparsed.append((sym.where, q, collapse(sym.text)[:80]))
            continue
        if any(DARK.search(p) for p in sym.owner) or DARK.search(name) and sym.owner:
            rep.skipped_themes.append((q, "dark-theme value"))
            continue
        imp = f"{sym.file.package}.{(sym.owner or (name,))[0]}" if sym.file.package else ""
        tokens.append(Token(_category(val, name, ".".join(sym.owner)), name, val, q, {imp} if imp else set(),
                            sym.where, 1 if sym.owner else 2))
    return tokens


def build_tokens(tokens: list[Token], rep: Report) -> tuple[dict, dict, str, set]:
    """-> (tokens.json, categoryCode, tokenCode template, imports)."""
    tokens = sorted(tokens, key=lambda t: t.priority)  # stable: theme > objects > top-level
    by_cat: dict[str, dict] = {}
    chosen: list[tuple[str, str, Token]] = []
    for t in tokens:
        cat = by_cat.setdefault(t.category, {})
        key = t.name
        if key in cat:
            if cat[key].code == t.code:
                continue
            container = t.code[:-len(t.name) - 1].split(".")[-1] if t.code.endswith("." + t.name) else "top"
            key = camel(container) + t.name[:1].upper() + t.name[1:]
            if key in cat:
                continue
        cat[key] = t
        chosen.append((t.category, key, t))

    prefixes: dict[str, str] = {}
    for c, toks in by_cat.items():
        cnt = Counter(t.code[:-len(t.name) - 1] if t.code.endswith("." + t.name) else "" for t in toks.values())
        prefixes[c] = cnt.most_common(1)[0][0]
    heads = {p.split(".", 1)[0] for p in prefixes.values() if "." in p}
    if all("." in p for p in prefixes.values()) and len(heads) == 1:
        head = heads.pop()
        template = head + ".{category}.{name}"
        category_code = {c: p.split(".", 1)[1] for c, p in prefixes.items()}
    else:
        template = "{category}.{name}"
        category_code = dict(prefixes)

    out: dict = {}
    imports: set = set()
    overrides = 0
    for c, key, t in chosen:
        cc = category_code.get(c, c)
        tpl = template.replace("{category}.", "") if cc == "" else template
        entry = {"$type": _token_type(t.value), "$value": _token_value(t.value)}
        if tpl.format(category=cc, name=camel(key)) != t.code:
            entry["$extensions"] = {"figma-codegen": {"compose": t.code}}
            overrides += 1
        out.setdefault(c, {})[key] = entry
        imports |= t.imports
    if overrides:
        rep.notes.append(f"{overrides} token(s) carry their exact Kotlin expression in "
                         f"`$extensions.figma-codegen.compose` because `tokenCode` can't produce it.")
    for c in by_cat:
        values = Counter(json.dumps(_token_value(t.value)) for t in by_cat[c].values())
        dups = [v for v, n in values.items() if n > 1]
        if dups and c != "typography":
            rep.notes.append(f"`{c}`: {len(dups)} value(s) are shared by several tokens (e.g. {dups[0]}); "
                             f"when matching by value the first token in tokens.json wins — order them so "
                             f"the semantic name comes first.")
    if "size" in by_cat:
        rep.notes.append(f"`size`: {len(by_cat['size'])} Dp token(s) could not be classified as spacing / radius / "
                         f"border and are not used for matching. Move real spacings into `spacing`.")
    if "shape" in by_cat and "radius" not in by_cat:
        rep.notes.append("Corner radii exist only as Shapes (`shape` category); the generator matches "
                         "`cornerRadius` against `radius` Dp tokens. Add Dp radius tokens, or the model will "
                         "write raw `RoundedCornerShape(N.dp)` that the lint rejects.")
    return out, category_code, template, imports
