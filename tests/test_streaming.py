"""Streamed replies: request flag, chunk assembly (text + tool calls), and the non-streamed fallback."""
from types import SimpleNamespace as NS

from codegen.agent import CodegenAgent, read_stream
from codegen.config import Config, load_config

from test_pipeline import FakeClient, call, ds, result  # noqa: F401  (fixtures)

GOOD = "```kotlin\nfun LoginScreen() { Column(Modifier.padding(AppTheme.spacing.m)) {} }\n```"


def delta(content=None, tool_calls=None):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=tool_calls))])


def tc(index, id=None, name=None, arguments=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=arguments))


def test_stream_is_requested_by_default_and_tool_calls_survive(ds, result):
    client = FakeClient([(None, [call("get_component_docs", {"name": "Button"})]), (GOOD, None)])
    cfg = Config(trace_dir=None, examples_dir=None)
    res = CodegenAgent(cfg, ds, client=client).run(result, "compose", "LoginScreen")
    assert res.ok and res.tool_calls == 1
    assert all(r["stream"] is True for r in client.requests)
    assert "UiButton" in client.requests[1]["messages"][-1]["content"]   # streamed tool call was dispatched


def test_stream_false_uses_the_whole_response(ds, result):
    client = FakeClient([(GOOD, None)])
    cfg = Config(trace_dir=None, examples_dir=None, stream=False)
    res = CodegenAgent(cfg, ds, client=client).run(result, "compose", "LoginScreen")
    assert res.ok and client.requests[0]["stream"] is False


def test_read_stream_assembles_interleaved_parallel_tool_calls():
    chunks = [
        delta("Let me "), delta("check."),
        delta(tool_calls=[tc(0, "a", "list_tokens", ""), tc(1, None, "get_component_docs", '{"na')]),
        delta(tool_calls=[tc(0, None, None, '{"category": '), tc(1, None, None, 'me": "Button"}')]),
        delta(tool_calls=[tc(0, None, "list_tokens", '"color"}')]),   # name repeated by some servers
        NS(choices=[]),
    ]
    seen = []
    msg = read_stream(chunks, seen.append)
    assert msg.content == "Let me check."
    assert [(c.id, c.function.name, c.function.arguments) for c in msg.tool_calls] == [
        ("a", "list_tokens", '{"category": "color"}'),
        ("call_1", "get_component_docs", '{"name": "Button"}'),     # missing id gets a stable one
    ]
    assert seen[-1] == len("Let me check.") + len('{"category": "color"}') + len('{"name": "Button"}')


def test_read_stream_text_only():
    msg = read_stream([delta("```kotlin\n"), delta("fun A() {}\n```")])
    assert msg.content == "```kotlin\nfun A() {}\n```" and msg.tool_calls is None
    assert read_stream([]).content is None


def test_stream_config_switches(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert load_config(env={}).stream is True
    assert load_config(env={"CODEGEN_LLM_STREAM": "false"}).stream is False
    assert load_config(overrides={"llm.stream": "false"}, env={}).stream is False


def test_cli_no_stream(monkeypatch, tmp_path):
    import openai

    from test_pipeline import FIXTURE, _run_cli
    client = FakeClient([(GOOD, None)])
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: client)
    rc = _run_cli(monkeypatch, tmp_path, ["generate", "--figma-json", str(FIXTURE), "--platform", "compose",
                                          "--out", "L.kt", "--no-tools", "--no-stream"])
    assert rc == 0 and client.requests[0]["stream"] is False
