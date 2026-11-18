"""Spec diff (codegen/specdiff.py) and `figma-codegen diff`. Synthetic specs only."""
import copy
import json

import pytest

from codegen.baseline import FigmaRef, add_header, write_baseline
from codegen.figma_client import load_node_from_file
from codegen.simplify import simplify, without_ids
from codegen.specdiff import diff_specs, render_changes

from test_baseline import CFG
from test_pipeline import FIXTURE, URL, _run_cli, ds  # noqa: F401  (fixture)


def text(name, txt, id, **kw):
    return {"type": "Text", "name": name, "id": id, "text": txt, "style": "typography.body/m", **kw}


def comp(component, name, id, **kw):
    return {"component": component, "name": name, "id": id, **kw}


def base():
    return {"screen": "LoginScreen", "root": {
        "type": "Column", "name": "Login", "id": "0:1", "gap": "spacing.m", "children": [
            text("Title", "Welcome", "1:1", style="typography.heading/m"),
            {"type": "Column", "name": "Form", "id": "1:2", "gap": "spacing.s", "children": [
                comp("TextField", "EmailField", "2:1", binding="email", props={"label": "Email"}),
                comp("TextField", "PasswordField", "2:2", binding="password", props={"label": "Password"}),
            ]},
            {"type": "Spacer", "id": "1:3", "weight": 1},
            comp("Button", "LoginButton", "1:4", event="onLogin", props={"style": "Primary", "text": "Log in"}),
        ]}}


def edit(fn):
    new = copy.deepcopy(base())
    fn(new["root"])
    return new


def kinds(changes):
    return [c.kind for c in changes]


def test_identical_specs_no_changes():
    assert diff_specs(base(), base()) == []
    assert render_changes([]) == "no changes"


def test_token_text_and_prop_changes():
    def f(r):
        r["children"][0]["style"] = "typography.heading/l"
        r["children"][0]["text"] = "Welcome back"
        r["children"][1]["gap"] = "spacing.m"
        r["children"][3]["props"]["style"] = "Secondary"
        r["children"][3]["props"]["text"] = "Sign in"
    ch = diff_specs(base(), edit(f))
    assert kinds(ch) == ["text_changed", "token_changed", "token_changed", "prop_changed", "prop_changed"]
    assert ch[1].path == "LoginScreen > Title" and ch[1].detail == {
        "key": "style", "old": "typography.heading/m", "new": "typography.heading/l"}
    assert ch[2].path == "LoginScreen > Form" and ch[2].severity == "visual"
    assert [c.severity for c in ch[3:]] == ["visual", "content"]   # style is visual, button copy is content
    out = render_changes(ch).splitlines()
    assert out[0] == "5 changes"
    assert out[1] == "visual     Title: style typography.heading/m → typography.heading/l"
    assert out[2] == "visual     Form: gap spacing.s → spacing.m"
    assert out[3] == "visual     LoginButton: style Primary → Secondary"
    assert out[4] == 'content    Title: text "Welcome" → "Welcome back"'
    assert out[5] == 'content    LoginButton: text "Log in" → "Sign in"'


def test_layout_changes():
    def f(r):
        r["children"][1]["type"] = "Row"
        r["children"][1]["arrangement"] = "spaceBetween"
        r["children"][1]["children"][0]["fill"] = "maxWidth"
    ch = diff_specs(base(), edit(f))
    assert kinds(ch) == ["layout_changed"] * 3
    assert ch[0].detail == {"key": "type", "old": "Column", "new": "Row"} and ch[0].severity == "structure"
    assert ch[1].detail["key"] == "arrangement" and ch[1].severity == "visual"
    assert ch[2].node_name == "EmailField" and ch[2].detail == {"key": "fill", "old": None, "new": "maxWidth"}


