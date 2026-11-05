import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from codegen.agent import CodegenAgent, extract_code
from codegen.config import Config
from codegen.design_system import DesignSystem
from codegen.figma_client import load_node_from_file, parse_figma_url
from codegen.prompts import user_prompt
from codegen.simplify import collect, simplify
from codegen.validate import lint

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/login_nodes.json"


@pytest.fixture
def ds():
    return DesignSystem.load(ROOT / "manifest/components.json", ROOT / "manifest/tokens.json")


@pytest.fixture
def result(ds):
    return simplify(ds, load_node_from_file(str(FIXTURE)))


def test_parse_url():
    assert parse_figma_url("https://www.figma.com/design/AbC123/App?node-id=12-300&t=x") == ("AbC123", "12:300")


def test_spec_structure(result):
    root = result.spec["root"]
    assert root["type"] == "Column"
    assert root["padding"] == {"horizontal": "spacing.l", "vertical": "spacing.xl"}
    assert collect(root, "binding") == ["email", "password"]
    assert collect(root, "event") == ["onForgotPassword", "onLogin"]
    button = root["children"][7]
    assert button["props"] == {"style": "Primary", "size": "L", "enabled": True, "text": "Log in"}
    email = root["children"][3]
    assert "error" not in email["props"]  # hidden Error layer ignored
    assert result.used_components == {"Button", "TextField", "Link"}
    assert len(result.warnings) == 2


def test_token_code(ds):
    assert ds.token_code("typography.heading/l", "compose") == "AppTheme.typography.headingL"
    assert ds.token_code("color.text.primary", "swiftui") == "Theme.colors.textPrimary"


def test_prompt_contains_essentials(ds, result):
    p = user_prompt(ds, result, "compose", "LoginScreen", "com.company.login", [])
    assert "spacing.l -> AppTheme.spacing.l" in p
    assert "onEmailChange: (String) -> Unit" in p
    assert "UiTextField(" in p and "UIKitTextField" not in p


def test_lint_compose():
    bad = 'Column(Modifier.padding(16.dp)) { Text("x", color = Color(0xFF000000)) }'
    good = "Column(Modifier.padding(AppTheme.spacing.m)) { Spacer(Modifier.height(0.dp)) }"
    assert len(lint(bad, "compose")) == 2
    assert lint(good, "compose") == []


def test_lint_swiftui():
    assert lint("VStack(spacing: 12) { }.padding(16)", "swiftui")
    assert lint("VStack(spacing: Theme.spacing.m) { }.padding(.horizontal, Theme.spacing.l)", "swiftui") == []


def test_extract_code():
    assert extract_code("here\n```kotlin\nfun a() {}\n```\n", "compose") == "fun a() {}\n"
    assert extract_code("no code", "compose") is None


class FakeClient:
    """Plays back scripted assistant turns."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        content, tool_calls = self.turns.pop(0)
        return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))])


def call(name, args, id="c1"):
    return NS(id=id, function=NS(name=name, arguments=json.dumps(args)))


def test_agent_tool_call_then_fix(ds, result, tmp_path):
    bad = "```kotlin\nfun LoginScreen() { Column(Modifier.padding(16.dp)) {} }\n```"
    good = "```kotlin\nfun LoginScreen() { Column(Modifier.padding(AppTheme.spacing.m)) {} }\n```"
    client = FakeClient([
        (None, [call("get_component_docs", {"name": "Button"})]),
        (bad, None),
        (good, None),
    ])
    cfg = Config(trace_dir=str(tmp_path), examples_dir=None)
    agent = CodegenAgent(cfg, ds, client=client, log=lambda *_: None)
    res = agent.run(result, "compose", "LoginScreen")
    assert res.ok and res.rounds == 2 and res.tool_calls == 1
    assert "AppTheme.spacing.m" in res.code
    # tool result was fed back to the model
    second = client.requests[1]["messages"]
    assert second[-1]["role"] == "tool" and "UiButton" in second[-1]["content"]
    # fix round got the lint feedback and a compacted history
    third = client.requests[2]["messages"]
    assert "raw dp value" in third[-1]["content"] and len(third) == 4
    assert Path(res.trace_path).exists()


def test_agent_gives_up(ds, result, tmp_path):
    bad = "```kotlin\nval x = 8.dp\n```"
    cfg = Config(trace_dir=None, examples_dir=None, max_rounds=2, use_tools=False)
    client = FakeClient([(bad, None), (bad, None)])
    res = CodegenAgent(cfg, ds, client=client, log=lambda *_: None).run(result, "compose", "X")
    assert not res.ok and res.rounds == 2 and "tools" not in client.requests[0]
