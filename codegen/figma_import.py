"""Draft or update manifest/components.json from Figma component JSON, instead of copying by hand.

    figma-codegen import-figma banner.json button.json            # saved /v1/files/:key/nodes responses
    figma-codegen import-figma "https://www.figma.com/design/KEY/Lib?node-id=417-2507"

What Figma knows goes into the manifest: the exact component (set) name, its key, and every
component property (variants with their values, text, boolean and instance-swap properties) as
`props` with the exact `figmaProp` names that `simplify` reads. What only the code knows (import,
signature, example) is kept from the base manifest; components the base does not have get a draft
marked with `todo`, and everything that needs a human is listed in figma-import-report.md.

Accepted input, per file: a component set node (all variants and `componentPropertyDefinitions`),
a single variant or component, or any frame/page. Component definitions are read from COMPONENT_SET /
COMPONENT nodes; instances anywhere add the components they use (only the values seen there).
Icons (components that are only vector artwork) and private components ('.Name', '_Name') are skipped.

The naming conventions this relies on are `figma_import` in codegen.yaml (config.FigmaImportRules);
`check()` reports where a library does not follow them, for designers (`import-figma --check`).
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .config import FigmaImportRules
from .design_system import normalize_name
from .naming import camel, pascal, words

SHAPE_TYPES = {"VECTOR", "RECTANGLE", "ELLIPSE", "BOOLEAN_OPERATION", "LINE", "STAR", "POLYGON", "REGULAR_POLYGON"}
PROP_TYPES = ("VARIANT", "TEXT", "INSTANCE_SWAP", "BOOLEAN")      # also the order props are written in
REF_TYPES = {"visible": "BOOLEAN", "characters": "TEXT", "mainComponent": "INSTANCE_SWAP"}
BOOL_VALUES = {"true": True, "false": False, "yes": True, "no": False, "on": True, "off": False}
PRESS_TRIGGERS = {"ON_CLICK", "ON_PRESS", "ON_TAP"}
TODO = "drafted by import-figma from Figma: check import, signature and example against the real code"


def name_has(name: str, phrases: list[str]) -> bool:
    """Does a component name contain one of the phrases as whole words? 'IconButton' has 'Button',
    'Check Box' and 'Checkbox' both have 'Checkbox'; 'Table' does not have 'Tab'."""
    ws = [w.lower() for w in words(name)]
    runs = {"".join(ws[i:j]) for i in range(len(ws)) for j in range(i + 1, len(ws) + 1)}
    return any("".join(w.lower() for w in words(ph)) in runs for ph in phrases if words(ph))


class Rules:
    """FigmaImportRules prepared for lookups."""

    def __init__(self, raw: Optional[FigmaImportRules] = None):
        self.raw = raw or FigmaImportRules()
        r = self.raw
        self.interaction = {v.lower() for v in r.interaction_states}
        self.disabled = {v.lower() for v in r.disabled_values}
        self.state_props = {normalize_name(v) for v in r.state_props}
        self.aliases = {normalize_name(k): v for k, v in r.prop_aliases.items()}

    def private(self, name: str) -> bool:
        return name.startswith(tuple(self.raw.private_prefixes)) if self.raw.private_prefixes else False

    def visibility(self, short: str) -> bool:
        return any(short.strip().startswith(px) for px in self.raw.visibility_prefixes if px)

    def strip_visibility(self, short: str) -> str:
        s = short.strip()
        for px in self.raw.visibility_prefixes:
            if px and s.startswith(px) and not px[-1:].isalpha():   # '👁 Title' -> 'Title'; 'Show Title' stays
                return s[len(px):]
        return s

    def alias(self, short: str) -> Optional[str]:
        return self.aliases.get(normalize_name(short))

    def check_on(self, name: str) -> bool:
        return self.raw.checks.get(name, True)


DEFAULT_RULES = Rules()


# ---------------------------------------------------------------- reading Figma JSON

@dataclass
class FigmaProp:
    key: str                      # exact property key: 'State', '✏️ Title#581:0'
    type: str                     # VARIANT | TEXT | BOOLEAN | INSTANCE_SWAP
    values: list = field(default_factory=list)    # VARIANT options, in Figma order
    default: Any = None
    complete: bool = False        # values come from componentPropertyDefinitions (all options known)
    layer: Optional[str] = None   # TEXT: name of the text layer it drives

    @property
    def short(self) -> str:
        return self.key.split("#")[0]

    def add_value(self, v) -> None:
        if v is not None and v not in self.values:
            self.values.append(v)


@dataclass
class FigmaComponent:
    name: str
    key: Optional[str]
    description: str = ""
    props: dict = field(default_factory=dict)          # key -> FigmaProp
    defined: bool = False                               # seen as COMPONENT / COMPONENT_SET, not only as instance
    pressable: bool = False                             # an ON_CLICK/ON_PRESS interaction on the component
    text_layers: set = field(default_factory=set)
    nested: set = field(default_factory=set)            # names of components used inside it
    sources: set = field(default_factory=set)
    artwork: bool = True                                # every variant/instance seen is only vector artwork
    node_id: Optional[str] = None                       # the component (set) node, when it was in the input
    loose_text: set = field(default_factory=set)        # text layers no text property drives

    def prop(self, key: str, type_: str) -> FigmaProp:
        p = self.props.get(key)
        if p is None:
            p = self.props[key] = FigmaProp(key, type_)
        return p


def load_entries(path: Path) -> list[dict]:
    """Every node entry ({document, components, componentSets, ...}) of a saved /v1/files/:key/nodes response."""
    data = json.loads(path.read_text())
    if isinstance(data, dict) and isinstance(data.get("nodes"), dict):
        return [e for e in data["nodes"].values() if isinstance(e, dict) and e.get("document")]
    if isinstance(data, dict) and data.get("document"):
        return [data]
    raise ValueError(f"{path}: not a Figma /v1/files/:key/nodes response")


def _is_artwork(n: dict) -> bool:
    """Only vector shapes inside (any visibility): an icon, logo or illustration."""
    if n.get("type") in SHAPE_TYPES:
        return True
    kids = n.get("children") or []
    return n.get("type") in {"FRAME", "GROUP", "COMPONENT", "INSTANCE"} and bool(kids) and all(map(_is_artwork, kids))


def _variant_values(name: str) -> dict[str, str]:
    """'Type=Tertiary, State=Normal' -> {'Type': 'Tertiary', 'State': 'Normal'}."""
    out = {}
    for part in (name or "").split(","):
        k, sep, v = part.partition("=")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out


class Collector:
    """Merges the components found in any number of node entries, keyed by component (set) key or name."""

    def __init__(self):
        self.components: dict[str, FigmaComponent] = {}

    def _get(self, name: str, key: Optional[str], description: str, source: str) -> FigmaComponent:
        ident = key or "name:" + normalize_name(name)
        c = self.components.get(ident)
        if c is None:
            c = self.components[ident] = FigmaComponent(name=name, key=key)
        c.description = c.description or (description or "").strip()
        c.sources.add(source)
        return c

    def add(self, entry: dict, source: str) -> None:
        self._components = entry.get("components") or {}
        self._sets = entry.get("componentSets") or {}
        self._source = source
        self._walk(entry["document"], parent_type=None)

    # -- which component does a node belong to
    def _set_of(self, component_id: Optional[str]) -> tuple[dict, dict]:
        meta = self._components.get(component_id or "") or {}
        return meta, self._sets.get(meta.get("componentSetId") or "") or {}

    def _walk(self, n: dict, parent_type: Optional[str]) -> None:
        t = n.get("type")
        if t == "COMPONENT_SET":
            meta = self._sets.get(n["id"]) or {}
            c = self._get(meta.get("name") or n.get("name", "?"), meta.get("key"),
                          meta.get("description") or n.get("description", ""), self._source)
            c.defined = True
            c.node_id = c.node_id or n.get("id")
            self._definitions(c, n.get("componentPropertyDefinitions") or {})
            variants = [v for v in n.get("children") or [] if v.get("type") == "COMPONENT"]
            c.artwork = c.artwork and bool(variants) and all(map(_is_artwork, variants))
            for v in variants:
                self._variant(c, v)
            return
        if t == "COMPONENT":
            meta, set_meta = self._set_of(n["id"])
            if set_meta:   # one variant, fetched on its own: the set's other props come from its layers
                c = self._get(set_meta["name"], set_meta.get("key"), set_meta.get("description", ""), self._source)
            else:
                c = self._get(meta.get("name") or n.get("name", "?"), meta.get("key"),
                              meta.get("description") or n.get("description", ""), self._source)
            c.defined = True
            c.node_id = c.node_id or (meta.get("componentSetId") if set_meta else None) or n.get("id")
            c.artwork = c.artwork and _is_artwork(n)
            self._definitions(c, n.get("componentPropertyDefinitions") or {})
            self._variant(c, n, variant_name=bool(set_meta))
            return
        if t == "INSTANCE":
            self._instance(n)
        for child in n.get("children") or []:
            self._walk(child, t)

    def _definitions(self, c: FigmaComponent, defs: dict) -> None:
        for key, d in defs.items():
            if d.get("type") not in PROP_TYPES:
                continue
            p = c.prop(key, d["type"])
            p.default = d.get("defaultValue", p.default)
            if d["type"] == "VARIANT":
                p.values = list(d.get("variantOptions") or p.values)
                p.complete = True
            elif d["type"] == "INSTANCE_SWAP":
                p.default = (self._components.get(p.default) or {}).get("name", p.default)

    def _variant(self, c: FigmaComponent, v: dict, variant_name: bool = True) -> None:
        if variant_name:
            for k, val in _variant_values(v.get("name", "")).items():
                p = c.prop(k, "VARIANT")
                p.add_value(val)
                if p.default is None:
                    p.default = val
        if any(((i.get("trigger") or {}).get("type") in PRESS_TRIGGERS) for i in v.get("interactions") or []):
            c.pressable = True
        self._layers(c, v, top=True)

    def _layers(self, c: FigmaComponent, n: dict, top: bool = False) -> None:
        """Property references and text layers of the component's own layers. An instance's insides
        belong to the nested component, so only the instance's own references count."""
        if not top:
            for field_, key in (n.get("componentPropertyReferences") or {}).items():
                type_ = REF_TYPES.get(field_)
                if not type_:
                    continue
                p = c.prop(key, type_)
                if type_ == "TEXT":
                    p.layer = p.layer or n.get("name")
                    if p.default is None:
                        p.default = n.get("characters")
                elif type_ == "BOOLEAN" and p.default is None:
                    p.default = n.get("visible", True) is not False
                elif type_ == "INSTANCE_SWAP" and p.default is None:
                    p.default = (self._components.get(n.get("componentId")) or {}).get("name")
            if n.get("type") == "TEXT" and n.get("name"):
                c.text_layers.add(n["name"])
                if "characters" not in (n.get("componentPropertyReferences") or {}):
                    c.loose_text.add(n["name"])
        if n.get("type") == "INSTANCE" and not top:
            meta, set_meta = self._set_of(n.get("componentId"))
            c.nested.add(set_meta.get("name") or meta.get("name") or n.get("name", "?"))
            self._instance(n)
            for child in n.get("children") or []:
                self._walk_instance_insides(child)
            return
        for child in n.get("children") or []:
            self._layers(c, child)

    def _walk_instance_insides(self, n: dict) -> None:
        if n.get("type") == "INSTANCE":
            self._instance(n)
        for child in n.get("children") or []:
            self._walk_instance_insides(child)

    def _instance(self, n: dict) -> None:
        meta, set_meta = self._set_of(n.get("componentId"))
        name = set_meta.get("name") or meta.get("name")
        if not name:
            return
        c = self._get(name, set_meta.get("key") or meta.get("key"),
                      set_meta.get("description") or meta.get("description", ""), self._source)
        c.artwork = c.artwork and _is_artwork(n)
        for key, p in (n.get("componentProperties") or {}).items():
            if p.get("type") not in PROP_TYPES:
                continue
            fp = c.prop(key, p["type"])
            if p["type"] == "VARIANT" and not fp.complete:
                fp.add_value(p.get("value"))
        if any(((i.get("trigger") or {}).get("type") in PRESS_TRIGGERS) for i in n.get("interactions") or []):
            c.pressable = True
        # Text layers of the instance: the nearest ones are the component's own.
        stack = list(n.get("children") or [])
        while stack:
            k = stack.pop()
            if k.get("type") == "TEXT" and k.get("name"):
                c.text_layers.add(k["name"])
                ref = (k.get("componentPropertyReferences") or {}).get("characters")
                if ref in c.props and not c.props[ref].layer:
                    c.props[ref].layer = k["name"]
            if k.get("type") != "INSTANCE":
                stack.extend(k.get("children") or [])


