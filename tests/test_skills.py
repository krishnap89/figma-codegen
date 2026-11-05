"""Skills: loading, deterministic selection, budget, prompt/lint/tool integration and CLI."""
import json
from pathlib import Path

import pytest

from codegen.agent import CodegenAgent
from codegen.config import Config
from codegen.design_system import DesignSystem
from codegen.figma_client import load_node_from_file
from codegen.prompts import system_prompt, user_prompt
from codegen.simplify import simplify
from codegen.skills import (SkillLoader, SkillWarning, load_skills, plan_skills, repeated_rows, scan_skills,
                            select_skills)
from codegen.tools import Tools
from codegen.validate import lint

from test_pipeline import FIXTURE, ROOT, FakeClient, _run_cli, call

GOLDEN = ROOT / "tests/fixtures/prompts"
GOOD = "```kotlin\nfun LoginScreen() { Column(Modifier.padding(AppTheme.spacing.m)) {} }\n```"


# ---------------------------------------------------------------- helpers

def write_skill(root: Path, name: str, front: str, body: str = "Rule one.\n\nDo\n- x\n", examples=None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{front.strip()}\n---\n{body}")
    for fname, text in (examples or {}).items():
        (d / "examples").mkdir(exist_ok=True)
        (d / "examples" / fname).write_text(text)
    return d


@pytest.fixture
def ds():
    manifest = {"components": [
        {"figma": "TextField", "kind": "input"},
        {"figma": "Check Box", "kind": "input", "stateType": "Boolean"},
        {"figma": "Button", "kind": "action"},
    ]}
    return DesignSystem(manifest, {})


@pytest.fixture
def skills_dir(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "conventions", """
name: conventions
description: Always-on Compose rules
platforms: [compose]
when: {always: true}
priority: 100
""", body="Hoist state.\n")
    write_skill(root, "forms", """
name: forms
description: Forms with inputs
when:
  components: [TextField]
  min_count: {TextField: 2}
forbidden_patterns: ['\\bisNotBlank\\(']
priority: 60
""", body="Submit enabled from state.\n")
    write_skill(root, "rows", """
name: rows
description: Repeated option rows
when: {repeated_rows: true}
forbidden_patterns: ['option1Checked']
""", body="One selected value.\n")
    write_skill(root, "manual", """
name: manual
description: Only loaded on request
""", body="Manual rule.\n")
    return root


def field(name="Email"):
    return {"component": "TextField", "name": name, "binding": name.lower()}


def row(label="A", extra=None):
    children = [{"component": "Check Box", "name": "Box"}, {"type": "Text", "name": "Label", "text": label}]
    return {"type": "Row", "name": f"Option{label}", "children": children + (extra or [])}


def spec(*children):
    return {"screen": "S", "root": {"type": "Column", "name": "Root", "children": list(children)}}


def names(selected):
    return [s.name for s in selected]


# ---------------------------------------------------------------- 1. loading

def test_valid_skill_loads_with_defaults_and_examples(tmp_path):
    write_skill(tmp_path, "lists", """
name: lists
description: Lists
""", examples={"Feed.kt": "fun Feed() {}", "Feed.spec.json": '{"screen": "Feed"}', "Feed.swift": "struct Feed {}"})
    (s,) = load_skills(tmp_path)
    assert (s.name, s.platforms, s.priority, s.when) == ("lists", ["compose", "swiftui"], 50, None)
    assert s.body.startswith("Rule one.")
    (kt,) = s.examples_for("compose")
    assert kt.name == "Feed" and kt.spec == '{"screen": "Feed"}'
    assert s.examples_for("swiftui")[0].spec == '{"screen": "Feed"}'


@pytest.mark.parametrize("front, message", [
    ("name: bad\ndescription: [unclosed", "bad YAML"),
    ("name: other\ndescription: x", "must equal the folder name"),
    ("name: bad\ndescription: x\nwhen: {component: [A]}", "unknown `when` key"),
    ("name: bad\ndescription: x\nforbidden_patterns: ['(']", "invalid regex"),
    ("name: bad\ndescription: x\nwhen: {kinds: [input], min_count: {TextField: 2}}", "must also be listed"),
    ("name: bad\ndescription: x\nplatforms: [android]", "unknown platform"),
])
def test_invalid_skill_is_skipped_with_a_warning(tmp_path, front, message):
    write_skill(tmp_path, "good", "name: good\ndescription: fine")
    write_skill(tmp_path, "bad", front)
    with pytest.warns(SkillWarning, match=message):
        skills = load_skills(tmp_path)
    assert names(skills) == ["good"]


def test_missing_skill_md_and_missing_dir(tmp_path):
    (tmp_path / "empty").mkdir()
    skills, errors = scan_skills(tmp_path)
    assert skills == [] and errors[0].name == "empty" and "missing SKILL.md" in errors[0].message
    assert scan_skills(tmp_path / "nope") == ([], []) and scan_skills(None) == ([], [])


# ---------------------------------------------------------------- 2. selection

def test_always_skill_only_on_its_platform(skills_dir, ds):
    skills = load_skills(skills_dir)
    assert names(select_skills(skills, spec(), "compose", ds)) == ["conventions"]
    assert names(select_skills(skills, spec(), "swiftui", ds)) == []


def test_components_with_min_count_and_reasons(skills_dir, ds):
    skills = load_skills(skills_dir)
    one = select_skills(skills, spec(field()), "compose", ds)
    assert names(one) == ["conventions"]
    two = select_skills(skills, spec(field("Email"), field("Password")), "compose", ds)
    assert [(s.name, s.reason) for s in two] == [("conventions", "always"), ("forms", "components: TextField ×2")]


def test_kinds_condition_and_skill_without_when(tmp_path, ds):
    write_skill(tmp_path, "inputs", "name: inputs\ndescription: x\nwhen: {kinds: [input], min_count: {input: 3}}")
    skills = load_skills(tmp_path)
    assert select_skills(skills, spec(field(), field("B")), "compose", ds) == []
    (s,) = select_skills(skills, spec(field(), field("B"), row()), "compose", ds)  # Check Box is an input too
    assert s.reason == "kinds: input ×3"


# ---------------------------------------------------------------- 3. repeated rows

def test_repeated_rows():
    assert repeated_rows(spec(row("A"), row("B"), row("C"), row("D"))["root"]) == ("Root", 4)
    assert repeated_rows(spec(row("A"), row("B"))["root"]) is None
    different = spec(row("A"), row("B", [{"type": "Text", "name": "Hint", "text": "x"}]),
                     row("C", [{"component": "Button", "name": "More"}]))
    assert repeated_rows(different["root"]) is None
    # three leaf Texts are not rows
    texts = spec(*[{"type": "Text", "name": f"T{i}", "text": "x"} for i in range(4)])
    assert repeated_rows(texts["root"]) is None


def test_repeated_rows_selects_skill_with_reason(skills_dir, ds):
    selected = select_skills(load_skills(skills_dir), spec(row("A"), row("B"), row("C")), "swiftui", ds)
    assert [(s.name, s.reason) for s in selected] == [("rows", "repeated_rows: 3 in Root")]


# ---------------------------------------------------------------- 4. budget

def test_budget_drops_lowest_priority_first(skills_dir, ds):
    skills = load_skills(skills_dir)
    s = spec(field("A"), field("B"), row("A"), row("B"), row("C"))
    full = plan_skills(skills, s, "compose", ds)
    assert names(full.included) == ["conventions", "forms", "rows"] and full.dropped == []
    budget = len(full.included[0].text) + len(full.included[1].text)
    plan = plan_skills(skills, s, "compose", ds, max_chars=budget)
    assert names(plan.included) == ["conventions", "forms"] and names(plan.dropped) == ["rows"]
    assert plan.chars <= budget
    assert plan.trace() == {"skills": [{"name": "conventions", "reason": "always"},
                                       {"name": "forms", "reason": "components: TextField ×2"}],
                            "skills_dropped": ["rows"], "skill_chars": plan.chars}


def test_forced_skill_is_never_dropped_and_unknown_is_an_error(skills_dir, ds):
    skills = load_skills(skills_dir)
    plan = plan_skills(skills, spec(), "compose", ds, max_chars=1, forced=["manual"])
    assert names(plan.included) == ["manual"] and names(plan.dropped) == ["conventions"]
    assert plan.included[0].selected.reason == "--skill"
    with pytest.raises(ValueError, match="unknown skill 'nope'"):
        plan_skills(skills, spec(), "compose", ds, forced=["nope"])
    with pytest.raises(ValueError, match="not swiftui"):
        plan_skills(skills, spec(), "swiftui", ds, forced=["conventions"])


def test_skill_examples_take_precedence_within_max_examples(tmp_path, ds):
    write_skill(tmp_path / "skills", "a", "name: a\ndescription: x\nwhen: {always: true}",
                examples={"A1.kt": "fun A1() {}", "A2.kt": "fun A2() {}", "A3.kt": "fun A3() {}"})
    skills = load_skills(tmp_path / "skills")
    plan = plan_skills(skills, spec(), "compose", ds, max_examples=2)
    assert plan.examples_used == 2
    assert "fun A2()" in plan.blocks()[0] and "fun A3()" not in plan.blocks()[0]


def test_global_examples_fill_the_remaining_slots(tmp_path, login):
    real, result = login
    write_skill(tmp_path / "skills", "a", "name: a\ndescription: x\nwhen: {always: true}",
                examples={"A1.kt": "fun A1() {}"})
    ex = tmp_path / "examples" / "compose"
    ex.mkdir(parents=True)
    for n in ("G1", "G2"):
        (ex / f"{n}.spec.json").write_text("{}")
        (ex / f"{n}.kt").write_text(f"fun {n}() {{}}")
    plan = plan_skills(load_skills(tmp_path / "skills"), result.spec, "compose", real, max_examples=2)
    client = FakeClient([(GOOD, None)])
    cfg = Config(trace_dir=None, examples_dir=str(tmp_path / "examples"), max_examples=2, use_tools=False)
    CodegenAgent(cfg, real, client=client).run(result, "compose", "L", skills=plan)
    prompt = client.requests[0]["messages"][1]["content"]
    assert "fun A1()" in prompt and "fun G1()" in prompt and "fun G2()" not in prompt


# ---------------------------------------------------------------- 5. prompt

@pytest.fixture
def login():
    real = DesignSystem.load(ROOT / "manifest/components.json", ROOT / "manifest/tokens.json")
    return real, simplify(real, load_node_from_file(str(FIXTURE)))


EXAMPLE = [('{"screen": "Ex"}', "fun Ex() {}\n")]


@pytest.mark.parametrize("platform", ["compose", "swiftui"])
def test_prompt_without_skills_is_unchanged(login, platform):
    real, result = login
    golden = (GOLDEN / f"login_{platform}_user.txt").read_text()
    for conventions in (None, []):
        assert user_prompt(real, result, platform, "LoginScreen", "com.company.login", EXAMPLE,
                           conventions) == golden
    assert user_prompt(real, result, platform, "LoginScreen", "com.company.login", EXAMPLE) == golden
    assert system_prompt(platform) == system_prompt(platform, []) == (GOLDEN / f"{platform}_system.txt").read_text()


def test_prompt_with_skills_puts_conventions_before_task_in_priority_order(login, skills_dir):
    real, result = login
    plan = plan_skills(load_skills(skills_dir), result.spec, "compose", real, forced=["manual"])
    p = user_prompt(real, result, "compose", "LoginScreen", None, [], plan.blocks())
    conv, task = p.index("## Team conventions for this screen\n"), p.index("## Task\n")
    order = [p.index(f"### {n} — ") for n in ("conventions", "forms", "manual")]
    assert conv < order[0] < order[1] < order[2] < task
    assert "### forms — Forms with inputs\nSubmit enabled from state." in p


# ---------------------------------------------------------------- 6. lint

def test_selected_skill_patterns_name_the_skill(skills_dir, ds):
    skills = load_skills(skills_dir)
    plan = plan_skills(skills, spec(field("A"), field("B")), "compose", ds)
    code = "val ok = state.email.isNotBlank()\nval option1Checked = true\n"
    issues = lint(code, "compose", None, plan.patterns())
    assert issues == ["line 1: forbidden by skill 'forms': \\bisNotBlank\\(: `val ok = state.email.isNotBlank()`"]


def test_agent_lints_with_skill_patterns_and_traces_skills(skills_dir, ds, login, tmp_path):
    real, result = login
    plan = plan_skills(load_skills(skills_dir), result.spec, "compose", real, forced=["forms"])
    bad = "```kotlin\nfun LoginScreen() { UiButton(enabled = state.email.isNotBlank()) }\n```"
    client = FakeClient([(bad, None), (GOOD, None)])
    cfg = Config(trace_dir=str(tmp_path), examples_dir=None, use_tools=False)
    res = CodegenAgent(cfg, real, client=client).run(result, "compose", "LoginScreen", skills=plan)
    assert res.ok and res.rounds == 2
    assert "forbidden by skill 'forms'" in client.requests[1]["messages"][-1]["content"]
    assert "## Team conventions for this screen" in client.requests[0]["messages"][1]["content"]
    start = json.loads(Path(res.trace_path).read_text().splitlines()[0])
    assert start["event"] == "start" and start["skills_dropped"] == [] and start["skill_chars"] == plan.chars
    assert [s["name"] for s in start["skills"]] == ["conventions", "forms"]


# ---------------------------------------------------------------- 7. load_skill tool

def test_load_skill_tool(skills_dir, ds):
    plan = plan_skills(load_skills(skills_dir), spec(), "compose", ds)
    assert "load_skill" not in {s["function"]["name"] for s in Tools(ds, "compose").schemas()}
    loader = SkillLoader(plan)
    tools = Tools(ds, "compose", skill_loader=loader)
    assert "load_skill" in {s["function"]["name"] for s in tools.schemas()}
    assert [s.name for s in loader.catalog()] == ["forms", "manual", "rows"]
    text = tools.dispatch("load_skill", {"name": "manual"})
    assert text.startswith("### manual — Only loaded on request\nManual rule.")
    assert loader.chars == plan.chars + len(text)
    assert "already included" in tools.dispatch("load_skill", {"name": "conventions"})
    assert "already loaded" in tools.dispatch("load_skill", {"name": "manual"})


def test_load_skill_respects_the_budget(skills_dir, ds):
    plan = plan_skills(load_skills(skills_dir), spec(), "compose", ds, max_chars=60)
    out = SkillLoader(plan).load("forms")
    assert out.startswith("error:") and "budget" in out


def test_agent_offers_load_skill_only_when_enabled(skills_dir, ds, login, tmp_path):
    real, result = login
    plan = plan_skills(load_skills(skills_dir), result.spec, "compose", real)
    off = FakeClient([(GOOD, None)])
    CodegenAgent(Config(trace_dir=None, examples_dir=None), real, client=off).run(result, "compose", "L", skills=plan)
    assert "load_skill" not in off.requests[0]["messages"][0]["content"]
    assert "load_skill" not in {t["function"]["name"] for t in off.requests[0]["tools"]}

    on = FakeClient([(None, [call("load_skill", {"name": "rows"})]), (GOOD, None)])
    cfg = Config(trace_dir=str(tmp_path), examples_dir=None, skill_tool=True)
    res = CodegenAgent(cfg, real, client=on).run(result, "compose", "L", skills=plan)
    system = on.requests[0]["messages"][0]["content"]
    assert "- rows: Repeated option rows" in system and "- conventions:" not in system
    assert on.requests[1]["messages"][-1]["content"].startswith("### rows — ")
    tool_events = [json.loads(l) for l in Path(res.trace_path).read_text().splitlines()]
    assert any(e["event"] == "tool" and e["name"] == "load_skill" for e in tool_events)


# ---------------------------------------------------------------- 8. CLI

def test_cli_skills_lists_and_fails_on_invalid(monkeypatch, tmp_path, capsys, skills_dir):
    monkeypatch.setenv("CODEGEN_SKILLS_DIR", str(skills_dir))
    assert _run_cli(monkeypatch, tmp_path, ["skills"]) == 0
    out = capsys.readouterr().out
    assert all(n in out for n in ("conventions", "forms", "rows", "manual")) and "0 invalid" in out
    write_skill(skills_dir, "broken", "name: wrong\ndescription: x")
    assert _run_cli(monkeypatch, tmp_path, ["skills"]) == 1
    out = capsys.readouterr().out
    assert "broken" in out and "invalid:" in out and "1 invalid" in out


def _generate(monkeypatch, tmp_path, skills_dir, *extra):
    import openai
    client = FakeClient([(GOOD, None)])
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: client)
    monkeypatch.setenv("CODEGEN_SKILLS_DIR", str(skills_dir))
    rc = _run_cli(monkeypatch, tmp_path, ["generate", "--figma-json", str(FIXTURE), "--platform", "compose",
                                          "--out", "L.kt", "--no-tools", *extra])
    return rc, client


