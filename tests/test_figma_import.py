import json
from pathlib import Path

import pytest

from codegen.cli import main
from codegen.design_system import DesignSystem
from codegen.figma_import import format_report, import_figma, load_entries
from codegen.simplify import simplify

FIX = Path(__file__).parent / "fixtures"
BANNER = FIX / "figma_banner_variant.json"      # one variant (State=Neutral), fetched on its own
BUTTON_SET = FIX / "figma_button_set.json"      # a whole component set with componentPropertyDefinitions
MANIFEST = Path(__file__).parent.parent / "manifest" / "components.json"
TOKENS = Path(__file__).parent.parent / "manifest" / "tokens.json"


def entries(*paths):
    return [(e, str(p)) for p in paths for e in load_entries(p)]


def comp(manifest, name):
    return next(c for c in manifest["components"] if c["figma"] == name)


def test_single_variant_gets_set_name_key_and_props_from_layer_references():
    res = import_figma(entries(BANNER), None)
    banner = comp(res.manifest, "Banner")
    assert banner["figmaKey"] == "00000000000000000000000000000000000000a6"   # the set's key, not the variant's
    props = banner["props"]
    assert props["state"] == {"figmaProp": "State", "values": ["Neutral"]}
    # exact short names, emoji included: that is what simplify reads from componentProperties
    assert props["title"] == {"figmaProp": "✏️ Title"}
    assert props["showTitle"] == {"figmaProp": "👁 Title"}
    assert props["captionIcon"] == {"figmaProp": "✅ Caption Icon"}
    assert props["show2ndButton"] == {"figmaProp": "👁 2nd Button"}
    assert "kind" not in banner and banner["todo"]
    assert "showCaption: Boolean = false" in banner["compose"]["signature"]     # hidden by default


def test_icons_and_private_components_are_skipped_nested_components_are_drafted():
    res = import_figma(entries(BANNER), None)
    assert {n for n, _ in res.skipped} == {"Info", "Star", ".Close"}
    button = comp(res.manifest, "Button")
    assert button["kind"] == "action"                       # ON_PRESS on the instance
    assert button["props"]["text"] == {"figmaProp": "✏️ Button Text"}   # 'Button' prefix dropped
    assert button["props"]["showIcon"]["map"] == {"False": False, "True": True}
    row = next(r for r in res.rows if r.figma == "Button")
    assert not row.defined and any("only as an instance" in r for r in row.review)


def test_component_set_definitions_give_all_values_enabled_map_and_kind():
    res = import_figma(entries(BUTTON_SET), None)
    button = comp(res.manifest, "Button")
    assert button["description"] == "Text button in three emphasis levels."
    assert button["kind"] == "action"
    assert button["props"]["type"]["values"] == ["Primary", "Secondary", "Tertiary"]
    assert button["props"]["enabled"] == {"figmaProp": "State",
                                          "map": {"Normal": True, "Pressed": True, "Disabled": False}}
    assert button["props"]["showSticky"] == {"figmaProp": "👁 Sticky"}
    assert "arrow-right" in {n for n, _ in res.skipped}           # icon behind the instance swap
    assert "onClick: () -> Unit" in button["compose"]["signature"]


def test_definitions_win_over_values_seen_in_instances():
    res = import_figma(entries(BANNER, BUTTON_SET), None)
    buttons = [c for c in res.manifest["components"] if c["figma"] == "Button"]
    assert len(buttons) == 1                                 # merged by set key
    assert buttons[0]["props"]["type"]["values"] == ["Primary", "Secondary", "Tertiary"]