# ---------------------------------------------------------------- drafting manifest entries

def infer_kind(c: FigmaComponent, rules: Rules = DEFAULT_RULES) -> tuple[Optional[str], Optional[str]]:
    """-> (kind, stateType) from the component name and its interactions."""
    if name_has(c.name, rules.raw.toggle_names):
        return "input", "Boolean"
    if name_has(c.name, rules.raw.input_names):
        return "input", None
    if name_has(c.name, rules.raw.action_names) or c.pressable:
        return "action", None
    return None, None


BOOL_PAIRS = {"true": "false", "yes": "no", "on": "off"}


def _bool_map(p: FigmaProp) -> Optional[dict]:
    """'👁 Icon' with values ['False'] -> {'False': False, 'True': True} (both, even if only one was seen)."""
    if not (p.type == "VARIANT" and p.values and all(str(v).lower() in BOOL_VALUES for v in p.values)):
        return None
    out = {v: BOOL_VALUES[str(v).lower()] for v in p.values}
    for v in list(p.values):
        low = str(v).lower()
        other = BOOL_PAIRS.get(low) or next((t for t, f in BOOL_PAIRS.items() if f == low), None)
        cased = other.title() if str(v)[:1].isupper() else other
        if str(v).isupper():
            cased = other.upper()
        out.setdefault(cased, BOOL_VALUES[other])
    return out


