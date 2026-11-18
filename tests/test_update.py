"""Targeted update (`figma-codegen update`, codegen/update.py). Fake LLM, synthetic code."""
import json

import openai
import pytest

from codegen.baseline import FigmaRef, add_header, baseline_path, is_code_modified, load_baseline, write_baseline
from codegen.figma_client import load_node_from_file
from codegen.simplify import simplify
from codegen.update import changed_lines, line_budget, screen_signature

from test_baseline import CFG
from test_pipeline import FIXTURE, URL, FakeClient, _run_cli, ds  # noqa: F401  (fixture)

REF = FigmaRef("AbC123", "12:300", URL)

BODY = """package com.x.login

data class LoginScreenState(
    val email: String = "",
    val password: String = "",
)

@Composable
fun LoginScreen(
    state: LoginScreenState,
    onEmailChange: (String) -> Unit,
    onPasswordChange: (String) -> Unit,
    onForgotPassword: () -> Unit,
    onLogin: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .background(AppTheme.colors.backgroundPrimary)
            .padding(horizontal = AppTheme.spacing.l, vertical = AppTheme.spacing.xl),
        verticalArrangement = Arrangement.spacedBy(AppTheme.spacing.s),
    ) {
        UiText("Welcome back", style = AppTheme.typography.headingL)
        UiText("Log in to continue", style = AppTheme.typography.bodyM)
        Spacer(Modifier.height(AppTheme.spacing.s))
        UiTextField(value = state.email, onValueChange = onEmailChange, label = "Email")
        UiTextField(value = state.password, onValueChange = onPasswordChange, label = "Password", isSecure = true)
        UiLink(text = "Forgot password?", onClick = onForgotPassword)
        Spacer(Modifier.weight(1f))
        UiButton(text = "Log in", style = UiButtonStyle.Primary, onClick = onLogin)
        // TODO(design): export Decoration as an asset
        // TODO(design): PromoBanner is not in UIKit
    }
}

@Preview
@Composable
private fun LoginScreenPreview() {
    LoginScreen(LoginScreenState(), {}, {}, {}, {})
}
"""
OLD_GAP = "spacedBy(AppTheme.spacing.s)"
NEW_GAP = "spacedBy(AppTheme.spacing.m)"


def fenced(code):
    return f"```kotlin\n{code}```"


@pytest.fixture
def screen(tmp_path, ds):  # noqa: F811
    """A generated LoginScreen.kt whose baseline has gap spacing.s; the current frame (fixture) has spacing.m."""
    spec = simplify(ds, load_node_from_file(str(FIXTURE))).spec
    spec["root"]["gap"] = "spacing.s"
    code = tmp_path / "LoginScreen.kt"
    code.write_text(add_header(BODY, REF, code))
    write_baseline(code, spec, REF, "compose", "LoginScreen", "com.x.login", CFG, "m")
    return code


def _update(monkeypatch, tmp_path, turns, *extra, rounds=4):
    client = FakeClient(turns)
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: client)
    monkeypatch.setenv("CODEGEN_USE_TOOLS", "false")
    monkeypatch.setenv("CODEGEN_MAX_ROUNDS", str(rounds))
    rc = _run_cli(monkeypatch, tmp_path, ["update", "LoginScreen.kt", *extra])
    return rc, client


def test_up_to_date(monkeypatch, tmp_path, capsys, ds):  # noqa: F811
    spec = simplify(ds, load_node_from_file(str(FIXTURE))).spec
    code = tmp_path / "LoginScreen.kt"
    code.write_text(add_header(BODY, REF, code))
    write_baseline(code, spec, REF, "compose", "LoginScreen", None, CFG, "m")
    rc, client = _update(monkeypatch, tmp_path, [])
    assert rc == 0 and capsys.readouterr().out.strip() == "up to date" and client.requests == []


def test_dry_run(monkeypatch, tmp_path, capsys, screen):
    rc, client = _update(monkeypatch, tmp_path, [], "--dry-run")
    assert rc == 10 and client.requests == []
    assert "visual     LoginScreen: gap spacing.s → spacing.m" in capsys.readouterr().out
    assert not screen.with_name("LoginScreen.kt.updated").exists()


