"""Design-system gap report (codegen/gaps.py), --gaps / --gaps-json, and gap changes in `update`."""
import copy
import json

import pytest

from codegen.baseline import FigmaRef, add_header, write_baseline
from codegen.figma_client import load_node_from_file
from codegen.gaps import blocking_node_ids, collect_gaps, render_json, render_markdown
from codegen.simplify import simplify

from test_baseline import CFG
from test_pipeline import FIXTURE, URL, _run_cli, ds  # noqa: F401  (fixture)
from test_update import BODY, _update, fenced

RAW = json.loads(FIXTURE.read_text())


def _find(node, name):
    if node.get("name") == name:
        return node
    for c in node.get("children", []):
        hit = _find(c, name)
        if hit:
            return hit
    return None


def mutated(*, variant=True, banner=True, tokens=True, title=None):
    """The login frame with design-system gaps added (and optionally a new title text)."""
    data = copy.deepcopy(RAW)
    entry = next(iter(data["nodes"].values()))
    doc = entry["document"]
    if variant:
        _find(doc, "LoginButton")["componentProperties"]["Style#1:0"]["value"] = "Tertiary"
    if banner:
        second = copy.deepcopy(_find(doc, "PromoBanner"))
        second["id"] = "12:380"
        doc["children"].append(second)
    if tokens:
        doc["itemSpacing"] = 13
        t = _find(doc, "Title")
        t["style"] = {"fontSize": 27, "fontWeight": 700}
        t.pop("styles", None)
        sub = _find(doc, "Subtitle")
        sub.pop("styles", None)
        sub["fills"] = [{"type": "SOLID", "color": {"r": 0.07, "g": 0.1, "b": 0.16, "a": 1}}]
    if title:
        _find(doc, "Title")["characters"] = title
    return data


def gaps_of(ds, data):  # noqa: F811
    entry = next(iter(data["nodes"].values()))
    return collect_gaps(simplify(ds, entry).issues, ds)


def by_kind(gaps, kind):
    return [g for g in gaps if g.kind == kind]


def test_each_gap_kind(ds):  # noqa: F811
    gaps = gaps_of(ds, mutated())
    assert [g.kind for g in gaps][:2] == ["missing_component", "unknown_variant"]   # stable kind order

    (comp,) = by_kind(gaps, "missing_component")
    assert comp.name == "Promo Banner" and comp.key == "zzz" and comp.count == 2
    assert [n["id"] for n in comp.nodes] == ["12:370", "12:380"] and comp.owner == "design system team"

    (variant,) = by_kind(gaps, "unknown_variant")
    assert variant.name == "Button.Style = Tertiary" and variant.known == ["Primary", "Secondary", "Text"]
    assert variant.nodes == [{"name": "LoginButton", "id": "12:350"}]

    tokens = {g.name: g for g in by_kind(gaps, "missing_token")}
    assert tokens["spacing 13px"].nearest == "spacing.sm"   # 10px and 16px tie; the smaller wins
    assert tokens["typography 27px/700"].nearest is not None
    color = next(g for name, g in tokens.items() if name.startswith("color #"))
    assert color.nearest == "color.text.primary"                   # #121A29 is closest to #111827
    assert all(g.owner == "design system team" for g in tokens.values())

    (elem,) = by_kind(gaps, "non_library_element")
    assert elem.name == "Decoration" and elem.owner == "designer"


def test_clean_frame_has_only_its_known_gaps(ds):  # noqa: F811
    gaps = gaps_of(ds, mutated(variant=False, banner=False, tokens=False))
    assert [(g.kind, g.name) for g in gaps] == [("missing_component", "Promo Banner"),
                                               ("non_library_element", "Decoration")]
    assert blocking_node_ids(gaps) == {"12:370"}


def test_markdown_report_has_sections_owners_and_node_links(ds):  # noqa: F811
    md = render_markdown(gaps_of(ds, mutated()), "LoginScreen", "AbC123", URL)
    assert md.startswith("# Design-system gaps: LoginScreen\n\nFigma: " + URL)
    assert "1 missing component, 1 unsupported variant, 3 values without a token, 1 non-library element" in md
    for title in ("## Components missing from UIKit", "## Variants UIKit does not support",
                  "## Values without a token", "## Elements not from the library"):
        assert title in md
    assert "Owner: **designer**" in md and "Owner: **design system team**" in md
    assert ("- **Promo Banner** (key `zzz`), used 2×: [PromoBanner](https://www.figma.com/design/AbC123?node-id=12-370), "
            "[PromoBanner](https://www.figma.com/design/AbC123?node-id=12-380)") in md
    assert "(supported: Primary, Secondary, Text): [LoginButton](https://www.figma.com/design/AbC123?node-id=12-350)" in md
    assert "**spacing 13px**, nearest token `spacing.sm`" in md