def _enabled_map(p: FigmaProp, rules: Rules = DEFAULT_RULES) -> Optional[dict]:
    low = [str(v).lower() for v in p.values]
    if p.type == "VARIANT" and low and all(v in rules.interaction for v in low) and rules.disabled & set(low):
        return {v: str(v).lower() not in rules.disabled for v in p.values}
    return None


def prop_name(component: str, p: FigmaProp, rules: Rules = DEFAULT_RULES) -> str:
    """'✏️ Button Text' on Button -> 'text'; '👁 2nd Button' -> 'show2ndButton'; 'State' with Disabled -> 'enabled'.
    figma_import.prop_aliases wins: {Type: style} names Figma's 'Type' `style`."""
    alias = rules.alias(p.short)
    if alias:
        return alias
    if _enabled_map(p, rules):
        return "enabled"
    ws = words(rules.strip_visibility(p.short))
    comp = [w.lower() for w in words(component)]
    if len(ws) > len(comp) and [w.lower() for w in ws[:len(comp)]] == comp:
        ws = ws[len(comp):]
    if not ws:
        ws = ["value"]
    is_bool = p.type == "BOOLEAN" or _bool_map(p) is not None
    if is_bool and ws[0].lower() not in ("show", "has", "is", "with", "hide") and \
            (rules.visibility(p.short) or p.type == "VARIANT"):
        ws = ["show"] + ws
    name = "".join(w[:1].upper() + w[1:] for w in ws)
    return name[:1].lower() + name[1:]


