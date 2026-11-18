"""`--json`: exactly one JSON object on stdout per command, human output on stderr."""
import json

import httpx
import openai
import pytest

from codegen.baseline import FigmaRef, add_header, write_baseline
from codegen.figma_client import load_node_from_file
from codegen.simplify import simplify

from test_baseline import CFG, GOOD
from test_pipeline import FIXTURE, ROOT, URL, FakeClient, FakeFigma, _run_cli, ds  # noqa: F401  (fixture)
from test_update import BODY, OLD_GAP, NEW_GAP, fenced

COMMON = {"schema", "command", "status", "exit_code", "duration_s", "warnings"}


def run_json(monkeypatch, tmp_path, capsys, argv, turns=None):
    if turns is not None:
        client = FakeClient(turns)
        monkeypatch.setattr(openai, "OpenAI", lambda **kw: client)
        monkeypatch.setenv("CODEGEN_USE_TOOLS", "false")
    rc = _run_cli(monkeypatch, tmp_path, ["--json", *argv])
    out = capsys.readouterr()
    data = json.loads(out.out)              # the whole of stdout is one JSON object
    assert isinstance(data, dict) and COMMON <= set(data)
    assert data["schema"] == 1 and data["exit_code"] == rc
    assert set(data["warnings"]) == {"total", "groups"}
    return rc, data, out.err


@pytest.fixture
def screen(tmp_path, ds):  # noqa: F811
    """LoginScreen.kt with a baseline whose gap (spacing.s) differs from the frame (spacing.m)."""
    ref = FigmaRef("AbC123", "12:300", URL)
    spec = simplify(ds, load_node_from_file(str(FIXTURE))).spec
    spec["root"]["gap"] = "spacing.s"
    code = tmp_path / "LoginScreen.kt"
    code.write_text(add_header(BODY, ref, code))
    write_baseline(code, spec, ref, "compose", "LoginScreen", "com.x.login", CFG, "m")
    return code


