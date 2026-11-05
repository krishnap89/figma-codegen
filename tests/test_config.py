from pathlib import Path

import pytest

from codegen.cli import main
from codegen.config import load_config

YAML = """
llm:
  base_url: http://from-file:8000/v1
  model: file-model
  max_tokens: 2048
agent:
  use_tools: true
paths:
  manifest: manifest/components.json
platforms:
  compose:
    compile_cmd: ["./gradlew", "build"]
    compile_timeout: 600
"""


@pytest.fixture
def cfg_file(tmp_path):
    p = tmp_path / "conf" / "codegen.yaml"
    p.parent.mkdir()
    p.write_text(YAML)
    return p


def test_file_values_and_relative_paths(cfg_file):
    cfg = load_config(str(cfg_file), env={})
    assert cfg.llm_base_url == "http://from-file:8000/v1"
    assert cfg.max_tokens == 2048
    assert cfg.manifest_path == str(cfg_file.parent / "manifest/components.json")
    assert cfg.source == str(cfg_file.resolve())


def test_env_overrides_file(cfg_file):
    env = {"CODEGEN_LLM_BASE_URL": "http://env:9000/v1", "CODEGEN_LLM_MAX_TOKENS": "8192",
           "CODEGEN_USE_TOOLS": "false", "CODEGEN_LLM_API_KEY": "0123"}
    cfg = load_config(str(cfg_file), env=env)
    assert cfg.llm_base_url == "http://env:9000/v1"
    assert cfg.max_tokens == 8192
    assert cfg.use_tools is False
    assert cfg.llm_api_key == "0123"  # string keys are not YAML-parsed


def test_cli_overrides_beat_env(cfg_file, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(str(cfg_file), env={"CODEGEN_LLM_MODEL": "env-model"}, overrides={
        "llm.model": "cli-model",
        "platforms.compose.compile_timeout": "900",
        "platforms.compose.compile_cmd": '["make", "x"]',
        "paths.tokens": "my/tokens.json",
    })
    assert cfg.llm_model == "cli-model"
    pc = cfg.platform("compose")
    assert pc.compile_timeout == 900 and pc.compile_cmd == ["make", "x"]
    # paths given on the command line resolve against cwd, not the config file dir
    assert cfg.tokens_path == str(tmp_path / "my/tokens.json")


def test_config_path_from_env(cfg_file, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEGEN_CONFIG", str(cfg_file))
    cfg = load_config()
    assert cfg.llm_model == "file-model"


def test_missing_explicit_config_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(str(tmp_path / "nope.yaml"), env={})


def test_cli_config_command(cfg_file, capsys, monkeypatch):
    monkeypatch.delenv("CODEGEN_LLM_BASE_URL", raising=False)
    rc = main(["--config", str(cfg_file), "--base-url", "http://flag:1/v1",
               "--set", "llm.api_key=supersecretkey", "config"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "http://flag:1/v1" in out
    assert "supersecretkey" not in out


def test_cli_bad_set(cfg_file):
    with pytest.raises(SystemExit):
        main(["--config", str(cfg_file), "--set", "nodot", "config"])