def prop_spec(p: FigmaProp, figma_prop: str, rules: Rules = DEFAULT_RULES) -> dict:
    spec: dict = {"figmaProp": figma_prop}
    m = _enabled_map(p, rules) or _bool_map(p)
    if m:
        spec["map"] = m
    elif p.type == "VARIANT":
        spec["values"] = list(p.values)
    return spec


def figma_prop_names(c: FigmaComponent) -> dict[str, str]:
    """key -> what `figmaProp` should say: the short name, or the exact key when two props share it."""
    shorts = Counter(p.short for p in c.props.values())
    out = {}
    for key, p in c.props.items():
        out[key] = key if shorts[p.short] > 1 and p.type != "VARIANT" else p.short
    return out


def ordered_props(c: FigmaComponent) -> list[FigmaProp]:
    return sorted(c.props.values(), key=lambda p: PROP_TYPES.index(p.type))


@dataclass
class CodeStyle:
    """How the base manifest names code, per platform: Figma 'Button' -> UiButton (compose), UIKitButton (swiftui)."""
    prefix: str
    module: str     # compose: package to put drafted imports in; swiftui: the module to import

    @classmethod
    def of(cls, base_components: list[dict], platform: str) -> "CodeStyle":
        seen, modules = Counter(), Counter()
        for e in base_components:
            code = e.get(platform) or {}
            name = re.split(r"[\s(<]", code.get("signature") or code.get("example") or "")[0]
            if platform == "compose":
                name = (code.get("import") or "").split(".")[-1] or name
                if (code.get("import") or "").count(".") >= 2:
                    modules[code["import"].rsplit(".", 2)[0]] += 1
            elif code.get("import"):
                modules[code["import"]] += 1
            p = pascal(e.get("figma", ""), fallback="")
            if p and name.endswith(p):
                seen[name[: -len(p)]] += 1
        prefix, n = seen.most_common(1)[0] if seen else ("", 0)
        default = "com.company.uikit" if platform == "compose" else "CompanyUIKit"
        return cls(prefix if n >= 2 else "", modules.most_common(1)[0][0] if modules else default)


def _enum_case(v) -> str:
    return pascal(str(v), fallback="Default")


def draft_code(c: FigmaComponent, entry: dict, platform: str, style: CodeStyle) -> dict:
    """A placeholder import/signature/example in the shape the rest of the manifest uses."""
    fn = style.prefix + pascal(c.name, fallback="Component")
    kind, state_type = entry.get("kind"), entry.get("stateType") or "String"
    figma_by_name = {p.short: p for p in c.props.values()} | {p.key: p for p in c.props.values()}
    params, args = [], []
    compose = platform == "compose"
    for name, spec in (entry.get("props") or {}).items():
        p = figma_by_name.get(spec.get("figmaProp", ""))
        if p is None:   # figmaText props: a String
            params.append(f"{name}: String")
            args.append(f'{name} = "{name.title()}"' if compose else f'{name}: "{name.title()}"')
            continue
        if "map" in spec:
            default = p.default if p.default in spec["map"] else next(iter(spec["map"]))
            d = str(spec["map"].get(default, True)).lower()
            params.append(f"{name}: {'Boolean' if compose else 'Bool'} = {d}")
        elif p.type == "VARIANT":
            enum = f"{fn}{pascal(name)}"
            default = p.default if p.default is not None else (p.values[0] if p.values else "Default")
            params.append(f"{name}: {enum} = {enum}.{_enum_case(default)}" if compose
                          else f"{name}: {enum} = .{camel(str(default))}")
        elif p.type == "TEXT":
            params.append(f"{name}: String")
            args.append(f'{name} = "{p.default or name}"' if compose else f'{name}: "{p.default or name}"')
        elif p.type == "BOOLEAN":
            d = str(p.default if isinstance(p.default, bool) else True).lower()
            params.append(f"{name}: {'Boolean' if compose else 'Bool'} = {d}")
        else:   # INSTANCE_SWAP: the swapped component's name reaches the spec; the code needs an icon
            params.append(f"{name}: ImageVector? = null" if compose else f"{name}: Image? = nil")
    var = camel(c.name)
    if compose:
        if kind == "input":
            t = "Boolean" if state_type == "Boolean" else state_type
            value, change = ("checked", "onCheckedChange") if t == "Boolean" else ("value", "onValueChange")
            params[:0] = [f"{value}: {t}", f"{change}: ({t}) -> Unit"]
            args[:0] = [f"{value} = state.{var}", f"{change} = on{pascal(c.name)}Change"]
        elif kind == "action":
            params.append("onClick: () -> Unit")
            args.append(f"onClick = on{pascal(c.name)}Click")
        params.append("modifier: Modifier = Modifier")
        return {"import": f"{style.module}.{fn}", "signature": f"{fn}({', '.join(params)})",
                "example": f"{fn}({', '.join(args)})"}
    if kind == "input":
        t = "Bool" if state_type == "Boolean" else state_type
        label = "isOn" if t == "Bool" else "text"
        params.insert(0, f"{label}: Binding<{t}>")
        args.insert(0, f"{label}: $state.{var}")
    elif kind == "action":
        params.append("action: @escaping () -> Void")
        args.append(f"action: on{pascal(c.name)}")
    return {"import": style.module, "signature": f"{fn}({', '.join(params)})", "example": f"{fn}({', '.join(args)})"}