def test_node_added_is_one_change_with_position():
    def f(r):
        r["children"][1]["children"].append(
            comp("Link", "ForgotPasswordLink", "2:3", event="onForgotPassword", props={"text": "Forgot password?"}))
        r["children"].insert(0, {"type": "Row", "name": "Header", "id": "5:1",
                                 "children": [text("Back", "Back", "5:2"), text("Step", "1/3", "5:3")]})
    ch = diff_specs(base(), edit(f))
    assert kinds(ch) == ["node_added", "node_added"]
    assert ch[0].detail["summary"] == 'Row "Header" (+2 nested)' and ch[0].detail["position"] == "first in LoginScreen"
    assert render_changes(ch).splitlines()[2] == \
        'structure  + added Link "ForgotPasswordLink" — "Forgot password?" after PasswordField'
    assert all(c.severity == "structure" for c in ch)


def test_node_removed_subtree_is_one_change():
    ch = diff_specs(base(), edit(lambda r: r["children"].pop(1)))
    assert kinds(ch) == ["node_removed"]
    assert ch[0].path == "LoginScreen > Form" and ch[0].detail == {
        "summary": 'Column "Form" (+2 nested)', "parent": "LoginScreen"}


def test_subtree_move_to_other_parent_is_one_change():
    def f(r):
        form = r["children"].pop(1)
        r["children"].insert(0, {"type": "Box", "name": "Card", "id": "7:1", "background": "color.surface",
                                 "children": [form]})
    ch = diff_specs(base(), edit(f))
    assert kinds(ch) == ["node_added", "node_moved"]   # the new Card, then Form (with its children) moved into it
    assert ch[1].detail["old_parent"] == "LoginScreen" and ch[1].detail["new_parent"] == "LoginScreen > Card"


def test_reorder_reports_only_the_node_that_moved():
    def f(r):
        r["children"].insert(0, r["children"].pop(3))   # button to the top
    ch = diff_specs(base(), edit(f))
    assert kinds(ch) == ["node_moved"]
    assert ch[0].node_name == "LoginButton" and ch[0].detail["position"] == "first in LoginScreen"


def test_insert_is_not_a_move_of_later_siblings():
    def f(r):
        r["children"].insert(1, text("Subtitle", "Hi", "9:1"))
    assert kinds(diff_specs(base(), edit(f))) == ["node_added"]


def test_component_swapped():
    def f(r):
        r["children"][1]["children"][0] = comp("SearchField", "EmailField", "2:1", binding="email")
    ch = diff_specs(base(), edit(f))
    assert kinds(ch) == ["component_swapped"]
    assert ch[0].detail["old"] == "TextField" and ch[0].detail["new"] == "SearchField"
    assert render_changes(ch).splitlines()[1] == "structure  EmailField: TextField → SearchField"


def test_component_swapped_without_ids_matches_by_name():
    old, new = without_ids(base()), without_ids(base())
    new["root"]["children"][3]["component"] = "IconButton"
    assert kinds(diff_specs(old, new)) == ["component_swapped"]


def test_binding_and_event_changes_are_structure():
    def f(r):
        r["children"][1]["children"][0]["binding"] = "userEmail"
        r["children"][3]["event"] = "onSignIn"
    ch = diff_specs(base(), edit(f))
    assert kinds(ch) == ["binding_changed", "event_changed"]
    assert {c.severity for c in ch} == {"structure"}


def test_rename_only_is_matched_by_id():
    def f(r):
        r["children"][0]["name"] = "Heading"
        r["children"][1]["children"][0]["name"] = "MailField"
    assert diff_specs(base(), edit(f)) == []


def test_rename_without_ids_is_removed_plus_added():
    old = without_ids(base())
    new = without_ids(edit(lambda r: r["children"][0].update(name="Heading")))
    assert kinds(diff_specs(old, new)) == ["node_added", "node_removed"]


