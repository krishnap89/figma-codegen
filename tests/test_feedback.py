"""Progress output and grouped Figma warnings."""
import ast
import io
import os
import re
import time
from pathlib import Path

import pytest

from codegen import progress as progress_mod
from codegen.agent import CodegenAgent
from codegen.config import Config
from codegen.progress import Progress, fmt_duration
from codegen.report import KINDS, format_warnings, group_warnings
from codegen.simplify import FigmaWarning

from test_pipeline import FakeClient, call, ds, result  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parents[1]


def w(kind, name, i=0):
    return FigmaWarning(kind, name, f"1:{i}", f"msg {i}")


def test_every_emitted_kind_has_a_label():
    tree = ast.parse((ROOT / "codegen/simplify.py").read_text())
    emitted = {
        c.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "_warn"
        for c in ast.walk(node.args[2])
        if isinstance(c, ast.Constant) and isinstance(c.value, str)
    }
    assert len(emitted) >= 10 and emitted <= set(KINDS), emitted - set(KINDS)


def test_grouped_summary_counts_and_examples():
    issues = ([w("color_unbound", f"Frame {i}", i) for i in range(12)]
              + [w("graphic", "Overlay", i) for i in range(3)] + [w("graphic", "Logo", 99)])
    text = format_warnings(issues)
    lines = text.splitlines()
    assert lines[0].startswith("Figma: 16 warnings")
    assert "12  colors not bound to a color style" in lines[1]          # largest group first
    assert "Frame 0, Frame 1, Frame 2 +9 more" in lines[1]
    assert re.search(r"4  hand-drawn graphics\s+Overlay ×3, Logo$", lines[2])
    assert "--verbose" in lines[-1]
    assert "msg 5" not in text


def test_verbose_lists_everything_with_hint():
    issues = [w("default_name", "Check Box", i) for i in range(2)] + [w("mystery", "X", 9)]
    text = format_warnings(issues, verbose=True)
    assert "rename the layer" in text
    assert "[Check Box 1:0] msg 0" in text and "[Check Box 1:1] msg 1" in text
    assert "1  other warning" in text and "--verbose" not in text
    assert list(group_warnings(issues)) == ["default_name", "mystery"]


def test_fmt_duration():
    assert fmt_duration(1.234) == "1.2s"
    assert fmt_duration(42.4) == "42s"
    assert fmt_duration(125) == "2m 05s"


def test_progress_non_live_finish_lines_and_failure():
    buf = io.StringIO()
    p = Progress(stream=buf, live=False)
    with p.step("fetching") as st:
        st.done("3 nodes")
    with pytest.raises(ValueError):
        with p.step("compiling"):
            raise ValueError("boom")
    out = buf.getvalue().splitlines()
    assert re.fullmatch(r"✓ fetching — 3 nodes  \(\d\.\ds\)", out[0])
    assert out[1].startswith("✗ compiling — failed: ValueError")
    assert "\r" not in buf.getvalue()


def test_progress_non_live_announces_slow_steps_and_heartbeats(monkeypatch):
    monkeypatch.setattr(progress_mod, "ANNOUNCE_AFTER_S", 0.0)
    monkeypatch.setattr(progress_mod, "HEARTBEAT_S", 0.0)
    buf = io.StringIO()
    p = Progress(stream=buf, live=False)
    with p.step("round 1/4 · generating"):
        p.update("waiting for qwen")
        time.sleep(2.2)   # ticker checks once a second
    text = buf.getvalue()
    assert "… round 1/4 · generating — waiting for qwen\n" in text
    assert "still running: round 1/4 · generating" in text


def test_progress_live_redraws_spinner_line():
    buf = io.StringIO()
    p = Progress(stream=buf, live=True)
    with p.step("round 1/4 · generating"):
        p.update("waiting for qwen")
        p.info("    tool list_tokens({})")
        time.sleep(0.25)
    text = buf.getvalue()
    assert "\r\x1b[2K" in text and "total" in text
    assert "    tool list_tokens({})\n" in text
    # the spinner line is erased before the permanent finish line is written over it
    finish = text.rindex("✓ round 1/4 · generating")
    assert text[finish - 5:finish] == "\r\x1b[2K" and text.endswith(")\n")


def test_disabled_progress_writes_nothing():
    buf = io.StringIO()
    p = Progress(stream=buf, enabled=False)
    with p.step("x"):
        p.info("y")
    assert buf.getvalue() == ""


def test_agent_reports_rounds(ds, result, tmp_path):  # noqa: F811
    bad = "```kotlin\nval x = 8.dp\n```"
    good = "```kotlin\nval x = AppTheme.spacing.m\n```"
    client = FakeClient([(None, [call("list_tokens", {})]), (bad, None), (good, None)])
    cfg = Config(trace_dir=None, examples_dir=None, max_rounds=3)
    buf = io.StringIO()
    CodegenAgent(cfg, ds, client=client, progress=Progress(stream=buf, live=False)).run(result, "compose", "X")
    lines = buf.getvalue().splitlines()
    assert lines[0] == "    tool list_tokens({})"
    assert lines[1].startswith("✓ round 1/3 · generating — 1 lines, 1 tool call  (")
    assert lines[2].startswith("✗ round 1/3 · lint — 1 lint issue(s), compile skipped")
    assert lines[3].startswith("✓ round 2/3 · generating — 1 lines  (")
    assert lines[4].startswith("✓ round 2/3 · lint — valid (compile skipped")


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def test_color_env_rules(monkeypatch):
    from codegen.style import use_color

    class Tty(io.StringIO):
        def isatty(self):
            return True

    for var in ("NO_COLOR", "FORCE_COLOR", "TERM"):
        monkeypatch.delenv(var, raising=False)
    assert use_color(Tty()) and not use_color(io.StringIO())
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert use_color(io.StringIO())
    monkeypatch.setenv("NO_COLOR", "1")          # NO_COLOR wins
    assert not use_color(Tty())


def test_colored_summary_matches_plain_once_stripped():
    from codegen.style import Style
    issues = [w("color_unbound", f"Frame {i}", i) for i in range(12)] + [w("graphic", "Logo", 99)]
    for verbose in (False, True):
        colored = format_warnings(issues, verbose, Style(True))
        assert "\x1b[" in colored
        assert ANSI.sub("", colored) == format_warnings(issues, verbose)   # same columns


def test_colored_finish_lines_and_live_width(monkeypatch):
    buf = io.StringIO()
    p = Progress(stream=buf, live=False, color=True)
    with p.step("lint") as st:
        st.done("2 lint issue(s)", ok=False)
    assert "\x1b[31m✗\x1b[0m" in buf.getvalue()
    assert ANSI.sub("", buf.getvalue()).startswith("✗ lint — 2 lint issue(s)  (")

    monkeypatch.setattr(progress_mod.shutil, "get_terminal_size", lambda *_: os.terminal_size((40, 20)))
    buf = io.StringIO()
    p = Progress(stream=buf, live=True, color=True)
    with p.step("round 1/4 · generating"):
        p.update("waiting for a-model-with-a-very-long-name")
    redraws = [ANSI.sub("", x) for x in buf.getvalue().split("\r\x1b[2K") if x and "⠋" in x]
    assert redraws and all(len(x) <= 39 for x in redraws)
