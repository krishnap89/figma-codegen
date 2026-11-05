"""Configuration.

Sources, lowest to highest precedence:
  1. built-in defaults
  2. config file: --config, else $CODEGEN_CONFIG, else ./codegen.yaml,
     else ~/.config/figma-codegen/codegen.yaml ($XDG_CONFIG_HOME respected)
  3. environment variables (see ENV_VARS)
  4. CLI overrides: --set section.key=value and shortcut flags such as --base-url

Relative paths from the config file resolve against the file's directory; relative paths
from env vars or the CLI resolve against the current working directory.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class PlatformConfig:
    # File inside a pre-built sandbox project that depends on your UIKit.
    sandbox_file: str | None = None
    compile_cmd: list[str] | None = None
    compile_cwd: str | None = None
    compile_timeout: int = 600
    # Extra regexes that must not appear in generated code (e.g. banned imports).
    forbidden_patterns: list[str] = field(default_factory=list)


@dataclass
class Config:
    llm_base_url: str = "http://localhost:8000/v1"
    llm_model: str = "qwen-coder"
    llm_api_key: str = "not-needed"
    temperature: float = 0.1
    max_tokens: int = 4096
    max_rounds: int = 4
    max_tool_calls_per_round: int = 8
    use_tools: bool = True
    figma_token: str | None = None
    manifest_path: str = "manifest/components.json"
    tokens_path: str = "manifest/tokens.json"
    examples_dir: str = "examples"
    max_examples: int = 2
    trace_dir: str = ".codegen/traces"
    # Every Figma download is saved here as <fileKey>_<nodeId>.json (reusable with --figma-json).
    figma_json_dir: str | None = ".codegen/figma"
    repo_root: str | None = None
    platforms: dict[str, PlatformConfig] = field(default_factory=dict)
    # Where the config file came from (None = defaults only). Informational.
    source: str | None = None

    def platform(self, name: str) -> PlatformConfig:
        return self.platforms.get(name, PlatformConfig())


# Environment variable -> dotted config key.
ENV_VARS: dict[str, str] = {
    "CODEGEN_LLM_BASE_URL": "llm.base_url",
    "CODEGEN_LLM_MODEL": "llm.model",
    "CODEGEN_LLM_API_KEY": "llm.api_key",
    "CODEGEN_LLM_TEMPERATURE": "llm.temperature",
    "CODEGEN_LLM_MAX_TOKENS": "llm.max_tokens",
    "CODEGEN_MAX_ROUNDS": "agent.max_rounds",
    "CODEGEN_MAX_TOOL_CALLS": "agent.max_tool_calls_per_round",
    "CODEGEN_USE_TOOLS": "agent.use_tools",
    "CODEGEN_MAX_EXAMPLES": "agent.max_examples",
    "FIGMA_TOKEN": "figma.token",
    "CODEGEN_FIGMA_TOKEN": "figma.token",
    "CODEGEN_MANIFEST": "paths.manifest",
    "CODEGEN_TOKENS": "paths.tokens",
    "CODEGEN_EXAMPLES_DIR": "paths.examples",
    "CODEGEN_TRACE_DIR": "paths.traces",
    "CODEGEN_FIGMA_JSON_DIR": "paths.figma_json",
    "CODEGEN_REPO_ROOT": "paths.repo_root",
}

_PATH_KEYS = {"paths.manifest", "paths.tokens", "paths.examples", "paths.traces", "paths.figma_json",
              "paths.repo_root"}
_PLATFORM_PATH_FIELDS = {"sandbox_file", "compile_cwd"}
_STRING_KEYS = {"llm.base_url", "llm.model", "llm.api_key", "figma.token"}


def _resolve(base: Path, value: str | None) -> str | None:
    if not value:
        return value
    p = Path(os.path.expanduser(str(value)))
    return str(p if p.is_absolute() else (base / p).resolve())


def _is_path_key(key: str) -> bool:
    parts = key.split(".")
    return key in _PATH_KEYS or (len(parts) == 3 and parts[0] == "platforms" and parts[2] in _PLATFORM_PATH_FIELDS)


def _parse_value(key: str, raw: str) -> Any:
    """Parse an override value as YAML so numbers, booleans and lists work
    (e.g. compile_cmd='["./gradlew", "build"]'). String-typed keys stay verbatim."""
    if key in _STRING_KEYS or _is_path_key(key):
        return raw
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def _set_dotted(data: dict, key: str, value: Any) -> None:
    parts = [p for p in key.split(".") if p]
    if len(parts) < 2:
        raise ValueError(f"config key must look like section.key, got {key!r}")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def find_config_file(path: str | None = None) -> Path | None:
    """Locate the config file. An explicitly named file (flag or env) must exist."""
    explicit = path or os.environ.get("CODEGEN_CONFIG")
    if explicit:
        p = Path(os.path.expanduser(explicit))
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {explicit}")
        return p
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    for candidate in (Path("codegen.yaml"), Path(xdg) / "figma-codegen" / "codegen.yaml"):
        if candidate.exists():
            return candidate
    return None


def load_config(path: str | None = None, overrides: dict[str, str] | None = None,
                env: dict[str, str] | None = None) -> Config:
    """Build the effective config.

    overrides: dotted key -> raw string value (from --set / CLI flags); highest precedence.
    env: environment mapping (defaults to os.environ); injectable for tests.
    """
    env = os.environ if env is None else env
    p = find_config_file(path)
    data: dict = {}
    if p is not None:
        data = yaml.safe_load(p.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{p}: top level must be a mapping")
    base = p.parent.resolve() if p is not None else Path.cwd()
    cwd = Path.cwd()

    # Layer env vars, then CLI overrides, onto the raw file data. Paths coming from outside the
    # file are made absolute against cwd so the file-relative resolution below leaves them alone.
    layered: list[tuple[str, Any]] = []
    for var, key in ENV_VARS.items():
        if env.get(var) not in (None, ""):
            layered.append((key, _parse_value(key, env[var])))
    for key, raw in (overrides or {}).items():
        layered.append((key, _parse_value(key, raw) if isinstance(raw, str) else raw))
    for key, value in layered:
        if _is_path_key(key) and value:
            value = _resolve(cwd, value)
        _set_dotted(data, key, value)

    cfg = _from_dict(data, base)
    cfg.source = str(p.resolve()) if p is not None else None
    return cfg


def _section(data: dict, name: str) -> dict:
    sec = data.get(name) or {}
    if not isinstance(sec, dict):
        raise ValueError(f"config section {name!r} must be a mapping")
    return sec


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _from_dict(data: dict, base: Path) -> Config:
    llm = _section(data, "llm")
    agent = _section(data, "agent")
    paths = _section(data, "paths")
    figma = _section(data, "figma")

    cfg = Config()
    cfg.llm_base_url = str(llm.get("base_url", cfg.llm_base_url))
    cfg.llm_model = str(llm.get("model", cfg.llm_model))
    cfg.llm_api_key = str(llm.get("api_key", cfg.llm_api_key))
    cfg.temperature = float(llm.get("temperature", cfg.temperature))
    cfg.max_tokens = int(llm.get("max_tokens", cfg.max_tokens))
    cfg.max_rounds = int(agent.get("max_rounds", cfg.max_rounds))
    cfg.max_tool_calls_per_round = int(agent.get("max_tool_calls_per_round", cfg.max_tool_calls_per_round))
    cfg.use_tools = _bool(agent.get("use_tools", cfg.use_tools))
    cfg.max_examples = int(agent.get("max_examples", cfg.max_examples))
    token = figma.get("token")
    cfg.figma_token = str(token) if token else None

    cfg.manifest_path = _resolve(base, paths.get("manifest", cfg.manifest_path))
    cfg.tokens_path = _resolve(base, paths.get("tokens", cfg.tokens_path))
    cfg.examples_dir = _resolve(base, paths.get("examples", cfg.examples_dir))
    cfg.trace_dir = _resolve(base, paths.get("traces", cfg.trace_dir))
    cfg.figma_json_dir = _resolve(base, paths.get("figma_json", cfg.figma_json_dir))  # empty/null disables saving
    cfg.repo_root = _resolve(base, paths.get("repo_root"))

    for name, pdata in _section(data, "platforms").items():
        pdata = pdata or {}
        cmd = pdata.get("compile_cmd")
        if isinstance(cmd, str):
            cmd = cmd.split()
        cfg.platforms[name] = PlatformConfig(
            sandbox_file=_resolve(base, pdata.get("sandbox_file")),
            compile_cmd=[str(c) for c in cmd] if cmd else None,
            compile_cwd=_resolve(base, pdata.get("compile_cwd")),
            compile_timeout=int(pdata.get("compile_timeout", 600)),
            forbidden_patterns=[str(x) for x in (pdata.get("forbidden_patterns") or [])],
        )
    return cfg


def describe(cfg: Config, show_secrets: bool = False) -> dict:
    """Effective config as a nested dict (secrets masked), for `figma-codegen config`."""
    def secret(v: str | None) -> str | None:
        if not v or show_secrets:
            return v
        return v[:4] + "…" if len(v) > 8 else "***"

    return {
        "source": cfg.source or "(defaults only — no config file found)",
        "llm": {"base_url": cfg.llm_base_url, "model": cfg.llm_model, "api_key": secret(cfg.llm_api_key),
                "temperature": cfg.temperature, "max_tokens": cfg.max_tokens},
        "agent": {"max_rounds": cfg.max_rounds, "max_tool_calls_per_round": cfg.max_tool_calls_per_round,
                  "use_tools": cfg.use_tools, "max_examples": cfg.max_examples},
        "figma": {"token": secret(cfg.figma_token)},
        "paths": {"manifest": cfg.manifest_path, "tokens": cfg.tokens_path, "examples": cfg.examples_dir,
                  "traces": cfg.trace_dir, "figma_json": cfg.figma_json_dir, "repo_root": cfg.repo_root},
        "platforms": {name: vars(pc) for name, pc in cfg.platforms.items()},
    }