def test_ordering_is_stable_and_follows_the_tree():
    def f(r):
        r["children"][3]["props"]["style"] = "Secondary"
        r["children"].pop(2)                                       # the spacer
        r["children"][0]["style"] = "typography.heading/l"
        r["children"][1]["children"][1]["props"]["label"] = "PIN"
    runs = [[c.to_json() for c in diff_specs(base(), edit(f))] for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
    assert [(c["kind"], c["node_name"]) for c in runs[0]] == [
        ("token_changed", "Title"), ("prop_changed", "PasswordField"),
        ("node_removed", "Spacer"), ("prop_changed", "LoginButton")]


def test_ambiguous_names_use_the_path():
    old = {"screen": "S", "root": {"type": "Column", "name": "S", "id": "0", "children": [
        {"type": "Row", "name": "A", "id": "1", "children": [text("Label", "x", "11")]},
        {"type": "Row", "name": "B", "id": "2", "children": [text("Label", "y", "21")]}]}}
    new = copy.deepcopy(old)
    new["root"]["children"][0]["children"][0]["text"] = "x2"
    new["root"]["children"][1]["children"][0]["text"] = "y2"
    lines = render_changes(diff_specs(old, new)).splitlines()
    assert lines[1] == 'content    S > A > Label: text "x" → "x2"'


def test_simplified_fixture_against_itself(ds):  # noqa: F811
    spec = simplify(ds, load_node_from_file(str(FIXTURE))).spec
    assert all("id" in c for c in spec["root"]["children"])
    assert diff_specs(spec, copy.deepcopy(spec)) == []


# --- CLI ---------------------------------------------------------------------------------------

def _baseline_for_fixture(tmp_path, ds, mutate=None, ref=FigmaRef("AbC123", "12:300", URL)):  # noqa: F811
    spec = simplify(ds, load_node_from_file(str(FIXTURE))).spec
    if mutate:
        mutate(spec["root"])
    code = tmp_path / "LoginScreen.kt"
    code.write_text(add_header("package x\n", ref, code))
    write_baseline(code, spec, ref, "compose", "LoginScreen", "x", CFG, "m")
    return code


def test_cli_diff_up_to_date_uses_baseline_url(monkeypatch, tmp_path, capsys, ds):  # noqa: F811
    _baseline_for_fixture(tmp_path, ds)
    assert _run_cli(monkeypatch, tmp_path, ["diff", "LoginScreen.kt"]) == 0
    assert capsys.readouterr().out.strip() == "no changes"


def test_cli_diff_reports_changes(monkeypatch, tmp_path, capsys, ds):  # noqa: F811
    def older(root):
        root["gap"] = "spacing.s"
        root["children"][0]["text"] = "Hello"
    _baseline_for_fixture(tmp_path, ds, older)
    assert _run_cli(monkeypatch, tmp_path, ["diff", "LoginScreen.kt", "--figma-json", str(FIXTURE)]) == 10
    out = capsys.readouterr().out.splitlines()
    assert out == ["2 changes", "visual     LoginScreen: gap spacing.s → spacing.m",
                   'content    Title: text "Hello" → "Welcome back"']


def test_cli_diff_errors(monkeypatch, tmp_path, capsys, ds):  # noqa: F811
    (tmp_path / "Hand.kt").write_text("x")
    assert _run_cli(monkeypatch, tmp_path, ["diff", "Hand.kt"]) == 2
    assert "run `figma-codegen generate` first" in capsys.readouterr().out
    _baseline_for_fixture(tmp_path, ds, ref=FigmaRef())
    assert _run_cli(monkeypatch, tmp_path, ["diff", "LoginScreen.kt"]) == 2
    assert "baseline has no Figma URL" in capsys.readouterr().out


def test_cli_diff_specs(monkeypatch, tmp_path, capsys):
    (tmp_path / "old.json").write_text(json.dumps(base()))
    (tmp_path / "new.json").write_text(json.dumps(edit(lambda r: r["children"][1].update(gap="spacing.l"))))
    assert _run_cli(monkeypatch, tmp_path, ["diff", "--specs", "old.json", "new.json"]) == 10
    assert "Form: gap spacing.s → spacing.l" in capsys.readouterr().out
    assert _run_cli(monkeypatch, tmp_path, ["diff", "--specs", "old.json", "old.json"]) == 0
    (tmp_path / "bad.json").write_text("[1]")
    assert _run_cli(monkeypatch, tmp_path, ["diff", "--specs", "old.json", "bad.json"]) == 2
