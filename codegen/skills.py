"""Skills: small folders of team conventions added to the prompt only when a screen needs them.

    skills/<name>/SKILL.md        YAML front matter + Markdown instructions
    skills/<name>/examples/       optional <Name>.kt / <Name>.swift (+ <Name>.spec.json)

Selection is deterministic (see `select_skills`): it looks at the simplified spec, never asks the model.
`plan_skills` then applies the character budget and the few-shot example limit.
"""
from __future__ import annotations

import re
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

PLATFORMS = ("compose", "swiftui")
_EXT = {".kt": "compose", ".swift": "swiftui"}
_FENCE = {"compose": "kotlin", "swiftui": "swift"}
_TOP_KEYS = {"name", "description", "platforms", "when", "forbidden_patterns", "priority"}
_WHEN_KEYS = {"always", "components", "min_count", "kinds", "repeated_rows"}
DEFAULT_PRIORITY = 50
REPEATED_ROWS_MIN = 3


class SkillWarning(UserWarning):
    """An invalid skill was skipped."""


@dataclass
class SkillError:
    name: str           # folder name
    path: Path
    message: str

    def __str__(self) -> str:
        return f"skill '{self.name}' skipped: {self.message} ({self.path})"


@dataclass
class Example:
    name: str
    platform: str
    code: str
    spec: Optional[str] = None


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    platforms: list = field(default_factory=lambda: list(PLATFORMS))
    when: Optional[dict] = None
    forbidden_patterns: list = field(default_factory=list)
    priority: int = DEFAULT_PRIORITY
    examples: list = field(default_factory=list)

    def examples_for(self, platform: str) -> list:
        return [e for e in self.examples if e.platform == platform]

    def render(self, platform: str, examples: Optional[list] = None) -> str:
        """The block added to the prompt (and returned by the `load_skill` tool)."""
        examples = self.examples_for(platform) if examples is None else examples
        out = [f"### {self.name} — {self.description}\n{self.body}"]
        for ex in examples:
            part = f"Example `{ex.name}`:\n"
            if ex.spec:
                part += f"```json\n{ex.spec.strip()}\n```\n"
            part += f"```{_FENCE[platform]}\n{ex.code.strip()}\n```"
            out.append(part)
        return "\n\n".join(out)


@dataclass
class SelectedSkill:
    skill: Skill
    reason: str
    forced: bool = False

    @property
    def name(self) -> str:
        return self.skill.name


@dataclass
class IncludedSkill:
    selected: SelectedSkill
    examples: list
    text: str

    @property
    def name(self) -> str:
        return self.selected.name


@dataclass
class SkillPlan:
    """What goes into one run's prompt."""
    platform: str
    included: list = field(default_factory=list)     # IncludedSkill, priority order
    dropped: list = field(default_factory=list)      # SelectedSkill, over budget
    available: list = field(default_factory=list)    # every valid Skill for the platform
    budget: int = 6000

    @property
    def chars(self) -> int:
        return sum(len(i.text) for i in self.included)

    @property
    def examples_used(self) -> int:
        return sum(len(i.examples) for i in self.included)

    def blocks(self) -> list:
        return [i.text for i in self.included]

    def patterns(self) -> dict:
        return {i.name: i.selected.skill.forbidden_patterns for i in self.included
                if i.selected.skill.forbidden_patterns}

    def summary(self) -> str:
        """'compose-conventions (always), forms (kinds: input ×2)'"""
        return ", ".join(f"{i.name} ({i.selected.reason})" for i in self.included) or "none"

    def trace(self) -> dict:
        return {"skills": [{"name": i.name, "reason": i.selected.reason} for i in self.included],
                "skills_dropped": [s.name for s in self.dropped], "skill_chars": self.chars}


# ---------------------------------------------------------------- loading

def _front_matter(text: str) -> tuple[str, str]:
    m = re.match(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?$\n?", text, re.S | re.M)
    if not m:
        raise ValueError("SKILL.md must start with YAML front matter between '---' lines")
    return m.group(1), text[m.end():]


def _str_list(meta: dict, key: str, where: str = "") -> list:
    v = meta.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"`{where}{key}` must be a list of strings")
    return v


