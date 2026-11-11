"""Readiness score (codegen/readiness.py), ignored device layers, `figma-codegen readiness`."""
import copy
import json

import pytest

from codegen.config import load_config
from codegen.simplify import FigmaWarning, SimplifyResult, simplify
from codegen.readiness import compute, level_for, render_html, render_text

from test_gaps import RAW, _find, mutated
from test_pipeline import URL, _run_cli, ds  # noqa: F401  (fixture)


def entry(data):
    return next(iter(data["nodes"].values()))


def clean():
    """The login frame without its two problem layers (Decoration, PromoBanner)."""
    data = copy.deepcopy(RAW)
    doc = entry(data)["document"]
    doc["children"] = [c for c in doc["children"] if c["name"] not in ("Decoration", "PromoBanner")]
    return data


def with_device_layers(data):
    """Adds a status bar, a home indicator and a '_guides' layer full of raw values."""
    data = copy.deepcopy(data)
    doc = entry(data)["document"]
    raw = lambda name, id: {"id": id, "name": name, "type": "FRAME", "layoutMode": "HORIZONTAL",
                            "itemSpacing": 13, "fills": [{"type": "SOLID", "color": {"r": 0.3, "g": 0.2, "b": 0.1}}],
                            "children": [{"id": id + "1", "name": "9:41", "type": "TEXT", "characters": "9:41",
                                          "style": {"fontSize": 17}},
                                         {"id": id + "2", "name": "Vector", "type": "VECTOR"}]}
    doc["children"].insert(0, raw("iOS Status Bar", "50:1"))
    doc["children"].append(raw("Home Indicator", "50:2"))
    doc["children"].append(raw("_guides", "50:3"))
    return data


def test_clean_frame_is_ready(ds):  # noqa: F811
    r = compute(simplify(ds, entry(clean())))
    assert (r.score, r.level, r.groups) == (100, "ready", [])
    assert "No issues" in render_text(r, "LoginScreen")


def test_unmapped_components_and_raw_colors_lower_the_level(ds):  # noqa: F811
    r = compute(simplify(ds, entry(mutated())))
    assert r.level in ("almost", "needs work") and r.score < 85
    issues = [g.issue for g in r.groups]
    assert issues[0] == "missing_component"                      # most points first
    assert {"unknown_variant", "unbound_style", "value_without_token", "hand_drawn_graphic"} <= set(issues)
    missing = r.groups[0]
    assert missing.count == 2 and missing.points == pytest.approx(100 * 6 * 2 / (6 * r.node_count))


def test_score_formula_and_levels():
    spec = {"root": {"type": "Column", "children": [{"type": "Text"}] * 9}}      # 10 nodes
    issues = [FigmaWarning("unknown_component", "A", "1:1", ""), FigmaWarning("default_name", "B", "1:2", "")]
    r = compute(SimplifyResult(spec, issues=issues))
    assert r.node_count == 10 and r.score == round(100 - 100 * (6 + 1) / 60)   # 88
    assert r.level == "ready"
    assert compute(SimplifyResult(spec, issues=issues), {"default_name": 0}).score == 90   # weight 0: not counted
    assert [level_for(s) for s in (100, 85, 84, 60, 59, 0)] == ["ready", "ready", "almost", "almost",
                                                                "needs work", "needs work"]
    many = [FigmaWarning("no_auto_layout", "X", f"2:{i}", "") for i in range(30)]
    assert compute(SimplifyResult(spec, issues=many)).score == 0                 # floored


def test_ignored_layers_affect_neither_score_nor_spec(ds):  # noqa: F811
    plain = simplify(ds, entry(mutated()))
    decorated = simplify(ds, entry(with_device_layers(mutated())))
    assert decorated.spec == plain.spec and decorated.warnings == plain.warnings
    assert compute(decorated).score == compute(plain).score
    assert [n["name"] for n in decorated.ignored] == ["iOS Status Bar", "Home Indicator", "_guides"]
    # an empty ignore list keeps everything
    kept = simplify(ds, entry(with_device_layers(mutated())), ignore_layers=[])
    assert len(kept.warnings) > len(plain.warnings) and kept.ignored == []