def draft_entry(c: FigmaComponent, styles: dict[str, CodeStyle], rules: Rules = DEFAULT_RULES) -> dict:
    entry: dict = {"figma": c.name}
    if c.key:
        entry["figmaKey"] = c.key
    kind, state_type = infer_kind(c, rules)
    if kind:
        entry["kind"] = kind
    if state_type:
        entry["stateType"] = state_type
    entry["description"] = c.description
    entry["props"] = {}
    names = figma_prop_names(c)
    for p in ordered_props(c):
        entry["props"][_unique(prop_name(c.name, p, rules), entry["props"])] = prop_spec(p, names[p.key], rules)
    for platform, style in styles.items():
        entry[platform] = draft_code(c, entry, platform, style)
    entry["todo"] = TODO
    return entry


def _unique(name: str, taken) -> str:
    out, i = name, 2
    while out in taken:
        out, i = f"{name}{i}", i + 1
    return out


# ---------------------------------------------------------------- merging into an existing manifest

@dataclass
class Row:
    figma: str
    key: Optional[str]
    status: str                   # new | updated | unchanged | skipped
    kind: Optional[str] = None
    props: int = 0
    defined: bool = True
    changes: list = field(default_factory=list)       # what was changed in an existing entry
    review: list = field(default_factory=list)        # what a human has to decide
    sources: list = field(default_factory=list)


@dataclass
class ImportResult:
    manifest: dict
    rows: list = field(default_factory=list)
    skipped: list = field(default_factory=list)       # (name, why)
    missing: list = field(default_factory=list)       # manifest components not in the Figma input
    sources: list = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r.status == status)

    @property
    def needs_review(self) -> int:
        return sum(len(r.review) for r in self.rows)


def _find_existing(components: list[dict], c: FigmaComponent) -> Optional[dict]:
    if c.key:
        for e in components:
            if e.get("figmaKey") == c.key:
                return e
    wanted = normalize_name(c.name)
    for e in components:
        if normalize_name(e.get("figma", "")) == wanted:
            return e
    return None


def _merge(e: dict, c: FigmaComponent, styles: dict[str, CodeStyle], row: Row, rules: Rules) -> None:
    if e.get("figma") != c.name:
        row.changes.append(f"`figma`: '{e.get('figma')}' → '{c.name}' (exact Figma name)")
        e["figma"] = c.name
    if c.key and e.get("figmaKey") != c.key:
        row.changes.append(f"`figmaKey`: {e.get('figmaKey') or '(none)'} → {c.key}")
        e["figmaKey"] = c.key
    if not e.get("description") and c.description:
        e["description"] = c.description
        row.changes.append("`description` from Figma")
    kind, state_type = infer_kind(c, rules)
    if "kind" not in e and kind:
        e["kind"] = kind
        if state_type and "stateType" not in e:
            e["stateType"] = state_type
        row.changes.append(f"`kind`: {kind} (from the {'name' if not c.pressable else 'click interaction'})")
    elif kind and e.get("kind") != kind:
        row.review.append(f"Figma suggests `kind: {kind}`, the manifest says `{e.get('kind')}`")

    props = e.setdefault("props", {})
    names = figma_prop_names(c)
    by_ref = {}
    for pname, spec in props.items():
        if "figmaProp" in spec:
            by_ref.setdefault(normalize_name(spec["figmaProp"].split("#")[0]), pname)
    text_layer_props = {spec["figmaText"]: pname for pname, spec in props.items() if "figmaText" in spec}
    covered = set()
    for p in ordered_props(c):
        pname = by_ref.get(normalize_name(p.short))
        if pname is None and rules.alias(p.short) in props:        # figma_import.prop_aliases: {Type: style}
            pname = rules.alias(p.short)
        if pname is None and p.type == "TEXT" and p.layer in text_layer_props:
            covered.add(text_layer_props[p.layer])    # a figmaText prop already reads this text
            continue
        if pname is None and p.type == "TEXT":
            same = props.get(prop_name(c.name, p, rules))
            if same is not None and "figmaText" in same and "figmaProp" not in same:
                pname = prop_name(c.name, p, rules)   # e.g. `text` read from layer 'Label' -> the Figma text property
                row.changes.append(f"`{pname}`: figmaText '{same['figmaText']}' → figmaProp '{names[p.key]}'")
                props[pname] = {"figmaProp": names[p.key]}
                covered.add(pname)
                continue
        if pname is None:
            new = _unique(prop_name(c.name, p, rules), props)
            props[new] = prop_spec(p, names[p.key], rules)
            covered.add(new)
            row.changes.append(f"prop `{new}` added (Figma {p.type.lower()} `{names[p.key]}`)")
            continue
        covered.add(pname)
        spec = props[pname]
        if spec["figmaProp"] != names[p.key]:
            row.changes.append(f"`{pname}.figmaProp`: '{spec['figmaProp']}' → '{names[p.key]}'")
            spec["figmaProp"] = names[p.key]
        if p.type != "VARIANT":
            continue
        if "map" in spec:
            unmapped = [v for v in p.values if v not in spec["map"]]
            if unmapped:
                row.review.append(f"`{pname}.map` has no entry for Figma value(s) {', '.join(map(repr, unmapped))}")
        elif p.complete and spec.get("values") != p.values:
            row.changes.append(f"`{pname}.values`: {spec.get('values')} → {p.values}")
            spec["values"] = list(p.values)
        elif not p.complete:
            extra = [v for v in p.values if v not in (spec.get("values") or [])]
            if extra:
                spec["values"] = list(spec.get("values") or []) + extra
                row.changes.append(f"`{pname}.values`: added {extra} (seen in Figma)")

    known = {normalize_name(p.short) for p in c.props.values()}
    for pname, spec in props.items():
        if pname in covered:
            continue
        if "figmaProp" in spec and normalize_name(spec["figmaProp"].split("#")[0]) not in known:
            where = "the component" if c.defined else "the instances in these files (maybe just unused there)"
            row.review.append(f"`{pname}.figmaProp` '{spec['figmaProp']}' is not a property of {where}")
        elif "figmaText" in spec and c.text_layers and spec["figmaText"] not in c.text_layers:
            row.review.append(f"`{pname}.figmaText` '{spec['figmaText']}' matches no text layer "
                              f"(layers: {', '.join(sorted(c.text_layers))})")

    for platform, style in styles.items():
        if platform not in e:
            e[platform] = draft_code(c, e, platform, style)
            e["todo"] = TODO
            row.review.append(f"`{platform}` section drafted from Figma: replace it with the real API")
    row.status = "updated" if row.changes else "unchanged"


