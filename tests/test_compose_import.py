import json
from pathlib import Path

import pytest

from codegen.cli import main
from codegen.compose_import import format_report, run_import
from codegen.design_system import DesignSystem
from codegen.kotlin_source import strip

FIX = Path(__file__).parent / "fixtures"
MODULE = FIX / "compose_uikit"
APP = FIX / "compose_app"
FIGMA = FIX / "compose_figma_components.json"


@pytest.fixture(scope="module")
def imported():
    manifest, tokens, rep = run_import(MODULE, APP, FIGMA, None)
    return manifest, tokens, rep, DesignSystem(manifest, tokens)


def comp(manifest, name):
    return next(c for c in manifest["components"] if c["compose"]["import"].endswith("." + name))


def test_strip_keeps_offsets_and_masks_strings_and_comments():
    src = 'val a = "{ // not a comment }" // real\n/** doc */ fun X() {}'
    code, masked, kdocs = strip(src)
    assert len(code) == len(masked) == len(src)
    assert '"{ // not a comment }"' in code and "real" not in code
    assert "{" not in masked[:masked.index("fun")]
    assert kdocs[0][2] == "/** doc */"


def test_theme_class_tokens_use_the_light_instance_and_its_accessor(imported):
    _, tokens, rep, ds = imported
    assert tokens["color"]["textPrimary"]["$value"] == "#111827"
    assert tokens["color"]["accentTint"]["$value"] == "#00AAB20D"      # .copy(alpha = 0.05f)
    assert ds.token_code("color.textPrimary", "compose") == "AppTheme.colors.textPrimary"
    assert any("darkAppColors" in what for what, _ in rep.skipped_themes)


def test_semantic_colors_win_value_matching_over_palette(imported):
    _, tokens, _, ds = imported
    assert ds.color_token("#111827") == "color.textPrimary"
    assert ds.token_code("color.Gray900", "compose") == "Gray900"      # top-level val, exact name kept
    assert "Secret" not in tokens["color"]                              # internal


def test_dimensions_are_split_by_name(imported):
    _, tokens, _, ds = imported
    assert set(tokens["spacing"]) == {"spacingXs", "spacingS", "spacingM", "spacingL"}
    assert set(tokens["radius"]) == {"cornerRadiusS", "cornerRadiusM"}
    assert "iconSize" in tokens["size"] and "borderThin" in tokens["border"]
    assert ds.dimension_token(16, "spacing") == "spacing.spacingM"
    assert ds.token_code("spacing.spacingM", "compose") == "Dimens.spacingM"


def test_material_typography_resolves_private_vals_and_copy(imported):
    _, tokens, _, ds = imported
    assert tokens["typography"]["h4"]["$value"] == {"fontFamily": "Inter", "fontWeight": 700,
                                                    "fontSize": "28px", "lineHeight": "34px"}
    assert tokens["typography"]["body2"]["$value"]["fontSize"] == "14px"
    assert tokens["typography"]["body2"]["$value"]["fontWeight"] == 400
    assert ds.typography_token(None, 14, 400) == "typography.body2"
    assert ds.token_code("typography.body2", "compose") == "MaterialTheme.typography.body2"


def test_components_from_public_composables(imported):
    manifest, _, _, ds = imported
    names = {c["compose"]["import"].split(".")[-1] for c in manifest["components"]}
    assert "LegacyButtonPreview" not in names and "ButtonPreviewHelper" not in names
    assert "LegacyAppTheme" not in names

    button = comp(manifest, "LegacyButton")
    assert button["figma"] == "Button" and button["kind"] == "action"
    assert button["description"] == "Primary action button. Use one Primary per screen."
    assert button["props"]["style"] == {"figmaProp": "Style", "values": ["Primary", "Secondary", "Text"]}
    assert button["props"]["size"]["values"] == ["S", "M", "L"]
    assert button["compose"]["example"] == 'LegacyButton(text = "Log in", onClick = onLogin, style = LegacyButtonStyle.Primary)'
    assert "content: @Composable () -> Unit" in button["compose"]["signature"]   # overload kept

    field = comp(manifest, "LegacyTextField")
    assert field["figma"] == "Text Field" and field["kind"] == "input" and "stateType" not in field
    assert "value" not in field["props"] and field["props"]["label"] == {"figmaText": "Label"}

    box = comp(manifest, "LegacyCheckBox")
    assert box["kind"] == "input" and box["stateType"] == "Boolean" and box["figmaKey"] == "k-cb"

    assert "kind" not in comp(manifest, "LegacyOldField")   # TextFieldValue binding: left to a human
    assert ds.match_component([], ["Text Field"]) is field


def test_text_primitive_and_imports(imported):
    manifest, *_ = imported
    prim = manifest["primitives"]["compose"]
    assert prim["Text"].startswith("LegacyText(")
    assert "com.legacy.uikit.theme.AppTheme" in prim["imports"]
    assert "androidx.compose.material.MaterialTheme" in prim["imports"]


def test_report_lists_what_needs_a_human(imported):
    manifest, tokens, rep, _ = imported
    text = format_report(rep, manifest, tokens)
    assert "Top Navigation Bar" in text                     # Figma component without composable
    assert "`LegacyBadge`" in text and "**missing**" in text  # no KDoc
    assert "TextFieldValue" in text


def test_cli_writes_drafts_and_refuses_to_overwrite(tmp_path, capsys):
    out = tmp_path / "drafts"
    args = ["import-compose", str(MODULE), "--app", str(APP), "--out-dir", str(out)]
    assert main(args) == 0
    assert json.loads((out / "tokens.json").read_text())["color"]["link"]["$value"] == "#2563EB"
    assert (out / "import-report.md").exists()
    assert main(args) == 2
    assert "--force" in capsys.readouterr().err
    assert main(args + ["--force"]) == 0