def test_report_without_file_key_shows_node_ids(ds):  # noqa: F811
    md = render_markdown(gaps_of(ds, mutated(tokens=False)), "LoginScreen")
    assert "PromoBanner (`12:370`)" in md and "figma.com" not in md


def test_json_report(ds):  # noqa: F811
    data = json.loads(render_json(gaps_of(ds, mutated()), "LoginScreen", "AbC123", URL))
    assert data["schema"] == 1 and data["figma"] == {"file_key": "AbC123", "url": URL}
    comp = data["gaps"][0]
    assert comp["kind"] == "missing_component" and comp["count"] == 2
    assert comp["nodes"][0]["url"] == "https://www.figma.com/design/AbC123?node-id=12-370"


def test_spec_flag_writes_both_reports(monkeypatch, tmp_path, capsys):
    (tmp_path / "frame.json").write_text(json.dumps(mutated()))
    rc = _run_cli(monkeypatch, tmp_path, ["spec", URL, "--figma-json", "frame.json",
                                          "--gaps", "out/gaps.md", "--gaps-json", "out/gaps.json"])
    assert rc == 0
    assert "## Variants UIKit does not support" in (tmp_path / "out/gaps.md").read_text()
    assert json.loads((tmp_path / "out/gaps.json").read_text())["gaps"]
    out = capsys.readouterr()
    assert json.loads(out.out)["screen"] == "LoginScreen"   # stdout still pure JSON
    assert "gaps report: out/gaps.md (6 gaps)" in out.err


def test_diff_specs_rejects_gaps(monkeypatch, tmp_path, capsys):
    (tmp_path / "a.json").write_text(json.dumps({"root": {"type": "Column"}}))
    assert _run_cli(monkeypatch, tmp_path, ["diff", "--specs", "a.json", "a.json", "--gaps", "g.md"]) == 2
    assert "--gaps needs the Figma frame" in capsys.readouterr().out


# --- update: gap changes are deferred ------------------------------------------------------------

@pytest.fixture
def screen(tmp_path, ds):  # noqa: F811
    ref = FigmaRef("AbC123", "12:300", URL)
    spec = simplify(ds, load_node_from_file(str(FIXTURE))).spec
    code = tmp_path / "LoginScreen.kt"
    code.write_text(add_header(BODY, ref, code))
    write_baseline(code, spec, ref, "compose", "LoginScreen", "com.x.login", CFG, "m")
    return code


def test_update_defers_gap_changes_and_applies_the_rest(monkeypatch, tmp_path, capsys, screen):
    (tmp_path / "frame.json").write_text(json.dumps(mutated(tokens=False, title="Hello again")))
    before = screen.read_text()
    answer = before.replace('UiText("Welcome back"', 'UiText("Hello again"').replace(
        '        UiButton(text = "Log in"',
        '        // TODO(design-system): LoginButton style Primary → Tertiary\n        UiButton(text = "Log in"')
    answer += "// TODO(design-system): added PromoBanner\n"
    rc, client = _update(monkeypatch, tmp_path, [(fenced(answer), None)], "--figma-json", "frame.json",
                         "--apply", "--gaps", "gaps.md")
    assert rc == 0 and 'UiText("Hello again"' in screen.read_text()

    out = capsys.readouterr().out
    assert "needs a UIKit change first" in out
    deferred = out.split("needs a UIKit change first", 1)[1]
    assert "LoginButton: style Primary → Tertiary" in deferred and 'added Unmapped "PromoBanner"' in deferred

    prompt = client.requests[0]["messages"][1]["content"]
    to_apply = prompt.split("## Design changes to apply", 1)[1].split("##", 1)[0]
    assert 'Title: text "Welcome back" → "Hello again"' in to_apply
    assert "Tertiary" not in to_apply and "PromoBanner" not in to_apply
    not_now = prompt.split("## Do NOT implement: needs a UIKit change first", 1)[1].split("## Task", 1)[0]
    assert "TODO(design-system)" in not_now and "Tertiary" in not_now and "PromoBanner" in not_now
    assert "and add the TODO(design-system) comments" in prompt
    assert "## Variants UIKit does not support" in (tmp_path / "gaps.md").read_text()


def test_update_with_only_gap_changes_still_marks_them(monkeypatch, tmp_path, screen):
    (tmp_path / "frame.json").write_text(json.dumps(mutated(banner=False, tokens=False)))
    answer = screen.read_text() + "// TODO(design-system): LoginButton style Primary → Tertiary\n"
    rc, client = _update(monkeypatch, tmp_path, [(fenced(answer), None)], "--figma-json", "frame.json")
    assert rc == 0
    prompt = client.requests[0]["messages"][1]["content"]
    assert "## Design changes to apply" not in prompt
    assert "Only add the TODO(design-system) comments listed above" in prompt