def collect(entries: list[tuple[dict, str]]) -> Collector:
    col = Collector()
    for entry, source in entries:
        col.add(entry, source)
    return col


def skip_reason(c: FigmaComponent, rules: Rules, ignore: Optional[list[str]]) -> Optional[str]:
    from .simplify import is_ignored

    if rules.private(c.name):
        return f"private component (name starts with {' or '.join(map(repr, rules.raw.private_prefixes))})"
    if ignore and is_ignored(c.name, ignore):
        return "matches readiness.ignore_layers (device decoration)"
    if c.artwork:
        return "icon / artwork only; reached through instance-swap props"
    return None


def import_figma(entries: list[tuple[dict, str]], base: Optional[dict], platforms: Optional[list[str]] = None,
                 default_primitives: Optional[dict] = None, ignore: Optional[list[str]] = None,
                 rules: Optional[FigmaImportRules] = None) -> ImportResult:
    """entries: (node entry, source label). base: the manifest to update (None = start a new one).
    ignore: layer-name globs (readiness.ignore_layers); matching components are device decoration.
    rules: figma_import naming conventions (default: FigmaImportRules())."""
    rules_ = Rules(rules)
    col = collect(entries)

    if base is not None:
        manifest = copy.deepcopy(base)
    else:
        manifest = {"version": "0.1.0",
                    "tokenCode": {"compose": "AppTheme.{category}.{name}", "swiftui": "Theme.{category}.{name}"},
                    "categoryCode": {"color": "colors", "spacing": "spacing", "radius": "radius",
                                     "typography": "typography"},
                    "primitives": copy.deepcopy(default_primitives or {}),
                    "components": []}
    components = manifest.setdefault("components", [])
    if not platforms:
        platforms = [p for p in ("compose", "swiftui") if p in (manifest.get("primitives") or {})] or \
            ["compose", "swiftui"]
    styles = {p: CodeStyle.of(components, p) for p in platforms}
    res = ImportResult(manifest, sources=sorted({s for _, s in entries}))

    matched = set()
    found = sorted(col.components.values(), key=lambda c: (not c.defined, c.name.lower()))
    for c in found:
        why = skip_reason(c, rules_, ignore)
        if why:
            res.skipped.append((c.name, why))
            continue
        row = Row(c.name, c.key, "new", defined=c.defined, sources=sorted(c.sources))
        existing = _find_existing(components, c)
        if existing is not None:
            matched.add(id(existing))
            _merge(existing, c, styles, row, rules_)
            entry = existing
        else:
            entry = draft_entry(c, styles, rules_)
            components.append(entry)
            matched.add(id(entry))
            row.review.append("new entry: write the real import, signature and example, then delete `todo`")
            if not c.description:
                row.review.append("no Figma description: write one line for the model")
        row.kind, row.props = entry.get("kind"), len(entry.get("props") or {})
        if not c.defined:
            row.review.append("seen only as an instance: variant values are the ones used here, not all of them "
                              "(import the component set itself for the full list)")
        partial = [p.short for p in c.props.values() if p.type == "VARIANT" and not p.complete and c.defined]
        if partial:
            row.review.append(f"variant(s) {', '.join(partial)} from a single variant: other values are unknown "
                              f"(import the component set for all of them)")
        if c.nested and not entry.get("kind"):
            actions = sorted(n for n in c.nested if name_has(n, rules_.raw.action_names) and not rules_.private(n))
            if actions:
                row.review.append(f"contains {', '.join(actions)}: its callbacks are not in the spec; add them to "
                                  f"the signature (e.g. `onActionClick`) if the code exposes them")
        res.rows.append(row)
    res.missing = [e.get("figma", "?") for e in components if id(e) not in matched]
    return res