def test_cli_generate_prints_and_uses_selected_skills(monkeypatch, tmp_path, capsys, skills_dir):
    rc, client = _generate(monkeypatch, tmp_path, skills_dir)
    assert rc == 0
    assert "skills: conventions (always), forms (components: TextField ×2)" in capsys.readouterr().out
    assert "### forms — " in client.requests[0]["messages"][1]["content"]


def test_cli_no_skills_and_forced_skill(monkeypatch, tmp_path, capsys, skills_dir):
    rc, client = _generate(monkeypatch, tmp_path, skills_dir, "--no-skills")
    assert rc == 0 and "skills: none (auto-selection off" in capsys.readouterr().out
    assert "Team conventions" not in client.requests[0]["messages"][1]["content"]

    rc, client = _generate(monkeypatch, tmp_path, skills_dir, "--no-skills", "--skill", "manual")
    assert rc == 0 and "skills: manual (--skill)" in capsys.readouterr().out
    assert "### manual — " in client.requests[0]["messages"][1]["content"]

    rc, _ = _generate(monkeypatch, tmp_path, skills_dir, "--skill", "nope")
    assert rc == 2 and "unknown skill 'nope'" in capsys.readouterr().out


def test_cli_spec_shows_selection(monkeypatch, tmp_path, capsys, skills_dir):
    monkeypatch.setenv("CODEGEN_SKILLS_DIR", str(skills_dir))
    assert _run_cli(monkeypatch, tmp_path, ["spec", "--figma-json", str(FIXTURE)]) == 0
    err = capsys.readouterr().err
    assert "skills (compose): conventions (always), forms (components: TextField ×2)" in err
    assert "skills (swiftui): forms (components: TextField ×2)" in err