def test_well_behaved_update_applied_and_baseline_refreshed(monkeypatch, tmp_path, capsys, screen):
    before = screen.read_text()
    rc, client = _update(monkeypatch, tmp_path, [(fenced(before.replace(OLD_GAP, NEW_GAP)), None)], "--apply")
    assert rc == 0
    assert screen.read_text() == before.replace(OLD_GAP, NEW_GAP)
    b = load_baseline(screen)
    assert b.spec["root"]["gap"] == "spacing.m" and not is_code_modified(screen, b)
    out = capsys.readouterr().out
    assert "-        verticalArrangement = Arrangement.spacedBy(AppTheme.spacing.s)," in out
    assert "+        verticalArrangement = Arrangement.spacedBy(AppTheme.spacing.m)," in out
    # the prompt: current file, the one change, only the tokens involved
    prompt = client.requests[0]["messages"][1]["content"]
    assert OLD_GAP in prompt and "1. LoginScreen: gap spacing.s → spacing.m" in prompt
    assert "spacing.m -> AppTheme.spacing.m" in prompt and "spacing.xl ->" not in prompt
    assert "Apply ONLY the listed changes" in client.requests[0]["messages"][0]["content"]


def test_default_writes_updated_file_only(monkeypatch, tmp_path, screen):
    before = screen.read_text()
    rc, _ = _update(monkeypatch, tmp_path, [(fenced(before.replace(OLD_GAP, NEW_GAP)), None)])
    assert rc == 0 and screen.read_text() == before
    assert NEW_GAP in screen.with_name("LoginScreen.kt.updated").read_text()
    assert load_baseline(screen).spec["root"]["gap"] == "spacing.s"   # not refreshed until applied


def test_full_rewrite_rejected_by_line_budget_then_retried(monkeypatch, tmp_path, screen):
    before = screen.read_text()
    good = before.replace(OLD_GAP, NEW_GAP)
    rewrite = "\n".join("  " + line for line in good.splitlines()) + "\n"
    rc, client = _update(monkeypatch, tmp_path, [(fenced(rewrite), None), (fenced(good), None)], "--apply")
    assert rc == 0 and screen.read_text() == good
    feedback = client.requests[1]["messages"][-1]["content"]
    assert "too many unrelated changes" in feedback and "(limit 20)" in feedback


def test_signature_change_rejected_when_not_allowed(monkeypatch, tmp_path, screen):
    good = screen.read_text().replace(OLD_GAP, NEW_GAP)
    renamed = good.replace("onLogin: () -> Unit", "onSubmit: () -> Unit")
    rc, client = _update(monkeypatch, tmp_path, [(fenced(renamed), None), (fenced(good), None)], "--apply")
    assert rc == 0 and screen.read_text() == good
    assert "public signature changed" in client.requests[1]["messages"][-1]["content"]


def test_edited_file_writes_updated_and_keeps_original(monkeypatch, tmp_path, capsys, screen):
    screen.write_text(screen.read_text() + "// my local change\n")
    edited = screen.read_text()
    rc, _ = _update(monkeypatch, tmp_path, [(fenced(edited.replace(OLD_GAP, NEW_GAP)), None)], "--apply")
    assert rc == 0 and screen.read_text() == edited
    updated = screen.with_name("LoginScreen.kt.updated").read_text()
    assert NEW_GAP in updated and "// my local change" in updated
    out = capsys.readouterr().out
    assert "file was edited since generation; developer changes are preserved" in out
    assert "pass --apply --force" in out
    # with --force it is applied
    rc, _ = _update(monkeypatch, tmp_path, [(fenced(edited.replace(OLD_GAP, NEW_GAP)), None)], "--apply", "--force")
    assert rc == 0 and NEW_GAP in screen.read_text()
    assert not screen.with_name("LoginScreen.kt.updated").exists()