# ---------------------------------------------------------------- report

def format_report(res: ImportResult) -> str:
    L = ["# Figma import report", "", "Sources:", ""] + [f"- `{s}`" for s in res.sources] + [""]
    L += [f"{res.count('new')} new, {res.count('updated')} updated, {res.count('unchanged')} unchanged; "
          f"{len(res.skipped)} skipped; **{res.needs_review} item(s) need a human**.", ""]
    L += ["## Components", "", "| figma | key | status | kind | props | source |", "|---|---|---|---|---|---|"]
    for r in res.rows:
        L.append(f"| {r.figma} | {r.key or '-'} | {r.status} | {r.kind or '-'} | {r.props} | "
                 f"{'definition' if r.defined else 'instances only'} |")
    L.append("")
    for r in res.rows:
        if not (r.changes or r.review):
            continue
        L += [f"### {r.figma}", ""]
        L += [f"- changed: {c}" for c in r.changes]
        L += [f"- **review**: {c}" for c in r.review]
        L.append("")
    if res.skipped:
        L += ["## Skipped", ""] + [f"- {n}: {why}" for n, why in res.skipped] + [""]
    if res.missing:
        L += ["## In the manifest, not in these Figma files", "",
              "Left as they are. Import their component sets too to check them.", ""]
        L += [f"- {n}" for n in res.missing] + [""]
    L += ["## Checklist", "",
          "1. Entries with `todo`: replace the drafted `import` / `signature` / `example` with the real UIKit API "
          "(prop names in `props` are what the spec will contain; rename them to the code's parameter names), "
          "then delete `todo`.",
          "2. Variants imported from a single variant or from instances list only the values seen: import the "
          "component set (`fetch` its link) to get all of them.",
          "3. `map`: Boolean-like and interaction-state variants (`State=Disabled`) were drafted as `map`; check "
          "the mapped values.",
          "4. Run `figma-codegen spec <frame>` on a real screen: each `prop_value` warning is a value the manifest "
          "does not know yet.",
          ""]
    return "\n".join(L)


# ---------------------------------------------------------------- library check (for designers)

CHECK_FIXES = {
    "no_description": "Add one line to the component set's description; the code generator reads it.",
    "boolean_variant": "Make it a boolean property (unless switching it changes the layout).",
    "state_values": "Rename the property (e.g. 'Tone' or 'Type'); keep '{prop}' for interaction states "
                    "like Normal / Pressed / Disabled.",
    "inconsistent_values": "Use '{use}' everywhere in the library.",
    "renamed_prop": "Rename it to '{code}' in Figma, or ask the developers to add "
                    "`figma_import.prop_aliases: {{{figma}: {prop}}}` to codegen.yaml.",
    "text_not_property": "Make it a text property (Create component property → Text) so instances can change it.",
}


@dataclass
class Problem:
    check: str                 # one of config.FIGMA_CHECKS
    component: str
    node_id: Optional[str]
    message: str
    fix: str
    key: Optional[str] = None

    def to_json(self) -> dict:
        return {"check": self.check, "component": self.component, "key": self.key, "node_id": self.node_id,
                "message": self.message, "fix": self.fix}


@dataclass
class CheckResult:
    problems: list = field(default_factory=list)
    checked: list = field(default_factory=list)        # (name, key) per component
    partial: list = field(default_factory=list)        # seen only as a single variant or as instances
    skipped: list = field(default_factory=list)        # (name, why)
    sources: list = field(default_factory=list)


def _values_text(values) -> str:
    return "/".join(map(str, values))