def _parse_when(when) -> Optional[dict]:
    if when is None:
        return None
    if not isinstance(when, dict):
        raise ValueError("`when` must be a mapping")
    unknown = set(when) - _WHEN_KEYS
    if unknown:
        raise ValueError(f"unknown `when` key(s): {', '.join(sorted(unknown))} "
                         f"(allowed: {', '.join(sorted(_WHEN_KEYS))})")
    for key in ("always", "repeated_rows"):
        if key in when and not isinstance(when[key], bool):
            raise ValueError(f"`when.{key}` must be true or false")
    out = {"always": when.get("always", False), "repeated_rows": when.get("repeated_rows", False),
           "components": _str_list(when, "components", "when."), "kinds": _str_list(when, "kinds", "when.")}
    mc = when.get("min_count") or {}
    if not isinstance(mc, dict) or not all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
                                           and v >= 1 for k, v in mc.items()):
        raise ValueError("`when.min_count` must map a component or kind to a whole number >= 1")
    stray = [k for k in mc if k not in out["components"] and k not in out["kinds"]]
    if stray:
        raise ValueError(f"`when.min_count` key(s) {', '.join(stray)} must also be listed in "
                         f"`when.components` or `when.kinds`")
    out["min_count"] = dict(mc)
    return out


def _load_examples(d: Path) -> list:
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.iterdir()):
        if f.suffix in _EXT and f.is_file():
            spec = f.with_name(f.stem + ".spec.json")
            out.append(Example(f.stem, _EXT[f.suffix], f.read_text(), spec.read_text() if spec.exists() else None))
    return out


def load_skill(folder: Path) -> Skill:
    """Parse one skill folder; raises ValueError with a readable message when it is invalid."""
    md = folder / "SKILL.md"
    if not md.is_file():
        raise ValueError("missing SKILL.md")
    raw, body = _front_matter(md.read_text())
    try:
        meta = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ValueError(f"bad YAML in front matter: {str(e).splitlines()[0]}") from None
    if not isinstance(meta, dict):
        raise ValueError("front matter must be a YAML mapping")
    unknown = set(meta) - _TOP_KEYS
    if unknown:
        raise ValueError(f"unknown front matter key(s): {', '.join(sorted(unknown))}")
    name = meta.get("name")
    if name != folder.name:
        raise ValueError(f"`name: {name}` must equal the folder name `{folder.name}`")
    desc = meta.get("description")
    if not isinstance(desc, str) or not desc.strip() or "\n" in desc.strip():
        raise ValueError("`description` must be one non-empty line")
    platforms = _str_list(meta, "platforms") or list(PLATFORMS)
    bad = [p for p in platforms if p not in PLATFORMS]
    if bad:
        raise ValueError(f"unknown platform(s) {', '.join(bad)} (allowed: {', '.join(PLATFORMS)})")
    patterns = _str_list(meta, "forbidden_patterns")
    for p in patterns:
        try:
            re.compile(p)
        except re.error as e:
            raise ValueError(f"forbidden_patterns: invalid regex {p!r}: {e}") from None
    priority = meta.get("priority", DEFAULT_PRIORITY)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ValueError("`priority` must be a whole number")
    if not body.strip():
        raise ValueError("SKILL.md has no instructions after the front matter")
    return Skill(name=name, description=desc.strip(), body=body.strip(), path=folder, platforms=platforms,
                 when=_parse_when(meta.get("when")), forbidden_patterns=patterns, priority=priority,
                 examples=_load_examples(folder / "examples"))


def scan_skills(path) -> tuple[list, list]:
    """-> (valid skills, errors). A missing directory means no skills."""
    if not path:
        return [], []
    root = Path(path)
    if not root.is_dir():
        return [], []
    skills, errors = [], []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        try:
            skills.append(load_skill(folder))
        except (ValueError, OSError, UnicodeDecodeError) as e:
            errors.append(SkillError(folder.name, folder, str(e)))
    return skills, errors


def load_skills(path) -> list:
    """Valid skills under `path`; each invalid one is reported as a SkillWarning and skipped."""
    skills, errors = scan_skills(path)
    for e in errors:
        warnings.warn(str(e), SkillWarning, stacklevel=2)
    return skills


# ---------------------------------------------------------------- selection

def _signature(node: dict) -> tuple:
    """Structure of a node: its type/component and its children's, ignoring texts and names."""
    head = node.get("component") or node.get("type") or "?"
    return (head, tuple(_signature(c) for c in node.get("children") or []))


def _walk(node: dict):
    yield node
    for c in node.get("children") or []:
        yield from _walk(c)


def repeated_rows(root: dict) -> Optional[tuple[str, int]]:
    """(container name, row count) of the first container with 3+ child containers of the same structure."""
    for node in _walk(root):
        rows = [c for c in node.get("children") or [] if c.get("children")]
        if len(rows) < REPEATED_ROWS_MIN:
            continue
        sig, n = Counter(_signature(c) for c in rows).most_common(1)[0]
        if n >= REPEATED_ROWS_MIN:
            return node.get("name") or node.get("type") or "?", n
    return None


@dataclass
class _Facts:
    components: Counter
    kinds: Counter
    rows: Optional[tuple]