def test_text_report_links_and_fix_hints(ds):  # noqa: F811
    r = compute(simplify(ds, entry(mutated())))
    text = render_text(r, "LoginScreen", "AbC123")
    assert text.startswith(f"Readiness: {r.level} ({r.score}/100) — LoginScreen, {r.node_count} layers")
    assert "2 components the app doesn't have" in text
    assert "Fix in Figma: Swap them for a component from the UIKit library" in text
    assert "· PromoBanner  https://www.figma.com/design/AbC123?node-id=12-370" in text


def test_html_page(ds):  # noqa: F811
    r = compute(simplify(ds, entry(mutated())))
    page = render_html(r, "LoginScreen", "AbC123", URL)
    assert page.startswith("<!doctype html>") and f"<b>{r.score}</b>" in page
    assert 'href="https://www.figma.com/design/AbC123?node-id=12-370"' in page
    assert "Fix in Figma:" in page and "prefers-color-scheme: dark" in page
    assert "<script" not in page                                          # static, self-contained


def test_weights_and_ignore_layers_from_config(tmp_path):
    (tmp_path / "codegen.yaml").write_text(
        "readiness:\n  weights: {missing_component: 1}\n  ignore_layers: ['*Debug*']\n")
    cfg = load_config(str(tmp_path / "codegen.yaml"), env={})
    assert cfg.readiness_weights["missing_component"] == 1 and cfg.readiness_weights["no_auto_layout"] == 6
    assert cfg.ignore_layers == ["*Debug*"]
    (tmp_path / "bad.yaml").write_text("readiness:\n  weights: {colour: 2}\n")
    with pytest.raises(ValueError, match="unknown issue"):
        load_config(str(tmp_path / "bad.yaml"), env={})


# --- CLI ---------------------------------------------------------------------------------------

def test_cli_readiness_min_score_and_html(monkeypatch, tmp_path, capsys):
    (tmp_path / "frame.json").write_text(json.dumps(mutated()))
    rc = _run_cli(monkeypatch, tmp_path, ["readiness", URL, "--figma-json", "frame.json", "--html", "r/page.html"])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("Readiness: ")
    assert (tmp_path / "r/page.html").read_text().startswith("<!doctype html>")
    assert _run_cli(monkeypatch, tmp_path, ["readiness", "--figma-json", "frame.json", "--min-score", "95"]) == 2
    assert "below --min-score 95" in capsys.readouterr().out
    assert _run_cli(monkeypatch, tmp_path, ["readiness", "--figma-json", "frame.json", "--min-score", "1"]) == 0


def test_cli_readiness_json(monkeypatch, tmp_path, capsys):
    (tmp_path / "frame.json").write_text(json.dumps(with_device_layers(mutated())))
    rc = _run_cli(monkeypatch, tmp_path, ["--json", "readiness", URL, "--figma-json", "frame.json",
                                          "--min-score", "95"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 2 and data["status"] == "failed" and data["min_score"] == 95
    assert data["level"] in ("almost", "needs work") and 0 <= data["score"] < 95
    first = data["issues"][0]
    assert first["issue"] == "missing_component" and first["fix_in_figma"].startswith("Swap them")
    assert first["nodes"][0]["url"] == "https://www.figma.com/design/AbC123?node-id=12-370"
    assert [n["name"] for n in data["ignored_layers"]] == ["iOS Status Bar", "Home Indicator", "_guides"]


def test_cli_ignore_layers_apply_to_spec_too(monkeypatch, tmp_path, capsys):
    (tmp_path / "frame.json").write_text(json.dumps(with_device_layers(clean())))
    assert _run_cli(monkeypatch, tmp_path, ["spec", "--figma-json", "frame.json"]) == 0
    out = capsys.readouterr()
    assert "Status Bar" not in out.out and "Home Indicator" not in out.out
    assert "simplifying — no warnings" in out.err
