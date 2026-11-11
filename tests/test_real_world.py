"""Simplifier behaviour on patterns from real production files (synthetic data only)."""
from codegen.design_system import DesignSystem
from codegen.prompts import typed_bindings, user_prompt
from codegen.simplify import simplify

GRAPHIC_WARNING = "hand-drawn graphic"


def make_ds(components=(), tokens=None):
    manifest = {"tokenCode": {"compose": "AppTheme.{category}.{name}", "swiftui": "Theme.{category}.{name}"},
                "categoryCode": {"color": "colors"}, "components": list(components)}
    return DesignSystem(manifest, tokens or {})


def frame(name, children=(), id="1:1", **kw):
    return {"id": id, "name": name, "type": "FRAME", "layoutMode": "VERTICAL", "children": list(children), **kw}


def text(chars, id="9:9"):
    return {"id": id, "name": chars, "type": "TEXT", "characters": chars, "style": {}}


def screen(*children, components=None, component_sets=None, styles=None):
    return {"document": frame("Screen", children, id="0:1"), "components": components or {},
            "componentSets": component_sets or {}, "styles": styles or {}}


def warnings_with(result, needle):
    return [w for w in result.warnings if needle in w]


# 1. R1 + R3: an unmapped section component keeps its content
def test_unmapped_instance_is_expanded():
    header = {"id": "2:1", "name": "Page Header", "type": "INSTANCE", "componentId": "c1",
              "layoutMode": "VERTICAL", "children": [text("Hello")]}
    res = simplify(make_ds(), screen(header, components={"c1": {"name": "Page Header"}}))
    node = res.spec["root"]["children"][0]
    assert node["type"] == "Text" and node["text"] == "Hello"
    assert node["figmaComponent"] == "Page Header"
    assert warnings_with(res, "expanded as plain layout")


# 2. R2: nested vector artwork collapses into one Graphic with one warning
def test_vector_artwork_becomes_one_graphic():
    vec = lambda i: {"id": f"v{i}", "name": "Vector", "type": "VECTOR"}
    inner = {"id": "g2", "name": "Group", "type": "GROUP", "children": [vec(1), vec(2)]}
    logo = {"id": "g1", "name": "logo", "type": "GROUP", "children": [inner, vec(3)],
            "absoluteBoundingBox": {"x": 0, "y": 0, "width": 120, "height": 40}}
    res = simplify(make_ds(), screen(logo))
    assert res.spec["root"]["children"] == [{"type": "Graphic", "name": "logo", "id": "g1", "sizePx": "120x40"}]
    assert len(warnings_with(res, GRAPHIC_WARNING)) == 1
    assert len(res.warnings) == 1


# 3. R3: pure wrapper frames collapse, passing their sizing to the child
def test_wrapper_frames_collapse():
    outer = frame("Outer", [frame("Inner", [text("Hi")], id="1:2")], id="1:1", layoutSizingHorizontal="FILL")
    res = simplify(make_ds(), screen(outer))
    assert res.spec["root"]["children"] == [
        {"type": "Text", "name": "Hi", "id": "9:9", "text": "Hi", "style": "raw:Nonepx/None", "fill": "maxWidth"}
    ]


CHECKBOX = {
    "figma": "Check Box", "kind": "input", "stateType": "Boolean",
    "props": {
        "checked": {"figmaProp": "Checked", "map": {"ON": True, "OFF": False}},
        "showText": {"figmaProp": "Text", "map": {"ON": True, "OFF": False}},
        "text": {"figmaProp": "Text#905:0"},
    },
}


