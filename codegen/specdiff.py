"""Deterministic diff of two simplified specs: what the designer changed, as a short reviewable list.

No LLM. Nodes of the old and new tree are matched in this order:

  a) same Figma node id ("id" on spec nodes), anywhere in the tree,
  b) same name + same type/component under the same matched parent (then same name only:
     a component swapped in place, or a Column that became a Row),
  c) everything left over is removed (old) or added (new).

The list is minimal: an added or removed subtree is one change, a moved subtree is one change
(its children move with it), and a reorder reports only the nodes that left their place
(longest run of siblings that kept their order stays put).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from .style import Style

CONTAINERS = {"Column", "Row", "Box"}
STRUCTURAL = {"children", "id", "name", "type", "component"}
# Attribute order in the change list (anything else follows, sorted by name).
KEY_ORDER = ["text", "style", "color", "background", "cornerRadius", "border", "padding", "gap", "size",
             "arrangement", "align", "fill", "weight", "sizePx", "figmaComponent", "binding", "event", "props"]
TOKEN_KEYS = {"style", "color", "background", "cornerRadius", "border", "padding", "gap", "size"}
LAYOUT_KEYS = {"arrangement", "align", "fill", "weight", "sizePx"}
# Component props that hold copy: a change there is content, not visual.
TEXT_PROPS = {"text", "label", "placeholder", "title", "subtitle", "hint", "helperText", "caption",
              "message", "description", "value"}
SEVERITY_ORDER = ("visual", "content", "structure")


@dataclass
class Change:
    kind: str          # prop_changed, token_changed, text_changed, layout_changed, node_added, node_removed,
                       # node_moved, component_swapped, binding_changed, event_changed
    path: str          # "LoginScreen > Form > EmailField"
    node_name: str
    detail: dict
    severity: str      # "visual" | "structure" | "content"
    node_id: str | None = None

    def to_json(self) -> dict:
        return asdict(self)


def node_kind(node: dict) -> str:
    return node.get("component") or node.get("type") or "?"


def _label(node: dict) -> str:
    return node.get("name") or node_kind(node)


def _children(node: dict) -> list[dict]:
    return node.get("children") or []


class SpecTree:
    """Preorder index of a spec tree: parent, sibling index and readable path of every node."""

    def __init__(self, spec: dict):
        if "root" in spec:
            self.root = spec["root"]
            root_label = spec.get("screen") or _label(self.root)
        else:
            self.root = spec
            root_label = _label(spec)
        self.nodes: list[dict] = []
        self.parent: dict[int, dict | None] = {}
        self.index: dict[int, int] = {}
        self._path: dict[int, str] = {}
        self._walk(self.root, None, 0, root_label)

    def _walk(self, node: dict, parent: dict | None, i: int, label: str) -> None:
        self.nodes.append(node)
        self.parent[id(node)] = parent
        self.index[id(node)] = i
        self._path[id(node)] = label if parent is None else f"{self._path[id(parent)]} > {label}"
        for j, c in enumerate(_children(node)):
            self._walk(c, node, j, _label(c))

    def path(self, node: dict) -> str:
        return self._path[id(node)]

    def label(self, node: dict) -> str:
        """Like _label, but the root shows the screen name."""
        return self._path[id(node)] if self.parent[id(node)] is None else _label(node)


def _lis(seq: list[int]) -> set[int]:
    """Positions in `seq` that form one longest increasing subsequence (O(n²); sibling lists are short)."""
    if not seq:
        return set()
    best = [1] * len(seq)
    prev = [-1] * len(seq)
    for i in range(len(seq)):
        for j in range(i):
            if seq[j] < seq[i] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    i = max(range(len(seq)), key=lambda k: (best[k], -k))
    keep = set()
    while i != -1:
        keep.add(i)
        i = prev[i]
    return keep


def _descendants(node: dict) -> int:
    return sum(1 + _descendants(c) for c in _children(node))


def summarize(node: dict) -> str:
    """One line: kind, layer name, its copy if it has any, nested count. 'Link "Forgot" — "Forgot password?"'"""
    s = node_kind(node)
    if node.get("name"):
        s += f' "{node["name"]}"'
    copy = node.get("text") or next((v for k, v in (node.get("props") or {}).items()
                                     if k in TEXT_PROPS and isinstance(v, str) and v), None)
    if copy and copy != node.get("name"):
        s += " — " + json.dumps(copy if len(copy) <= 40 else copy[:39] + "…", ensure_ascii=False)
    n = _descendants(node)
    if n:
        s += f" (+{n} nested)"
    return s


class _Differ:
    def __init__(self, old: dict, new: dict):
        self.old, self.new = SpecTree(old), SpecTree(new)
        self.m: dict[int, dict] = {}     # id(old node) -> new node
        self.rev: dict[int, dict] = {}   # id(new node) -> old node
        self.changes: list[Change] = []
        self._match()

    # ---------- matching ----------
    def _pair(self, o: dict, n: dict) -> None:
        self.m[id(o)] = n
        self.rev[id(n)] = o

    def _match(self) -> None:
        self._pair(self.old.root, self.new.root)
        by_id = {n["id"]: n for n in self.new.nodes if n.get("id")}
        for o in self.old.nodes:                                  # a) Figma node id
            n = by_id.get(o.get("id")) if o.get("id") else None
            if n is not None and id(o) not in self.m and id(n) not in self.rev:
                self._pair(o, n)
        for o in self.old.nodes:                                  # b) name under the matched parent
            n = self.m.get(id(o))           # preorder: a parent is always handled before its children
            if n is None:
                continue
            for strict in (True, False):
                free = [c for c in _children(n) if id(c) not in self.rev]
                for oc in _children(o):
                    if id(oc) in self.m or (not strict and not oc.get("name")):
                        continue
                    key = (oc.get("name"), node_kind(oc)) if strict else oc.get("name")
                    for nc in free:
                        if id(nc) not in self.rev and key == ((nc.get("name"), node_kind(nc)) if strict
                                                               else nc.get("name")):
                            self._pair(oc, nc)
                            break

    # ---------- emitting ----------
    def _add(self, kind: str, tree: SpecTree, node: dict, severity: str, **detail: Any) -> None:
        self.changes.append(Change(kind, tree.path(node), tree.label(node), detail, severity, node.get("id")))

    def run(self) -> list[Change]:
        self._visit(self.new.root, reordered=set())
        return self.changes

    def _visit(self, n: dict, reordered: set[int]) -> None:
        o = self.rev.get(id(n))
        if o is None:
            parent = self.new.parent[id(n)]
            if parent is not None and id(parent) in self.rev:   # else covered by an added ancestor
                self._add("node_added", self.new, n, "structure", summary=summarize(n),
                          position=self._position(n))
        else:
            self._moved(o, n, reordered)
            self._attrs(o, n)

        kids = _children(n)
        if o is None:
            for c in kids:
                self._visit(c, set())
            return
        # Siblings that stayed under the same parent: those outside the longest in-order run moved.
        same = [(i, self.old.index[id(self.rev[id(c)])]) for i, c in enumerate(kids)
                if id(c) in self.rev and self.old.parent[id(self.rev[id(c)])] is o]
        keep = _lis([k for _, k in same])
        moved_here = {id(kids[i]) for pos, (i, _) in enumerate(same) if pos not in keep}

        removed = [c for c in _children(o) if id(c) not in self.m]
        for c in kids:
            oc = self.rev.get(id(c))
            if oc is not None and self.old.parent[id(oc)] is o and id(c) not in moved_here:
                k = self.old.index[id(oc)]
                while removed and self.old.index[id(removed[0])] < k:
                    self._removed(removed.pop(0))
            self._visit(c, moved_here)
        for r in removed:
            self._removed(r)

    def _removed(self, o: dict) -> None:
        parent = self.old.parent[id(o)]
        self._add("node_removed", self.old, o, "structure", summary=summarize(o),
                  parent=self.old.label(parent) if parent is not None else None)

    def _position(self, n: dict) -> str:
        parent = self.new.parent[id(n)]
        i = self.new.index[id(n)]
        if parent is None:
            return ""
        if i == 0:
            return f"first in {self.new.label(parent)}"
        return f"after {_label(_children(parent)[i - 1])}"

    def _moved(self, o: dict, n: dict, reordered: set[int]) -> None:
        op, np_ = self.old.parent[id(o)], self.new.parent[id(n)]
        if np_ is None:
            return
        if op is None or self.m.get(id(op)) is not np_:
            self._add("node_moved", self.new, n, "structure",
                      old_parent=self.old.path(op) if op is not None else None,
                      new_parent=self.new.path(np_), position=self._position(n))
        elif id(n) in reordered:
            self._add("node_moved", self.new, n, "structure", old_index=self.old.index[id(o)],
                      new_index=self.new.index[id(n)], position=self._position(n))

    def _attrs(self, o: dict, n: dict) -> None:
        ko, kn = node_kind(o), node_kind(n)
        if ko != kn:
            if ko in CONTAINERS and kn in CONTAINERS:
                self._add("layout_changed", self.new, n, "structure", key="type", old=ko, new=kn)
            else:
                # A different component: its props are a new set, not edits of the old ones.
                self._add("component_swapped", self.new, n, "structure", old=ko, new=kn,
                          props=n.get("props") or {})
                return
        keys = [k for k in KEY_ORDER if k in o or k in n]
        keys += sorted(k for k in set(o) | set(n) if k not in STRUCTURAL and k not in KEY_ORDER)
        for key in keys:
            if key == "props":
                self._props(o.get("props") or {}, n.get("props") or {}, n)
                continue
            old, new = o.get(key), n.get(key)
            if old == new:
                continue
            if key == "text":
                self._add("text_changed", self.new, n, "content", key=key, old=old, new=new)
            elif key in TOKEN_KEYS:
                self._add("token_changed", self.new, n, "visual", key=key, old=old, new=new)
            elif key in LAYOUT_KEYS:
                self._add("layout_changed", self.new, n, "visual", key=key, old=old, new=new)
            elif key == "binding":
                self._add("binding_changed", self.new, n, "structure", key=key, old=old, new=new)
            elif key == "event":
                self._add("event_changed", self.new, n, "structure", key=key, old=old, new=new)
            elif key == "figmaComponent":
                self._add("component_swapped", self.new, n, "structure", key=key, old=old, new=new)
            else:
                self._add("prop_changed", self.new, n, "visual", key=key, old=old, new=new)

    def _props(self, old: dict, new: dict, n: dict) -> None:
        for key in list(new) + [k for k in old if k not in new]:
            if old.get(key) != new.get(key):
                copy = key in TEXT_PROPS and isinstance(old.get(key) or new.get(key), str)
                self._add("prop_changed", self.new, n, "content" if copy else "visual",
                          key=key, old=old.get(key), new=new.get(key))


def diff_specs(old: dict, new: dict) -> list[Change]:
    """Changes from `old` to `new`, in tree order. Each may be a full spec ({"screen", "root"}) or a root node."""
    return _Differ(old, new).run()


# ---------- rendering ----------
def _fmt(value: Any, quote: bool = False) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return ", ".join(f"{k} {_fmt(v)}" for k, v in value.items()) or "none"
    if quote and isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def describe(c: Change, name: str | None = None) -> str:
    """One line for a change, e.g. 'Form: gap spacing.s → spacing.m'."""
    name = name or c.node_name
    d = c.detail
    if c.kind == "node_added":
        return f"+ added {d['summary']}" + (f" {d['position']}" if d.get("position") else "")
    if c.kind == "node_removed":
        return f"- removed {d['summary']}" + (f" from {d['parent']}" if d.get("parent") else "")
    if c.kind == "node_moved":
        if "new_parent" in d:
            return f"~ moved {name}: {d['old_parent'] or '?'} → {d['new_parent']} ({d['position']})"
        return f"~ moved {name}: now {d['position']}"
    if c.kind == "component_swapped" and "key" not in d:
        return f"{name}: {d['old']} → {d['new']}"
    quote = c.kind == "text_changed" or c.severity == "content"
    return f"{name}: {d['key']} {_fmt(d['old'], quote)} → {_fmt(d['new'], quote)}"


def render_changes(changes: list[Change], style: Style | None = None) -> str:
    """Grouped by severity (visual, content, structure), tree order inside each group:

      3 changes
      visual     Title: style typography.heading/m → typography.heading/l
      visual     Form: gap spacing.s → spacing.m
      structure  + added Link "ForgotPasswordLink" after PasswordField
    """
    st = style or Style(False)
    if not changes:
        return "no changes"
    # Use the full path where a layer name alone is ambiguous.
    paths_by_name: dict[str, set[str]] = {}
    for c in changes:
        paths_by_name.setdefault(c.node_name, set()).add(c.path)
    lines = [f"{len(changes)} change{'s' if len(changes) != 1 else ''}"]
    for sev in SEVERITY_ORDER:
        for c in (c for c in changes if c.severity == sev):
            name = c.path if len(paths_by_name[c.node_name]) > 1 else c.node_name
            label = f"{sev:<10}"
            label = st.warn(label) if sev == "structure" else st.dim(label)
            lines.append(f"{label} {describe(c, name)}")
    return "\n".join(lines)