def test_spec(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CODEGEN_SKILLS_DIR", str(ROOT / "skills"))
    rc, data, err = run_json(monkeypatch, tmp_path, capsys, ["spec", URL])
    assert rc == 0 and data["command"] == "spec" and data["status"] == "success"
    assert data["spec"]["screen"] == "LoginScreen"
    assert data["warnings"]["total"] == 2
    assert {g["kind"] for g in data["warnings"]["groups"]} == {"graphic", "unknown_component"}
    assert data["warnings"]["groups"][0]["nodes"][0].keys() == {"name", "id"}
    assert "selected" in data["skills"]["compose"]
    assert [g["kind"] for g in data["gaps"]] == ["missing_component", "non_library_element"]
    assert "simplifying" in err and "Figma: 2 warnings" in err   # progress + human text on stderr


def test_json_flag_after_subcommand(monkeypatch, tmp_path, capsys):
    rc = _run_cli(monkeypatch, tmp_path, ["spec", "--json", URL])
    assert rc == 0 and json.loads(capsys.readouterr().out)["command"] == "spec"


def test_generate_success(monkeypatch, tmp_path, capsys):
    rc, data, err = run_json(monkeypatch, tmp_path, capsys,
                             ["generate", URL, "--platform", "compose", "--out", "out/LoginScreen.kt"],
                             turns=[(GOOD, None)])
    assert rc == 0 and data["status"] == "success"
    assert data["screen"] == "LoginScreen" and data["platform"] == "compose"
    assert data["output_file"] == "out/LoginScreen.kt" and data["draft_file"] is None
    assert data["baseline_file"] == "out/LoginScreen.kt.codegen.json"
    assert data["rounds"] == 1 and data["tool_calls"] == 0 and data["lint_issues"] == []
    assert data["compile"] == {"ok": True, "skipped": True, "output": "compile skipped: no sandbox configured"}
    assert "trace" in data and "Generating compose screen" in err


def test_generate_failed(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CODEGEN_MAX_ROUNDS", "1")
    bad = "```kotlin\nval x = 8.dp\n```"
    rc, data, _ = run_json(monkeypatch, tmp_path, capsys,
                           ["generate", URL, "--platform", "compose", "--out", "X.kt"], turns=[(bad, None)])
    assert rc == 1 and data["status"] == "failed" and data["output_file"] is None
    assert data["draft_file"] == "X.kt.draft"
    assert data["lint_issues"][0]["line"] == 1 and "raw dp value" in data["lint_issues"][0]["message"]


def test_lint(monkeypatch, tmp_path, capsys):
    (tmp_path / "A.kt").write_text("val a = 1\nval b = 16.dp\n")
    rc, data, err = run_json(monkeypatch, tmp_path, capsys, ["lint", "A.kt", "--platform", "compose"])
    assert rc == 1 and data["status"] == "failed"
    assert data["issues"][0]["line"] == 2 and data["issues"][0]["message"].startswith("raw dp value")
    assert "1 issue(s)" in err


def test_diff_changes_and_up_to_date(monkeypatch, tmp_path, capsys, screen):
    rc, data, err = run_json(monkeypatch, tmp_path, capsys, ["diff", "LoginScreen.kt"])
    assert rc == 10 and data["status"] == "changes"
    assert data["changes"] == [{"kind": "token_changed", "path": "LoginScreen", "node_name": "LoginScreen",
                                "detail": {"key": "gap", "old": "spacing.s", "new": "spacing.m"},
                                "severity": "visual", "node_id": "12:300"}]
    assert "1 change" in err
    rc, data, _ = run_json(monkeypatch, tmp_path, capsys, ["diff", "--specs", "LoginScreen.kt.codegen.json",
                                                           "LoginScreen.kt.codegen.json"])
    assert rc == 0 and data["status"] == "up_to_date" and data["changes"] == []


def test_update_dry_run_and_apply(monkeypatch, tmp_path, capsys, screen):
    rc, data, _ = run_json(monkeypatch, tmp_path, capsys, ["update", "LoginScreen.kt", "--dry-run"])
    assert rc == 10 and data["status"] == "changes" and len(data["applied"]) == 1 and data["skipped"] == []
    assert data["updated_file"] is None

    good = screen.read_text().replace(OLD_GAP, NEW_GAP)
    rc, data, err = run_json(monkeypatch, tmp_path, capsys, ["update", "LoginScreen.kt", "--apply"],
                             turns=[(fenced(good), None)])
    assert rc == 0 and data["status"] == "success"
    assert data["updated_file"] == "LoginScreen.kt" and data["overwritten"] is True
    assert data["baseline_file"] == "LoginScreen.kt.codegen.json"
    assert "+        verticalArrangement = Arrangement.spacedBy(AppTheme.spacing.m)," in data["diff"]
    assert data["changes"][0]["kind"] == "token_changed" and isinstance(data["gaps"], list)
    assert "+        verticalArrangement" in err          # the human diff went to stderr

    rc, data, _ = run_json(monkeypatch, tmp_path, capsys, ["update", "LoginScreen.kt"])
    assert rc == 0 and data["status"] == "up_to_date"


def test_status(monkeypatch, tmp_path, capsys, screen):
    rc, data, _ = run_json(monkeypatch, tmp_path, capsys, ["status", "."])
    assert rc == 0 and data["files"] == [{
        "path": "LoginScreen.kt", "screen": "LoginScreen", "platform": "compose",
        "figma": {"file_key": "AbC123", "node_id": "12:300", "url": URL},
        "generated_at": data["files"][0]["generated_at"], "model": "m", "modified": False, "missing": False}]


def test_skills_config_and_components(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CODEGEN_SKILLS_DIR", str(ROOT / "skills"))
    rc, data, _ = run_json(monkeypatch, tmp_path, capsys, ["skills"])
    assert rc == 0 and data["skills"] and all(s["valid"] for s in data["skills"])

    rc, data, _ = run_json(monkeypatch, tmp_path, capsys, ["config"])
    assert rc == 0 and "llm" in data["config"] and "CODEGEN_MANIFEST" in data["env_overrides"]

    FakeFigma.list_library_components = lambda self, key: [{"name": "Button", "key": "k1"}]
    try:
        rc, data, _ = run_json(monkeypatch, tmp_path, capsys, ["components", URL])
    finally:
        del FakeFigma.list_library_components
    assert rc == 0 and data["components"] == [{"name": "Button", "key": "k1", "componentSet": None}]


def test_fetch(monkeypatch, tmp_path, capsys):
    rc, data, _ = run_json(monkeypatch, tmp_path, capsys, ["fetch", URL])
    assert rc == 0 and data["figma_json"].endswith("AbC123_12-300.json")


# --- errors ------------------------------------------------------------------------------------

def test_error_missing_baseline(monkeypatch, tmp_path, capsys):
    (tmp_path / "Hand.kt").write_text("x")
    rc, data, err = run_json(monkeypatch, tmp_path, capsys, ["diff", "Hand.kt"])
    assert rc == 2 and data["status"] == "error"
    assert data["error"] == {"type": "baseline",
                             "message": "Hand.kt has no baseline; run `figma-codegen generate` first"}
    assert "error:" in err


def test_error_bad_config(monkeypatch, tmp_path, capsys):
    rc, data, _ = run_json(monkeypatch, tmp_path, capsys, ["--config", "missing.yaml", "spec", URL])
    assert rc == 2 and data["error"]["type"] == "config" and "invalid configuration" in data["error"]["message"]


def test_error_bad_arguments(monkeypatch, tmp_path, capsys):
    rc, data, err = run_json(monkeypatch, tmp_path, capsys, ["lint", "A.kt"])   # --platform missing
    assert rc == 2 and data["command"] == "lint" and data["error"]["type"] == "usage"
    assert "--platform" in err


def test_error_figma_api(monkeypatch, tmp_path, capsys):
    def boom(self, file_key, node_id):
        req = httpx.Request("GET", "https://api.figma.com/v1/files/x/nodes")
        raise httpx.HTTPStatusError("403 Forbidden", request=req, response=httpx.Response(403, request=req))
    monkeypatch.setattr(FakeFigma, "get_nodes_response", boom)
    rc, data, err = run_json(monkeypatch, tmp_path, capsys, ["spec", URL])
    assert rc == 1 and data["status"] == "error" and data["error"]["type"] == "figma"
    assert "403" in data["error"]["message"] and "Traceback" in err


def test_error_llm(monkeypatch, tmp_path, capsys):
    def fail(**kwargs):
        raise openai.APIConnectionError(request=httpx.Request("POST", "http://llm/v1/chat/completions"))
    client = FakeClient([])
    client.chat.completions.create = fail
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: client)
    rc, data, _ = run_json(monkeypatch, tmp_path, capsys,
                           ["generate", URL, "--platform", "compose", "--out", "X.kt"])
    assert rc == 3 and data["status"] == "error" and data["error"]["type"] == "llm"
    assert data["error"]["message"].startswith("LLM request failed (APIConnectionError")


def test_without_json_output_is_unchanged(monkeypatch, tmp_path, capsys):
    assert _run_cli(monkeypatch, tmp_path, ["spec", URL]) == 0
    assert json.loads(capsys.readouterr().out)["screen"] == "LoginScreen"   # the plain spec, as before