def test_conflict_hint_when_old_token_is_gone(monkeypatch, tmp_path, capsys, screen):
    # the developer replaced spacing.s everywhere; .sm must not count as the old token
    screen.write_text(screen.read_text().replace("AppTheme.spacing.s)", "AppTheme.spacing.sm)"))
    rc, client = _update(monkeypatch, tmp_path, [], "--dry-run")
    assert rc == 10
    assert "possible conflict: code no longer matches the baseline (`AppTheme.spacing.s` not found" \
        in capsys.readouterr().out
    current = screen.read_text()
    rc, client = _update(monkeypatch, tmp_path, [(fenced(current), None)])
    prompt = client.requests[0]["messages"][1]["content"]
    assert "gap spacing.s → spacing.m  [possible conflict" in prompt and "## Notes" in prompt


def test_failure_keeps_original_and_writes_draft(monkeypatch, tmp_path, capsys, screen):
    before = screen.read_text()
    bad = before.replace(OLD_GAP, "spacedBy(12.dp)")
    rc, _ = _update(monkeypatch, tmp_path, [(fenced(bad), None), (fenced(bad), None)], "--apply", rounds=2)
    assert rc == 1 and screen.read_text() == before
    assert "12.dp" in screen.with_name("LoginScreen.kt.updated.draft").read_text()
    assert load_baseline(screen).spec["root"]["gap"] == "spacing.s"
    assert "raw dp value" in capsys.readouterr().out


def test_trace_has_changes_and_safety_results(monkeypatch, tmp_path, screen):
    monkeypatch.setenv("CODEGEN_TRACE_DIR", str(tmp_path / "traces"))
    good = screen.read_text().replace(OLD_GAP, NEW_GAP)
    _update(monkeypatch, tmp_path, [(fenced(good), None)], "--apply")
    events = [json.loads(line) for f in (tmp_path / "traces").glob("*-update.jsonl") for line in f.open()]
    start = next(e for e in events if e["event"] == "start_update")
    assert start["changes"][0]["kind"] == "token_changed"
    safety = next(e for e in events if e["event"] == "safety")
    assert safety["changed_lines"] == 1 and safety["budget"] == 20 and safety["signature_ok"]


def test_missing_baseline(monkeypatch, tmp_path, capsys):
    (tmp_path / "LoginScreen.kt").write_text(BODY)
    rc, _ = _update(monkeypatch, tmp_path, [])
    assert rc == 2 and "run `figma-codegen generate` first" in capsys.readouterr().out


# --- units ---------------------------------------------------------------------------------------

def test_changed_lines_and_budget():
    assert changed_lines("a\nb\nc\n", "a\nb\nc\n") == 0
    assert changed_lines("a\nb\nc\n", "a\nB\nc\nd\n") == 2
    assert line_budget(1) == 20 and line_budget(3) == 36


def test_screen_signature_compose_ignores_body_and_formatting():
    sig = screen_signature(BODY, "compose", "LoginScreen")
    assert sig.startswith("fun LoginScreen(state: LoginScreenState, onEmailChange")
    assert "class LoginScreenState(val email" in sig
    assert screen_signature(BODY.replace(OLD_GAP, NEW_GAP), "compose", "LoginScreen") == sig
    assert screen_signature(BODY.replace("modifier: Modifier = Modifier,", "modifier: Modifier = Modifier"),
                            "compose", "LoginScreen") == sig   # trailing comma
    assert screen_signature(BODY.replace("val password", "val pin"), "compose", "LoginScreen") != sig
    assert screen_signature("fun Other() {}", "compose", "LoginScreen") is None


def test_screen_signature_swiftui():
    code = """import SwiftUI

struct LoginScreenState {
    var email: String = ""
}

struct LoginScreen: View {
    @Binding var state: LoginScreenState
    let onLogin: () -> Void

    var body: some View {
        VStack(spacing: Theme.spacing.m) { let x = 1 }
    }
}
"""
    sig = screen_signature(code, "swiftui", "LoginScreen")
    assert sig == ("struct LoginScreen { @Binding var state: LoginScreenState; let onLogin: () -> Void } | "
                   'struct LoginScreenState { var email: String = "" }')
    assert screen_signature(code.replace("Theme.spacing.m", "Theme.spacing.l"), "swiftui", "LoginScreen") == sig
    assert screen_signature(code.replace("onLogin", "onSubmit"), "swiftui", "LoginScreen") != sig