# 4. R5 + R9: exact/short property keys and Boolean state bindings
def test_checkbox_props_and_typed_binding():
    cb = {"id": "3:1", "name": "AgreeCheckBox", "type": "INSTANCE", "componentId": "c2", "children": [],
          "componentProperties": {"Text#905:0": {"type": "TEXT", "value": "Label"},
                                  "Checked": {"type": "VARIANT", "value": "ON"},
                                  "Text": {"type": "VARIANT", "value": "OFF"}}}
    ds = make_ds([CHECKBOX])
    res = simplify(ds, screen(cb, components={"c2": {"name": "Checked=ON", "componentSetId": "s2"}},
                              component_sets={"s2": {"name": "Check Box"}}))
    node = res.spec["root"]["children"][0]
    assert node["props"] == {"checked": True, "showText": False, "text": "Label"}
    binding = node["binding"]
    assert typed_bindings(ds, res.spec["root"], "compose") == [(binding, "Boolean", "false")]
    assert typed_bindings(ds, res.spec["root"], "swiftui") == [(binding, "Bool", "false")]
    prompt = user_prompt(ds, res, "compose", "Agree", None, [])
    assert f"val {binding}: Boolean = false" in prompt
    assert f"on{binding[0].upper() + binding[1:]}Change: (Boolean) -> Unit" in prompt


# 5. R5: INSTANCE_SWAP values resolve to component names
def test_instance_swap_resolves_to_component_name():
    button_entry = {"figma": "Button", "kind": "action", "props": {"rightIcon": {"figmaProp": "Right Icon"}}}
    btn = {"id": "4:1", "name": "ContinueButton", "type": "INSTANCE", "componentId": "c3", "children": [],
           "componentProperties": {"Right Icon#33633:15": {"type": "INSTANCE_SWAP", "value": "172:3670"}}}
    res = simplify(make_ds([button_entry]), screen(btn, components={"c3": {"name": "Button"},
                                                                     "172:3670": {"name": "arrow-right"}}))
    node = res.spec["root"]["children"][0]
    assert node["props"]["rightIcon"] == "arrow-right"
    assert node["event"] == "onContinue"


# 6. R6 + R7 + R8: color by style name; transparent reserved border
def test_style_name_color_and_transparent_border():
    tokens = {"color": {"carbon": {"darkest": {"$type": "color", "$value": "#000000"}}}}
    card = frame("Card", [text("a", id="9:1"), text("b", id="9:2")], id="5:1",
                 fills=[{"type": "SOLID", "color": {"r": 0.14, "g": 0.15, "b": 0.17, "a": 1}}],
                 styles={"fill": "S:1"},
                 strokes=[{"type": "SOLID", "color": {"r": 0, "g": 0.6, "b": 0.7, "a": 0}}], strokeWeight=2)
    res = simplify(make_ds(tokens=tokens), screen(card, styles={"S:1": {"name": "Carbon / Darkest"}}))
    node = res.spec["root"]["children"][0]
    assert node["background"] == "color.carbon.darkest"
    assert node["border"] == {"width": "2px", "color": "transparent"}
    assert not warnings_with(res, "color")


def test_unmatched_color_warning_names_bound_style():
    card = frame("Card", [text("a", id="9:1"), text("b", id="9:2")], id="5:1",
                 fills=[{"type": "SOLID", "color": {"r": 1, "g": 0, "b": 0, "a": 1}}], styles={"fill": "S:1"})
    res = simplify(make_ds(), screen(card, styles={"S:1": {"name": "Brand / Red"}}))
    assert res.spec["root"]["children"][0]["background"] == "raw:#FF0000"
    assert warnings_with(res, "background color #FF0000 (style 'Brand / Red') matches no color token")


def test_gap_only_with_two_or_more_children():
    one = frame("One", [text("a")], id="6:1", itemSpacing=10, fills=[{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}])
    res = simplify(make_ds(tokens={"color": {"white": {"$type": "color", "$value": "#FFFFFF"}}}), screen(one))
    assert "gap" not in res.spec["root"]["children"][0]
    assert not warnings_with(res, "itemSpacing")


def test_typography_token_by_style_name():
    ds = make_ds(tokens={"typography": {"heading/l": {"$type": "typography", "$value": {"fontSize": "28px"}}}})
    assert ds.token_by_name("typography", "Heading/L") == "typography.heading/l"
    assert ds.typography_token("Heading / L", 99, 400) == "typography.heading/l"
