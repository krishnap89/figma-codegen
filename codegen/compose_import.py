"""Draft manifest/components.json + manifest/tokens.json from an existing Jetpack Compose UIKit module.

    figma-codegen import-compose ../android-app/uikit --app ../android-app --figma-components figma.json

Tokens come from the Kotlin theme (so token paths match what the code can reference); components
from public @Composable functions. Everything that needs a human is listed in import-report.md.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .design_system import normalize_name
from .kotlin_source import (KtFile, collapse, expr_end, is_hidden, kotlin_files, match_bracket, split_args)
from .naming import camel, words

TOKEN_TYPES = {"Color": "color", "Dp": "dimension", "TextStyle": "typography", "Shape": "shape",
               "RoundedCornerShape": "shape", "CornerBasedShape": "shape"}

NAMED_COLORS = {"White": "FFFFFF", "Black": "000000", "Red": "FF0000", "Green": "00FF00", "Blue": "0000FF",
                "Yellow": "FFFF00", "Cyan": "00FFFF", "Magenta": "FF00FF", "Gray": "888888",
                "LightGray": "CCCCCC", "DarkGray": "444444", "Transparent": "00000000"}
FONT_WEIGHTS = {"Thin": 100, "ExtraLight": 200, "Light": 300, "Normal": 400, "Medium": 500, "SemiBold": 600,
                "Bold": 700, "ExtraBold": 800, "Black": 900}
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
TEXT_PARAMS = {"text", "label", "title", "subtitle", "placeholder", "hint", "error", "errortext",
               "helpertext", "supportingtext", "description", "caption", "message", "headline", "body"}


@dataclass
class Value:
    type: str               # color | dimension | typography | shape
    value: object


@dataclass
class Token:
    category: str
    name: str
    value: Value
    code: str
    imports: set
    where: str
    priority: int           # 0 theme instance, 1 object member, 2 top-level


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


# ---------------------------------------------------------------- source model

@dataclass
class Sym:
    file: KtFile
    text: str
    owner: tuple            # object path the symbol lives in
    where: str
    hidden: bool = False    # private/internal: resolvable, but never a token


@dataclass
class ClassInfo:
    name: str
    file: KtFile
    params: list            # [(name, type, default_text|None, is_property)]
    body: list              # [(name, type, init_text)]


class Module:
    def __init__(self, root: Path):
        self.root = root
        self.files = [KtFile.load(p) for p in kotlin_files(root)]
        self.symbols: dict[str, Sym] = {}
        self.classes: dict[str, ClassInfo] = {}
        self.enums: dict[str, list[str]] = {}
        self.objects: dict[str, tuple] = {}       # qualified object path -> (file, )
        for f in self.files:
            self._index(f)

    def where(self, f: KtFile, offset: int) -> str:
        try:
            rel = f.path.relative_to(self.root)
        except ValueError:
            rel = f.path
        return f"{rel}:{f.line(offset)}"

    def owner_path(self, f: KtFile, offset: int) -> Optional[list]:
        """Object path for a declaration at `offset`; None if inside a fun/lambda/class body."""
        path = []
        scope = f.scope(offset)
        for i, b in enumerate(scope):
            if b.kind == "object":
                if b.name == "Companion" and i > 0 and scope[i - 1].kind == "class":
                    continue
                if not b.name:
                    return None
                path.append(b.name)
            elif b.kind == "class" and i + 1 < len(scope) and scope[i + 1].kind == "object" \
                    and scope[i + 1].name == "Companion":
                path.append(b.name)
            else:
                return None
        return path

    def _index(self, f: KtFile) -> None:
        m = f.masked
        # classes with primary constructors
        for cm in re.finditer(r"\bclass\s+([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*"
                              r"(?:(?:public|private|internal|protected)\s+)?(?:@\w+\s+)*(?:constructor\s*)?\(", m):
            open_ = cm.end() - 1
            close = match_bracket(m, open_)
            params = []
            for piece, off in split_args(f.code[open_ + 1:close], m[open_ + 1:close]):
                pm = re.match(r"\s*(?:@[\w.]+(?:\([^)]*\))?\s*)*((?:\w+\s+)*?)(va[lr]\s+)?([A-Za-z_]\w*)\s*:\s*"
                              r"([^=]+?)\s*(?:=\s*(.*))?\s*$", piece, re.S)
                if not pm:
                    continue
                hidden = is_hidden(pm.group(1) or "")
                params.append((pm.group(3), pm.group(4).strip(), (pm.group(5) or "").strip() or None,
                               bool(pm.group(2)) and not hidden))
            self.classes[cm.group(1)] = ClassInfo(cm.group(1), f, params, [])
        # enums, sealed hierarchies
        for em in re.finditer(r"\benum\s+class\s+([A-Za-z_]\w*)[^{]*\{", m):
            close = match_bracket(m, em.end() - 1)
            body = m[em.end():close].split(";")[0]
            entries = [re.match(r"\s*(?:@\w+\s*)*([A-Za-z_]\w*)", p) for p, _ in split_args(body, body)]
            self.enums[em.group(1)] = [e.group(1) for e in entries if e]
        for sm in re.finditer(r"\bobject\s+([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*)", m):
            self.enums.setdefault(sm.group(2), []).append(sm.group(1))
        # vals
        for vm in re.finditer(r"(?<![\w.])va[lr]\s+([A-Za-z_]\w*)\s*(?::\s*([^=\n{]+?))?\s*(=|\bby\b)", m):
            name, typ, start = vm.group(1), (vm.group(2) or "").strip(), vm.end()
            end = expr_end(m, start)
            text = f.code[start:end].strip()
            prefix = f.prefix_before(vm.start())
            scope = f.scope(vm.start())
            if scope and scope[-1].kind == "class" and scope[-1].name in self.classes:
                if not is_hidden(prefix):
                    self.classes[scope[-1].name].body.append((name, typ, text))
                continue
            if scope and scope[-1].kind == "object" and scope[-1].name == "Companion" and len(scope) > 1 \
                    and scope[-2].kind == "class":
                self.enums.setdefault(scope[-2].name, []).append(name)   # companion "constants" of a style class
            path = self.owner_path(f, vm.start())
            if path is None or vm.group(3) != "=":
                continue
            q = ".".join(path + [name])
            sym = Sym(f, text, tuple(path), self.where(f, vm.start()), is_hidden(prefix))
            if q not in self.symbols or self.symbols[q].hidden:
                self.symbols[q] = sym

    def lookup(self, ref: str, owner: tuple = ()) -> Optional[Sym]:
        if ref in self.symbols:
            return self.symbols[ref]
        for i in range(len(owner), 0, -1):
            q = ".".join(owner[:i]) + "." + ref
            if q in self.symbols:
                return self.symbols[q]
        hits = [s for q, s in self.symbols.items() if q.endswith("." + ref)]
        if len(hits) > 1:
            hits = [s for s in hits if not s.hidden]
        return hits[0] if len(hits) == 1 else None

    def object_package(self, name: str) -> Optional[str]:
        for f in self.files:
            if re.search(rf"\b(object|class)\s+{re.escape(name)}\b", f.masked) or \
                    re.search(rf"(?<![\w.])va[lr]\s+{re.escape(name)}\b", f.masked):
                return f.package
        return None


# ---------------------------------------------------------------- evaluation

def _call(t: str) -> Optional[tuple[str, str]]:
    """'Foo.bar(a, b)' -> ('Foo.bar', 'a, b') when the whole text is one call."""
    if not t.endswith(")"):
        return None
    depth = 0
    for j in range(len(t) - 1, -1, -1):
        if t[j] == ")":
            depth += 1
        elif t[j] == "(":
            depth -= 1
            if depth == 0:
                head = t[:j].strip()
                return (head, t[j + 1:-1]) if re.fullmatch(r"[A-Za-z_][\w.]*|.+\.copy", head, re.S) else None
    return None


def _args(text: str) -> tuple[list, dict]:
    pos, named = [], {}
    for piece, _ in split_args(text, text):
        m = re.match(r"\s*([A-Za-z_]\w*)\s*=(?!=)\s*(.*)$", piece, re.S)
        if m:
            named[m.group(1)] = m.group(2).strip()
        else:
            pos.append(piece.strip())
    return pos, named


def _num(t: str) -> Optional[float]:
    m = re.fullmatch(r"\(?\s*(-?\d+(?:\.\d+)?)[fFdL]?\s*\)?", t.strip())
    return float(m.group(1)) if m else None


def _unit(t: str, unit: str) -> Optional[float]:
    m = re.fullmatch(rf"\(?\s*(-?\d+(?:\.\d+)?)[fF]?\s*\)?\s*\.{unit}", t.strip())
    return float(m.group(1)) if m else None


def _hex(argb: int) -> str:
    a, rgb = (argb >> 24) & 0xFF, argb & 0xFFFFFF
    return f"#{rgb:06X}" + ("" if a == 0xFF else f"{a:02X}")


def _px(v: float) -> str:
    return f"{v:g}px"


class Evaluator:
    def __init__(self, mod: Module):
        self.mod = mod

    def ev(self, text: str, owner: tuple = (), env: Optional[dict] = None, depth: int = 0) -> Optional[Value]:
        if depth > 12 or not text:
            return None
        t = text.strip().rstrip("!").strip()
        env = env or {}
        if t.startswith("(") and t.endswith(")") and len(t) > 1 and _call("x" + t) and _call("x" + t)[0] == "x":
            t = t[1:-1].strip()
        # literals
        dp = _unit(t, "dp")
        if dp is not None:
            return Value("dimension", dp)
        m = re.fullmatch(r"Color\.(\w+)", t)
        if m and m.group(1) in NAMED_COLORS:
            h = NAMED_COLORS[m.group(1)]
            return Value("color", "#" + h)
        m = re.fullmatch(r'"(#[0-9a-fA-F]{6,8})"\.toColorInt\(\)', t)
        if m:
            return self._color_from_css(m.group(1))
        if t in ("CircleShape", "RectangleShape"):
            return None
        call = _call(t)
        if call:
            return self._ev_call(call[0], call[1], owner, env, depth)
        # references
        if re.fullmatch(r"[A-Za-z_][\w.]*", t):
            if t in env:
                f_text, f_owner = env[t]
                return self.ev(f_text, f_owner, {}, depth + 1)
            sym = self.mod.lookup(t, owner)
            if sym:
                return self.ev(sym.text, sym.owner, {}, depth + 1)
        return None

    def _color_from_css(self, h: str) -> Value:
        h = h.lstrip("#").upper()
        return Value("color", "#" + (h[2:] + h[:2] if len(h) == 8 else h))  # #AARRGGBB -> #RRGGBBAA

    def _ev_call(self, head: str, inner: str, owner, env, depth) -> Optional[Value]:
        pos, named = _args(inner)
        if head.endswith(".copy"):
            base = self.ev(head[:-5], owner, env, depth + 1)
            if base is None:
                return None
            if base.type == "color" and "alpha" in named:
                a = _num(named["alpha"])
                if a is None:
                    return None
                return Value("color", base.value[:7] + ("" if a >= 1 else f"{round(a * 255):02X}"))
            if base.type == "typography":
                merged = dict(base.value)
                merged.update(self._text_style({}, named, owner, env, depth))
                return Value("typography", merged)
            return None
        if head in ("mutableStateOf", "derivedStateOf", "remember") and pos:
            return self.ev(pos[0], owner, env, depth + 1)
        if head == "Color":
            return self._color(pos, named, owner, env, depth)
        if head == "Dp" and pos:
            n = _num(pos[0])
            return Value("dimension", n) if n is not None else None
        if head == "TextStyle":
            return Value("typography", self._text_style(pos, named, owner, env, depth))
        if head == "TextStyle.Default.copy":
            return Value("typography", self._text_style([], named, owner, env, depth))
        if head == "RoundedCornerShape":
            arg = (pos or [named.get("size") or named.get("corner") or ""])[0]
            v = self.ev(arg, owner, env, depth + 1)
            return Value("shape", v.value) if v and v.type == "dimension" else None
        if head.endswith("parseColor") and pos:
            m = re.fullmatch(r'"(#[0-9a-fA-F]{6,8})"', pos[0].strip())
            return self._color_from_css(m.group(1)) if m else None
        return None

    def _color(self, pos, named, owner, env, depth) -> Optional[Value]:
        if len(pos) == 1 and not named:
            a = pos[0].strip()
            m = re.fullmatch(r"0[xX]([0-9a-fA-F]{1,8})[uL]*", a)
            if m:
                return Value("color", _hex(int(m.group(1), 16)))
            if re.fullmatch(r"-?\d+", a):
                return Value("color", _hex(int(a) & 0xFFFFFFFF))
            m = re.search(r'parseColor\(\s*"(#[0-9a-fA-F]{6,8})"\s*\)', a)
            if m:
                return self._color_from_css(m.group(1))
            return None
        vals = pos + [named[k] for k in ("red", "green", "blue", "alpha") if k in named]
        nums = [_num(v) for v in vals]
        if len(nums) not in (3, 4) or any(n is None for n in nums):
            return None
        is_float = any(re.search(r"\.\d|[fF]\b|[fF]$", v) for v in vals)
        chans = [round(n * 255) if is_float else int(n) for n in nums]
        if len(chans) == 3:
            chans.append(255)
        r, g, b, a = chans
        return Value("color", f"#{r:02X}{g:02X}{b:02X}" + ("" if a == 255 else f"{a:02X}"))

    def _text_style(self, pos, named, owner, env, depth) -> dict:
        out = {}
        if "fontFamily" in named:
            out["fontFamily"] = self._font_family(named["fontFamily"], owner)
        if "fontWeight" in named:
            w = self._font_weight(named["fontWeight"], owner, depth)
            if w:
                out["fontWeight"] = w
        for key in ("fontSize", "lineHeight", "letterSpacing"):
            if key in named:
                v = named[key].strip()
                sp = _unit(v, "sp")
                if sp is not None:
                    out[key] = _px(sp)
                elif _unit(v, "em") is not None:
                    out[key] = f"{_unit(v, 'em'):g}em"
                else:
                    sym = self.mod.lookup(v, owner) if re.fullmatch(r"[\w.]+", v) else None
                    sp = _unit(sym.text, "sp") if sym else None
                    if sp is not None:
                        out[key] = _px(sp)
        return out

    def _font_weight(self, t: str, owner, depth) -> Optional[int]:
        t = t.strip()
        m = re.fullmatch(r"FontWeight\.(\w+)", t)
        if m:
            name = m.group(1)
            if re.fullmatch(r"W\d00", name):
                return int(name[1:])
            return FONT_WEIGHTS.get(name)
        m = re.fullmatch(r"FontWeight\(\s*(\d+)\s*\)", t)
        if m:
            return int(m.group(1))
        sym = self.mod.lookup(t, owner) if re.fullmatch(r"[\w.]+", t) and depth < 12 else None
        return self._font_weight(sym.text, sym.owner, depth + 1) if sym else None

    def _font_family(self, t: str, owner) -> str:
        t = t.strip()
        m = re.fullmatch(r"FontFamily\.(\w+)", t)
        if m:
            return m.group(1)
        sym = self.mod.lookup(t, owner) if re.fullmatch(r"[\w.]+", t) else None
        src = sym.text if sym else t
        m = re.search(r"R\.font\.([a-z0-9]+)", src)
        if m:
            return m.group(1).capitalize()
        return t.split(".")[-1]


# ---------------------------------------------------------------- tokens

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
        return _px(value.value)
    return value.value


def _declared_token_type(typ: str) -> Optional[str]:
    base = re.sub(r"[?\s]", "", typ or "").split(".")[-1]
    return TOKEN_TYPES.get(base)


def _material_package(mod: Module) -> str:
    return "material3" if any("androidx.compose.material3" in f.masked for f in mod.files) else "material"


def _class_accessor(mod: Module, cls: str) -> Optional[tuple[str, set]]:
    """'AppColors' -> ('AppTheme.colors', {'com.x.AppTheme'})."""
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
        for other in sites[1:]:
            rep.skipped_themes.append((f"{cls}: {other[2] or '(anonymous)'} at {mod.where(other[0], other[1])}",
                                       f"used `{inst or '(anonymous)'}` instead"))
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
        pos, named = _args(args_text)
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

    # 2. object members and top-level vals
    for q, sym in mod.symbols.items():
        name = q.split(".")[-1]
        if sym.hidden or name in instance_names or name.startswith("Local"):
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


# ---------------------------------------------------------------- components

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


def _usages(app_files: list[KtFile], names: set[str]) -> dict[str, list]:
    """name -> [(KtFile, call_text)] for call sites outside the module."""
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

    usages = _usages(app_files, set(found))
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
        rep.components.append(dict(name=name, figma=figma_name, figma_src=figma_src, kind=kind or "-",
                                   usages=len(calls), example=ex_src, props=len(entry["props"]),
                                   description=bool(desc), note=note, where=mod.where(main["file"], main["offset"])))
    rep.figma_unmatched = sorted(n for k, (n, _) in figma_names.items() if k not in matched_figma)
    return comps


# ---------------------------------------------------------------- entry point

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


def _text_primitive(comps: list[dict]) -> Optional[dict]:
    for c in comps:
        name = c["compose"]["import"].split(".")[-1]
        sig = c["compose"]["signature"].split("\n")[0]
        if name.endswith("Text") and re.search(r"\btext\s*:\s*String\b", sig) and re.search(r"\bstyle\s*:", sig):
            color = ", color = <color token>" if re.search(r"\bcolor\s*:", sig) else ""
            return {"Text": f'{name}(text = "...", style = <typography token>{color})',
                    "import": c["compose"]["import"]}
    return None


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
    text = _text_primitive(comps)
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
