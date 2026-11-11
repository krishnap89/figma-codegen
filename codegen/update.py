"""Targeted update: apply a design change list to an existing screen without losing developer edits.

The model gets the current file plus the change list (specdiff) and nothing about the rest of the
design. Its answer is then checked deterministically before lint + compile:

  - changed-lines budget: at most max(20, 12 × number of changes) lines may differ, so a full
    rewrite is rejected and retried with feedback,
  - public signature: the screen function / state shape must stay the same unless a change adds,
    removes or renames a binding or event.

Conflict hints (best effort): when the OLD value of a change no longer occurs in the file
(e.g. `AppTheme.spacing.s` was replaced by hand), the change is flagged as a possible conflict.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field

from .baseline import strip_header
from .design_system import DesignSystem
from .simplify import collect
from .specdiff import TOKEN_KEYS, Change, SpecTree, describe

CONFLICT_KINDS = {"token_changed", "text_changed", "prop_changed"}
# Changes that build something a blocking gap is about (gaps.py): deferred until UIKit has it.
DEFERRABLE = {"node_added", "component_swapped", "prop_changed"}


@dataclass
class UpdatePlan:
    changes: list[Change]
    conflicts: dict[int, str] = field(default_factory=dict)   # index in `changes` -> note
    components: list[str] = field(default_factory=list)       # components in the changed nodes
    tokens: list[str] = field(default_factory=list)           # tokens in the changes
    signature_may_change: bool = False
    deferred: list[int] = field(default_factory=list)          # need a UIKit change first: TODO only

    def change_lines(self) -> list[str]:
        """One line per change to apply, with the conflict note when there is one."""
        return [describe(c) + (f"  [{self.conflicts[i]}]" if i in self.conflicts else "")
                for i, c in enumerate(self.changes) if i not in self.deferred]

    def deferred_lines(self) -> list[str]:
        return [describe(self.changes[i]) for i in self.deferred]

    def trace(self) -> dict:
        return {"changes": [c.to_json() for c in self.changes],
                "conflicts": {self.changes[i].path: note for i, note in self.conflicts.items()},
                "deferred": [self.changes[i].path for i in self.deferred],
                "signature_may_change": self.signature_may_change}


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _subtree(node: dict) -> list[dict]:
    return [node] + [d for c in node.get("children") or [] for d in _subtree(c)]


def _root(spec: dict) -> dict:
    return spec.get("root", spec)


def _old_expressions(ds: DesignSystem, c: Change, platform: str) -> list[str]:
    """How the change's old value is likely written in the code; [] when we can't tell."""
    old = c.detail.get("old")
    if c.kind == "token_changed":
        return [ds.token_code(t, platform) for t in _strings(old) if t in ds.tokens]
    if c.kind in ("text_changed", "prop_changed") and isinstance(old, str) and old:
        return [old, json.dumps(old, ensure_ascii=False)[1:-1]]   # literal, or with escapes
    return []


def _occurs(expr: str, code: str) -> bool:
    """`expr` in the code, not as part of a longer identifier (AppTheme.spacing.s vs .sm)."""
    return re.search(rf"(?<![\w.]){re.escape(expr)}(?!\w)", code) is not None


def plan_update(ds: DesignSystem, old_spec: dict, new_spec: dict, changes: list[Change], code: str,
                platform: str, blocked_ids: set[str] | frozenset = frozenset()) -> UpdatePlan:
    """blocked_ids: Figma node ids with a missing component / unknown variant (gaps.blocking_node_ids).
    Changes that would build those nodes are deferred: the model only leaves a TODO(design-system)."""
    plan = UpdatePlan(changes)
    old_tree, new_tree = SpecTree(old_spec), SpecTree(new_spec)
    by_path = {**{old_tree.path(n): n for n in old_tree.nodes}, **{new_tree.path(n): n for n in new_tree.nodes}}
    components: set[str] = set()
    tokens: set[str] = set()
    for i, c in enumerate(changes):
        node = by_path.get(c.path)
        whole = c.kind in ("node_added", "node_removed", "component_swapped")   # the subtree is new code
        nodes = (_subtree(node) if whole else [node]) if node is not None else []
        components.update(n["component"] for n in nodes if n.get("component"))
        if c.kind == "component_swapped":
            components.update(v for v in (c.detail.get("old"), c.detail.get("new")) if isinstance(v, str))
        if whole:
            for n in nodes:
                tokens.update(s for k in TOKEN_KEYS for s in _strings(n.get(k)))
        tokens.update(_strings(c.detail.get("old")) + _strings(c.detail.get("new")))

        if c.kind in DEFERRABLE and blocked_ids:
            ids = {n.get("id") for n in (nodes if c.kind != "prop_changed" else [])} | {c.node_id}
            if ids & set(blocked_ids):
                plan.deferred.append(i)
                continue
        if c.kind in CONFLICT_KINDS:
            exprs = _old_expressions(ds, c, platform)
            if exprs and not any(_occurs(e, code) for e in exprs):
                plan.conflicts[i] = (f"possible conflict: code no longer matches the baseline "
                                     f"(`{exprs[0]}` not found in the file)")
    plan.components = sorted(n for n in components if ds.component(n))
    plan.tokens = sorted(t for t in tokens if t in ds.tokens)
    plan.signature_may_change = any(collect(_root(old_spec), k) != collect(_root(new_spec), k)
                                    for k in ("binding", "event"))
    return plan


# ---------- safety checks ----------
def changed_lines(a: str, b: str) -> int:
    """Lines that differ between two versions (a replaced block counts its longer side)."""
    sm = difflib.SequenceMatcher(None, a.splitlines(), b.splitlines(), autojunk=False)
    total = 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "replace":
            total += max(i2 - i1, j2 - j1)
        elif op == "delete":
            total += i2 - i1
        elif op == "insert":
            total += j2 - j1
    return total


def line_budget(n_changes: int) -> int:
    return max(20, 12 * n_changes)


def _balanced(code: str, start: int, open_ch: str, close_ch: str) -> str | None:
    """Text from code[start] (an opening bracket) to its matching close, inclusive."""
    depth = 0
    for i in range(start, len(code)):
        if code[i] == open_ch:
            depth += 1
        elif code[i] == close_ch:
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
    return None


def _norm(text: str) -> str:
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*,?\s*\)", ")", text)      # trailing comma, space before ')'
    return re.sub(r"\(\s+", "(", text)


def _swift_members(code: str, struct: str) -> str | None:
    m = re.search(rf"\bstruct\s+{re.escape(struct)}\b[^{{]*\{{", code)
    if not m:
        return None
    body = _balanced(code, m.end() - 1, "{", "}")
    if body is None:
        return None
    members, depth, line = [], 0, ""
    for ch in body[1:-1]:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "\n":
            if depth == 0 and re.match(r"\s*(@\w+(\([^)]*\))?\s+)*((public|private|internal)\s+)?(var|let)\s+\w+",
                                       line) and not re.match(r"\s*(\w+\s+)*var\s+body\b", line):
                members.append(_norm(line))
            line = ""
            continue
        if depth == 0:
            line += ch
    return f"struct {struct} {{ " + "; ".join(members) + " }"


def screen_signature(code: str, platform: str, screen: str) -> str | None:
    """The screen's public shape, whitespace-normalised; None when the screen can't be found.

    compose: `fun <Screen>(...)` and `class <Screen>State(...)`;
    swiftui: stored properties of `struct <Screen>` and `struct <Screen>State`.
    """
    code = strip_header(code)
    if platform == "compose":
        parts = []
        for pattern in (rf"\bfun\s+{re.escape(screen)}\s*\(", rf"\bclass\s+{re.escape(screen)}State\s*\("):
            m = re.search(pattern, code)
            if m:
                params = _balanced(code, m.end() - 1, "(", ")")
                parts.append(_norm(m.group(0)[:-1] + (params or "")))
            elif "fun" in pattern:
                return None
        return " | ".join(parts)
    view = _swift_members(code, screen)
    if view is None:
        return None
    state = _swift_members(code, f"{screen}State")
    return view + (" | " + state if state else "")


class SafetyCheck:
    """`check(code)` for CodegenAgent.run_update: problems with an answer, as feedback for the model."""

    def __init__(self, original: str, plan: UpdatePlan, platform: str, screen: str):
        self.original = original
        self.plan = plan
        self.platform = platform
        self.screen = screen
        self.budget = line_budget(len(plan.changes))
        self.signature = screen_signature(original, platform, screen)
        self.last_stats: dict = {}

    def __call__(self, code: str) -> list[str]:
        problems = []
        n = changed_lines(strip_header(self.original), strip_header(code))
        if n > self.budget:
            problems.append(f"too many unrelated changes: {n} changed lines for {len(self.plan.changes)} design "
                            f"change(s) (limit {self.budget}). Start again from the current file, apply only the "
                            "listed changes and leave every other line exactly as it was.")
        signature_ok = True
        if self.signature is not None and not self.plan.signature_may_change:
            new = screen_signature(code, self.platform, self.screen)
            if new != self.signature:
                signature_ok = False
                problems.append("the screen's public signature changed, but no listed change adds, removes or "
                                f"renames a binding or event. Keep it exactly as it was: {self.signature}")
        self.last_stats = {"changed_lines": n, "budget": self.budget, "signature_ok": signature_ok}
        return problems


def unified_diff(old: str, new: str, old_name: str, new_name: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        old_name, new_name))