def test_merge_keeps_code_updates_figma_side_and_reports_what_needs_a_human():
    base = json.loads(MANIFEST.read_text())
    res = import_figma(entries(BANNER, BUTTON_SET), base)
    button = comp(res.manifest, "Button")
    assert button["compose"] == comp(base, "Button")["compose"]        # code untouched
    assert button["figmaKey"] == "00000000000000000000000000000000000000a7"
    assert button["props"]["text"] == {"figmaProp": "✏️ Button Text"}  # figmaText 'Label' upgraded
    assert button["props"]["enabled"]["map"] == {"Default": True, "Disabled": False}   # a human's map is kept
    assert "todo" not in button
    row = next(r for r in res.rows if r.figma == "Button")
    assert row.status == "updated"
    assert any("'Normal'" in r and "'Pressed'" in r for r in row.review)
    assert any("'Style' is not a property of the component" in r for r in row.review)
    assert "TextField" in res.missing
    assert [c["figma"] for c in res.manifest["components"]][:len(base["components"])] == \
        [c["figma"] for c in base["components"]]            # order kept, new ones appended
    text = format_report(res)
    assert "### Button" in text and "Banner" in text and "## Skipped" in text


def test_merge_draws_new_code_in_the_base_manifests_style():
    res = import_figma(entries(BANNER), json.loads(MANIFEST.read_text()))
    banner = comp(res.manifest, "Banner")
    assert banner["compose"]["import"] == "com.company.uikit.UiBanner"
    assert banner["swiftui"]["signature"].startswith("UIKitBanner(")


def test_imported_props_round_trip_through_simplify():
    res = import_figma(entries(BANNER, BUTTON_SET), json.loads(MANIFEST.read_text()))
    ds = DesignSystem(res.manifest, json.loads(TOKENS.read_text()))
    screen = {
        "document": {"id": "1:1", "name": "Home", "type": "FRAME", "layoutMode": "VERTICAL", "children": [
            {"id": "2:1", "name": "Promo Banner", "type": "INSTANCE", "componentId": "417:2508",
             "componentProperties": {
                 "State": {"type": "VARIANT", "value": "Neutral"},
                 "✏️ Title#581:0": {"type": "TEXT", "value": "Hello"},
                 "👁 Caption#12377:0": {"type": "BOOLEAN", "value": True}}},
            {"id": "2:2", "name": "Retry Button", "type": "INSTANCE", "componentId": "1:410",
             "componentProperties": {
                 "Type": {"type": "VARIANT", "value": "Tertiary"},
                 "State": {"type": "VARIANT", "value": "Disabled"},
                 "👁 Icon": {"type": "VARIANT", "value": "True"},
                 "✏️ Button Text#227:0": {"type": "TEXT", "value": "Retry"}}},
        ]},
        "components": {"417:2508": {"key": "v", "name": "State=Neutral", "componentSetId": "417:2507"},
                       "1:410": {"key": "b1", "name": "Type=Tertiary", "componentSetId": "1:352"}},
        "componentSets": {"417:2507": {"key": "00000000000000000000000000000000000000a6", "name": "Banner"},
                          "1:352": {"key": "00000000000000000000000000000000000000a7", "name": "Button"}},
    }
    spec = simplify(ds, screen).spec
    banner, button = spec["root"]["children"]
    assert banner["component"] == "Banner"
    assert banner["props"] == {"state": "Neutral", "title": "Hello", "showCaption": True}
    assert button["props"] == {"type": "Tertiary", "enabled": False, "showIcon": True, "text": "Retry"}
    assert button["event"] == "onRetry"


def test_cli_writes_manifest_and_report_and_refuses_to_overwrite(tmp_path, capsys):
    out = tmp_path / "imported" / "components.json"
    args = ["import-figma", str(BANNER), str(BUTTON_SET), "--base", str(MANIFEST), "--out", str(out)]
    assert main(args) == 0
    manifest = json.loads(out.read_text())
    assert comp(manifest, "Banner")["todo"]
    assert (out.parent / "figma-import-report.md").exists()
    assert main(args) == 2
    assert "--force" in capsys.readouterr().err
    assert main(args + ["--force"]) == 0


