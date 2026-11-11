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
    # Visual check (`compare`, `generate --compare`): a command in the sandbox that screenshots
    # CodegenRenderTarget (written by the harness to render_entry_file) to $CODEGEN_RENDER_OUT.
    render_cmd: list[str] | None = None
    render_cwd: str | None = None          # default: compile_cwd
    render_entry_file: str | None = None   # default: CodegenRenderTarget.kt/.swift next to sandbox_file
    render_output: str | None = None       # PNG (or glob) the command writes; default: $CODEGEN_RENDER_OUT
    render_timeout: int = 600


READINESS_WEIGHTS: dict[str, float] = {
    "missing_component": 6, "no_auto_layout": 6, "hand_drawn_graphic": 3, "unbound_style": 2,
    "value_without_token": 2, "default_name": 1, "unknown_variant": 3,
}
# Same default as simplify.DEFAULT_IGNORE_LAYERS (kept here so config doesn't import the simplifier).
DEFAULT_IGNORE_LAYERS = ["_*", "*Statusbar*", "*Status Bar*", "*Home Indicator*", "*Keyboard*"]


@dataclass
class Config:
    llm_base_url: str = "http://localhost:8000/v1"
    llm_model: str = "qwen-coder"
    llm_api_key: str = "not-needed"
    temperature: float = 0.1
    max_tokens: int = 4096
    # Stream replies so tokens flow while the model works; a gateway/proxy in front of the server
    # (nginx, a load balancer) may return 504 for a non-streamed request that takes minutes.
    stream: bool = True
    max_rounds: int = 4
    max_tool_calls_per_round: int = 8
    use_tools: bool = True
    figma_token: str | None = None
    manifest_path: str = "manifest/components.json"
    tokens_path: str = "manifest/tokens.json"
    examples_dir: str = "examples"
    max_examples: int = 2
    # Skills: team conventions added to the prompt only when a screen needs them (see skills.py).
    skills_dir: str | None = "skills"
    max_skill_chars: int = 6000    # skill bodies + examples per prompt; lowest priority dropped first
    skill_tool: bool = False       # offer `load_skill` so the model can pull in unselected skills
    trace_dir: str = ".codegen/traces"
    # Every Figma download is saved here as <fileKey>_<nodeId>.json (reusable with --figma-json).
    figma_json_dir: str | None = ".codegen/figma"
    # Side-by-side Figma vs. rendered-screen pages from `compare` / `generate --compare`.
    compare_dir: str = ".codegen/compare"
    repo_root: str | None = None
    platforms: dict[str, PlatformConfig] = field(default_factory=dict)
    # `readiness`: issue weights, and device-decoration layers ignored everywhere (see readiness.py).
    readiness_weights: dict[str, float] = field(default_factory=lambda: dict(READINESS_WEIGHTS))
    ignore_layers: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_LAYERS))
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
    "CODEGEN_LLM_STREAM": "llm.stream",
    "CODEGEN_MAX_ROUNDS": "agent.max_rounds",
    "CODEGEN_MAX_TOOL_CALLS": "agent.max_tool_calls_per_round",
    "CODEGEN_USE_TOOLS": "agent.use_tools",
    "CODEGEN_MAX_EXAMPLES": "agent.max_examples",
    "CODEGEN_MAX_SKILL_CHARS": "agent.max_skill_chars",
    "CODEGEN_SKILL_TOOL": "agent.skill_tool",
    "FIGMA_TOKEN": "figma.token",
    "CODEGEN_FIGMA_TOKEN": "figma.token",
    "CODEGEN_MANIFEST": "paths.manifest",
    "CODEGEN_TOKENS": "paths.tokens",
    "CODEGEN_EXAMPLES_DIR": "paths.examples",
    "CODEGEN_SKILLS_DIR": "paths.skills",
    "CODEGEN_TRACE_DIR": "paths.traces",
    "CODEGEN_FIGMA_JSON_DIR": "paths.figma_json",
    "CODEGEN_COMPARE_DIR": "paths.compare",
    "CODEGEN_REPO_ROOT": "paths.repo_root",
}

_PATH_KEYS = {"paths.manifest", "paths.tokens", "paths.examples", "paths.skills", "paths.traces",
              "paths.figma_json", "paths.compare", "paths.repo_root"}