def check(entries: list[tuple[dict, str]], base: Optional[dict] = None, rules: Optional[FigmaImportRules] = None,
          ignore: Optional[list[str]] = None) -> CheckResult:
    """Where the Figma library does not follow the naming conventions (figma_import in codegen.yaml).
    base: the manifest, to find properties named differently in Figma and in the code (`renamed_prop`)."""
    r = Rules(rules)
    res = CheckResult(sources=sorted({s for _, s in entries}))
    comps = []
    for c in sorted(collect(entries).components.values(), key=lambda c: c.name.lower()):
        why = skip_reason(c, r, ignore)
        if why:
            res.skipped.append((c.name, why))
        else:
            comps.append(c)

    def add(check_: str, c: FigmaComponent, message: str, **fmt) -> None:
        if r.check_on(check_):
            res.problems.append(Problem(check_, c.name, c.node_id, message, CHECK_FIXES[check_].format(**fmt),
                                        c.key))

    # Spellings of the same value across the library: 'Disabled' vs 'disabled', 'Disabled' vs 'Inactive'.
    spellings: dict[str, Counter] = {}
    for c in comps:
        for p in c.props.values():
            if p.type != "VARIANT":
                continue
            for v in p.values:
                low = str(v).lower()
                group = "<disabled>" if low in r.disabled else low
                spellings.setdefault(group, Counter())[str(v)] += 1

    manifest = {normalize_name(e.get("figma", "")): e for e in (base or {}).get("components", [])}
    for c in comps:
        res.checked.append((c.name, c.key))
        if not c.defined or any(p.type == "VARIANT" and not p.complete for p in c.props.values()):
            res.partial.append(c.name)
        if c.defined and not c.description:
            add("no_description", c, "No description")
        for p in ordered_props(c):
            if _bool_map(p):
                add("boolean_variant", c, f"'{p.short}' is a variant with {_values_text(p.values)}")
            if p.type == "VARIANT" and normalize_name(p.short) in r.state_props:
                other = [v for v in p.values if str(v).lower() not in r.interaction]
                if other:
                    mixed = len(other) < len(p.values)
                    add("state_values", c, f"'{p.short}' has {_values_text(other)}"
                        + (" mixed with interaction states" if mixed else "")
                        + ": those describe what it is, not an interaction state", prop=p.short)
            if p.type == "VARIANT":
                for v in p.values:
                    low = str(v).lower()
                    counts = spellings["<disabled>" if low in r.disabled else low]
                    use = counts.most_common(1)[0][0]
                    if len(counts) > 1 and str(v) != use:
                        others = ", ".join(f"'{s}' ×{n}" for s, n in counts.most_common())
                        add("inconsistent_values", c, f"'{p.short}={v}': the library spells this value "
                                                      f"differently elsewhere ({others})", use=use)
        if c.defined:
            for layer in sorted(c.loose_text):
                add("text_not_property", c, f"Text layer '{layer}' is not a text property")
        e = manifest.get(normalize_name(c.name))
        if e:
            _renamed_props(c, e, r, add)
    return res


def _renamed_props(c: FigmaComponent, e: dict, r: Rules, add) -> None:
    """A manifest variant prop whose Figma name doesn't exist, and a Figma variant the manifest doesn't know:
    probably the same property under two names ('Style' in code, 'Type' in Figma)."""
    figma = {normalize_name(p.short): p for p in c.props.values() if p.type == "VARIANT" and not _bool_map(p)
             and not _enabled_map(p, r)}
    props = e.get("props") or {}
    used = {normalize_name(s["figmaProp"].split("#")[0]) for s in props.values() if "figmaProp" in s}
    aliased = {k for k in figma if r.alias(figma[k].short) in props}
    unknown_figma = [p for k, p in figma.items() if k not in used and k not in aliased]
    stale = [(n, s) for n, s in props.items() if "figmaProp" in s and ("values" in s or "map" in s)
             and normalize_name(s["figmaProp"].split("#")[0]) not in {normalize_name(p.short) for p in c.props.values()}]
    for name, spec in stale:
        code_values = set(map(str, spec.get("values") or spec.get("map") or []))
        match = [p for p in unknown_figma if code_values & set(map(str, p.values))]
        if not match and len(stale) == 1 and len(unknown_figma) == 1:
            match = unknown_figma
        if len(match) == 1:
            p = match[0]
            unknown_figma.remove(p)
            add("renamed_prop", c, f"Property '{p.short}' ({_values_text(p.values)}), but the code calls it "
                                   f"'{spec['figmaProp']}'", code=spec["figmaProp"], figma=p.short, prop=name)


def format_check(res: CheckResult, st=None) -> str:
    from .style import Style
    st = st or Style(False)
    by: dict[tuple, list] = {}
    for p in res.problems:
        by.setdefault((p.component, p.key), []).append(p)
    names = Counter(n for n, _ in res.checked)
    L = []
    for name, key in res.checked:
        probs = by.get((name, key), [])
        if not probs:
            continue
        L.append(st(name, "bold") + (st.dim(f"  (key {key})") if names[name] > 1 and key else ""))
        for p in probs:
            L.append(f"  {st.err('✗')} {p.message}")
            L.append(st.dim(f"      Fix in Figma: {p.fix}"))
        L.append("")
    ok = [n for n, k in res.checked if (n, k) not in by]
    if ok:
        L.append(f"{st.ok('✓')} no problems: {', '.join(ok)}")
    if res.partial:
        L.append(st.dim(f"partial check (single variant or instances only; fetch the component set for all "
                        f"values): {', '.join(res.partial)}"))
    if res.skipped:
        L.append(st.dim(f"skipped: {', '.join(n for n, _ in res.skipped)}"))
    n = len(res.problems)
    L.append(f"\n{len(res.checked)} component(s) checked, {n} problem{'s' if n != 1 else ''}")
    return "\n".join(L)