def _facts(spec: dict, ds) -> _Facts:
    root = spec.get("root", spec)
    comps = Counter(n["component"] for n in _walk(root) if n.get("component"))
    kinds: Counter = Counter()
    for name, n in comps.items():
        kind = ((ds.component(name) if ds is not None else None) or {}).get("kind")
        if kind:
            kinds[kind] += n
    return _Facts(comps, kinds, repeated_rows(root))


def _match(when: dict, facts: _Facts) -> Optional[str]:
    if when["always"]:
        return "always"
    reasons = []
    for key, counts in (("components", facts.components), ("kinds", facts.kinds)):
        hits = [f"{x} ×{counts[x]}" for x in when[key] if counts[x] and counts[x] >= when["min_count"].get(x, 1)]
        if hits:
            reasons.append(f"{key}: {', '.join(hits)}")
    if when["repeated_rows"] and facts.rows:
        reasons.append(f"repeated_rows: {facts.rows[1]} in {facts.rows[0]}")
    return "; ".join(reasons) or None


def _order(items: list) -> list:
    return sorted(items, key=lambda s: (-s.skill.priority, s.skill.name))


def select_skills(skills: Iterable, spec: dict, platform: str, ds) -> list:
    """Skills whose `when` matches this spec on this platform, each with the reason, priority order."""
    facts = _facts(spec, ds)
    out = []
    for s in skills:
        if platform not in s.platforms or not s.when:
            continue
        reason = _match(s.when, facts)
        if reason:
            out.append(SelectedSkill(s, reason))
    return _order(out)


def plan_skills(skills: list, spec: dict, platform: str, ds, *, max_chars: int = 6000, max_examples: int = 2,
                forced: Iterable = (), auto: bool = True) -> SkillPlan:
    """Select (unless auto=False), add forced skills, hand out example slots and apply the char budget.

    Forced skills (--skill) are never dropped. Raises ValueError for an unknown or wrong-platform forced name.
    """
    by_name = {s.name: s for s in skills}
    chosen = select_skills(skills, spec, platform, ds) if auto else []
    names = {s.name for s in chosen}
    for name in forced:
        s = by_name.get(name)
        if s is None:
            raise ValueError(f"unknown skill '{name}' (available: {', '.join(sorted(by_name)) or 'none'})")
        if platform not in s.platforms:
            raise ValueError(f"skill '{name}' is for {', '.join(s.platforms)}, not {platform}")
        if name in names:
            next(c for c in chosen if c.name == name).forced = True
        else:
            chosen.append(SelectedSkill(s, "--skill", forced=True))
            names.add(name)
    chosen = _order(chosen)

    plan = SkillPlan(platform, available=[s for s in skills if platform in s.platforms], budget=max_chars)
    slots = max(0, max_examples)
    for sel in chosen:
        examples = sel.skill.examples_for(platform)[:slots]
        slots -= len(examples)
        plan.included.append(IncludedSkill(sel, examples, sel.skill.render(platform, examples)))
    for inc in reversed(list(plan.included)):          # lowest priority first
        if plan.chars <= max_chars:
            break
        if not inc.selected.forced:
            plan.included.remove(inc)
            plan.dropped.insert(0, inc.selected)
    return plan


# ---------------------------------------------------------------- model-chosen skills

class SkillLoader:
    """Backs the `load_skill` tool: skills the model may pull in, under the same char budget."""

    def __init__(self, plan: SkillPlan):
        self.plan = plan
        self.loaded: list = []
        self.chars = plan.chars

    @property
    def platform(self) -> str:
        return self.plan.platform

    def included_names(self) -> set:
        return {i.name for i in self.plan.included}

    def catalog(self) -> list:
        """Skills not already in the prompt: offered by name + description."""
        inc = self.included_names()
        return [s for s in self.plan.available if s.name not in inc]

    def load(self, name: str) -> str:
        if name in self.included_names():
            return f"Skill '{name}' is already included in the prompt (Team conventions section)."
        if any(s.name == name for s in self.loaded):
            return f"Skill '{name}' was already loaded above."
        skill = next((s for s in self.plan.available if s.name == name), None)
        if skill is None:
            names = ", ".join(s.name for s in self.catalog()) or "none"
            return f"Unknown skill '{name}'. Available: {names}"
        text = skill.render(self.platform)
        left = self.plan.budget - self.chars
        if len(text) > left:
            return (f"error: skill '{name}' ({len(text)} chars) does not fit the remaining skill budget "
                    f"({max(left, 0)} chars of agent.max_skill_chars). Continue without it.")
        self.chars += len(text)
        self.loaded.append(skill)
        return text

    def patterns(self) -> dict:
        return {s.name: s.forbidden_patterns for s in self.loaded if s.forbidden_patterns}

    def loaded_text(self) -> str:
        return "\n\n".join(s.render(self.platform) for s in self.loaded)