_PLATFORM_PATH_FIELDS = {"sandbox_file", "compile_cwd", "render_cwd", "render_entry_file", "render_output"}
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
    readiness = _section(data, "readiness")

    cfg = Config()
    weights = readiness.get("weights") or {}
    if not isinstance(weights, dict):
        raise ValueError("readiness.weights must be a mapping, e.g. {missing_component: 6}")
    unknown = sorted(set(weights) - set(READINESS_WEIGHTS))
    if unknown:
        raise ValueError(f"readiness.weights: unknown issue(s) {', '.join(unknown)}; "
                         f"known: {', '.join(READINESS_WEIGHTS)}")
    cfg.readiness_weights.update({k: float(v) for k, v in weights.items()})
    if "ignore_layers" in readiness:
        layers = readiness["ignore_layers"] or []
        cfg.ignore_layers = [layers] if isinstance(layers, str) else [str(x) for x in layers]
    cfg.llm_base_url = str(llm.get("base_url", cfg.llm_base_url))
    cfg.llm_model = str(llm.get("model", cfg.llm_model))
    cfg.llm_api_key = str(llm.get("api_key", cfg.llm_api_key))
    cfg.temperature = float(llm.get("temperature", cfg.temperature))
    cfg.max_tokens = int(llm.get("max_tokens", cfg.max_tokens))
    cfg.stream = _bool(llm.get("stream", cfg.stream))
    cfg.max_rounds = int(agent.get("max_rounds", cfg.max_rounds))
    cfg.max_tool_calls_per_round = int(agent.get("max_tool_calls_per_round", cfg.max_tool_calls_per_round))
    cfg.use_tools = _bool(agent.get("use_tools", cfg.use_tools))
    cfg.max_examples = int(agent.get("max_examples", cfg.max_examples))
    cfg.max_skill_chars = int(agent.get("max_skill_chars", cfg.max_skill_chars))
    cfg.skill_tool = _bool(agent.get("skill_tool", cfg.skill_tool))
    token = figma.get("token")
    cfg.figma_token = str(token) if token else None

    cfg.manifest_path = _resolve(base, paths.get("manifest", cfg.manifest_path))
    cfg.tokens_path = _resolve(base, paths.get("tokens", cfg.tokens_path))
    cfg.examples_dir = _resolve(base, paths.get("examples", cfg.examples_dir))
    cfg.skills_dir = _resolve(base, paths.get("skills", cfg.skills_dir))  # empty/null disables skills
    cfg.trace_dir = _resolve(base, paths.get("traces", cfg.trace_dir))
    cfg.figma_json_dir = _resolve(base, paths.get("figma_json", cfg.figma_json_dir))  # empty/null disables saving
    cfg.compare_dir = _resolve(base, paths.get("compare") or cfg.compare_dir)
    cfg.repo_root = _resolve(base, paths.get("repo_root"))

    def cmd_list(value: Any) -> list[str] | None:
        if isinstance(value, str):
            value = value.split()
        return [str(c) for c in value] if value else None

    for name, pdata in _section(data, "platforms").items():
        pdata = pdata or {}
        compile_timeout = int(pdata.get("compile_timeout", 600))
        cfg.platforms[name] = PlatformConfig(
            sandbox_file=_resolve(base, pdata.get("sandbox_file")),
            compile_cmd=cmd_list(pdata.get("compile_cmd")),
            compile_cwd=_resolve(base, pdata.get("compile_cwd")),
            compile_timeout=compile_timeout,
            forbidden_patterns=[str(x) for x in (pdata.get("forbidden_patterns") or [])],
            render_cmd=cmd_list(pdata.get("render_cmd")),
            render_cwd=_resolve(base, pdata.get("render_cwd")),
            render_entry_file=_resolve(base, pdata.get("render_entry_file")),
            render_output=_resolve(base, pdata.get("render_output")),
            render_timeout=int(pdata.get("render_timeout", compile_timeout)),
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
                "temperature": cfg.temperature, "max_tokens": cfg.max_tokens, "stream": cfg.stream},
        "agent": {"max_rounds": cfg.max_rounds, "max_tool_calls_per_round": cfg.max_tool_calls_per_round,
                  "use_tools": cfg.use_tools, "max_examples": cfg.max_examples,
                  "max_skill_chars": cfg.max_skill_chars, "skill_tool": cfg.skill_tool},
        "figma": {"token": secret(cfg.figma_token)},
        "paths": {"manifest": cfg.manifest_path, "tokens": cfg.tokens_path, "examples": cfg.examples_dir,
                  "skills": cfg.skills_dir,
                  "traces": cfg.trace_dir, "figma_json": cfg.figma_json_dir, "compare": cfg.compare_dir,
                  "repo_root": cfg.repo_root},
        "platforms": {name: vars(pc) for name, pc in cfg.platforms.items()},
        "readiness": {"weights": cfg.readiness_weights, "ignore_layers": cfg.ignore_layers},
    }