def test_cli_updates_in_place_when_out_is_the_base_and_reads_directories(tmp_path, capsys):
    figma_dir = tmp_path / "figma"
    figma_dir.mkdir()
    (figma_dir / "banner.json").write_text(BANNER.read_text())
    (figma_dir / "notes.json").write_text("{}")            # not Figma: skipped, not fatal
    base = tmp_path / "components.json"
    base.write_text(MANIFEST.read_text())
    assert main(["--json", "import-figma", str(figma_dir), "--base", str(base), "--out", str(base)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "success" and result["needs_review"] > 0
    assert {c["figma"]: c["status"] for c in result["components"]}["Banner"] == "new"
    assert comp(json.loads(base.read_text()), "Banner")


def test_cli_rejects_non_figma_json(tmp_path, capsys):
    bad = tmp_path / "x.json"
    bad.write_text("[]")
    assert main(["import-figma", str(bad), "--no-base", "--out", str(tmp_path / "o.json")]) == 2
    assert "not a Figma" in capsys.readouterr().err


def test_spec_notes_unreviewed_drafts(tmp_path, capsys):
    out = tmp_path / "components.json"
    assert main(["import-figma", str(BANNER), "--base", str(MANIFEST), "--out", str(out)]) == 0
    screen = tmp_path / "screen.json"
    screen.write_text(json.dumps({"nodes": {"1:1": {
        "document": {"id": "1:1", "name": "Home", "type": "FRAME", "layoutMode": "VERTICAL", "children": [
            {"id": "2:1", "name": "Banner", "type": "INSTANCE", "componentId": "417:2508"}]},
        "components": {"417:2508": {"key": "v", "name": "State=Neutral", "componentSetId": "417:2507"}},
        "componentSets": {"417:2507": {"key": "00000000000000000000000000000000000000a6", "name": "Banner"}}}}}))
    capsys.readouterr()
    assert main(["--set", f"paths.manifest={out}", "--set", f"paths.tokens={TOKENS}",
                 "spec", "--figma-json", str(screen)]) == 0
    assert "unreviewed manifest draft(s) used: Banner" in capsys.readouterr().err


# ---------------------------------------------------------------- rules (figma_import in codegen.yaml)

from codegen.config import FigmaImportRules, load_config          # noqa: E402
from codegen.figma_import import check, format_check, name_has     # noqa: E402


def test_name_has_matches_whole_words_and_joined_spellings():
    assert name_has("IconButton", ["Button"]) and name_has("Check Box", ["Checkbox"])
    assert name_has("Checkbox", ["Check Box"]) and name_has("TextField", ["Text Field"])
    assert not name_has("Table", ["Tab"])


def test_rules_load_from_config_and_reject_unknown_keys(tmp_path):
    cfg_file = tmp_path / "codegen.yaml"
    cfg_file.write_text("figma_import:\n  prop_aliases: {Type: style}\n  visibility_prefixes: ['Show ']\n"
                        "  checks: {no_description: false}\n")
    rules = load_config(str(cfg_file)).figma_import
    assert rules.prop_aliases == {"Type": "style"} and rules.visibility_prefixes == ["Show "]
    assert rules.checks["no_description"] is False and rules.checks["boolean_variant"] is True
    cfg_file.write_text("figma_import:\n  checks: {nope: false}\n")
    with pytest.raises(ValueError, match="unknown check"):
        load_config(str(cfg_file))
    cfg_file.write_text("figma_import:\n  aliases: {}\n")
    with pytest.raises(ValueError, match="unknown key"):
        load_config(str(cfg_file))


def test_alias_maps_a_figma_property_onto_the_existing_code_prop():
    rules = FigmaImportRules(prop_aliases={"Type": "style"})
    res = import_figma(entries(BUTTON_SET), json.loads(MANIFEST.read_text()), rules=rules)
    button = comp(res.manifest, "Button")
    assert button["props"]["style"] == {"figmaProp": "Type", "values": ["Primary", "Secondary", "Tertiary"]}
    assert "type" not in button["props"]
    row = next(r for r in res.rows if r.figma == "Button")
    assert not any("'Style'" in r for r in row.review)


def test_custom_rules_change_private_prefix_and_kinds():
    rules = FigmaImportRules(private_prefixes=["Base/"], action_names=["Banner"])
    res = import_figma(entries(BANNER), None, rules=rules)
    assert comp(res.manifest, "Banner")["kind"] == "action"
    # '.' is no longer a private prefix: .Close is still skipped, but as an icon (vectors only)
    assert dict(res.skipped)[".Close"].startswith("icon")


# ---------------------------------------------------------------- import-figma --check

def _library(*sets):
    """A nodes response with one COMPONENT_SET per (name, definitions, variant children)."""
    nodes = {}
    for i, (name, defs, kids, desc) in enumerate(sets):
        sid = f"{i}:0"
        nodes[sid] = {"document": {"id": sid, "name": name, "type": "COMPONENT_SET",
                                   "componentPropertyDefinitions": defs, "children": kids},
                      "components": {}, "componentSets": {sid: {"key": f"k{i}", "name": name, "description": desc}}}
    return [(e, "lib.json") for e in nodes.values()]


def _variant(id_, name, *kids):
    return {"id": id_, "name": name, "type": "COMPONENT", "children": list(kids)}


def _text(id_, name, prop=None):
    n = {"id": id_, "name": name, "type": "TEXT", "characters": name}
    if prop:
        n["componentPropertyReferences"] = {"characters": prop}
    return n


def test_check_reports_each_convention_with_a_fix():
    lib = _library(
        ("Chip", {"State": {"type": "VARIANT", "variantOptions": ["Normal", "Inactive"]}},
         [_variant("0:1", "State=Normal", _text("0:2", "Label", "Label#1:0"))], ""),
        ("Toggle", {"State": {"type": "VARIANT", "variantOptions": ["Normal", "Disabled", "Error"]},
                    "Checked": {"type": "VARIANT", "variantOptions": ["Yes", "No"]}},
         [_variant("1:1", "State=Normal, Checked=Yes", _text("1:2", "Hint"))], "On/off switch."),
        ("Card", {"State": {"type": "VARIANT", "variantOptions": ["Disabled"]}},
         [_variant("2:1", "State=Disabled", _text("2:2", "Title", "Title#2:0"))], "Content card."),
    )
    res = check(lib)
    got = {(p.component, p.check) for p in res.problems}
    assert ("Chip", "no_description") in got and ("Toggle", "no_description") not in got
    assert ("Toggle", "boolean_variant") in got
    assert ("Toggle", "state_values") in got                     # Error is not an interaction state
    assert ("Chip", "inconsistent_values") in got                # Inactive vs Disabled ×2
    assert ("Card", "inconsistent_values") not in got
    assert ("Toggle", "text_not_property") in got and ("Chip", "text_not_property") not in got
    chip = next(p for p in res.problems if p.check == "inconsistent_values")
    assert "'Disabled'" in chip.fix
    text = format_check(res)
    assert "Fix in Figma:" in text and "3 component(s) checked" in text


def test_check_finds_renamed_props_against_the_manifest_and_respects_aliases_and_toggles():
    base = json.loads(MANIFEST.read_text())
    res = check(entries(BUTTON_SET), base)
    renamed = [p for p in res.problems if p.check == "renamed_prop"]
    assert len(renamed) == 1 and "'Type'" in renamed[0].message and "'Style'" in renamed[0].message
    assert "{Type: style}" in renamed[0].fix
    res = check(entries(BUTTON_SET), base, FigmaImportRules(prop_aliases={"Type": "style"},
                                                            checks={"boolean_variant": False}))
    assert not res.problems


def test_check_cli_writes_nothing_and_exits_1_on_problems(tmp_path, capsys):
    out = tmp_path / "components.json"
    rc = main(["--json", "import-figma", str(BANNER), str(BUTTON_SET), "--check", "--base", str(MANIFEST),
               "--out", str(out)])
    assert rc == 1 and not out.exists()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert {p["check"] for p in result["problems"]} == {"no_description", "state_values", "boolean_variant",
                                                        "renamed_prop"}
    assert result["partial"] == ["Banner"]
