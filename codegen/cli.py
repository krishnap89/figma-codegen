"""figma-codegen CLI.

  figma-codegen generate <figma-url> --platform compose --out LoginScreen.kt
  figma-codegen spec <figma-url>                     # inspect the simplified spec
  figma-codegen lint LoginScreen.kt --platform compose
  figma-codegen components <library-file-url>        # help build the manifest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from .config import ENV_VARS, describe, load_config
from .design_system import DesignSystem
from .figma_client import FigmaClient, load_node_from_file, parse_figma_url
from .naming import pascal
from .simplify import simplify
from .validate import SandboxConfigError, check_sandbox, lint


def _load_node(args, cfg) -> dict:
    node_id = None
    if args.figma_url:
        file_key, node_id = parse_figma_url(args.figma_url)
    if args.figma_json:
        return load_node_from_file(args.figma_json, node_id)
    if not args.figma_url:
        sys.exit("error: pass a Figma URL or --figma-json")
    return FigmaClient(cfg.figma_token).get_node(file_key, node_id)


def _load_ds(cfg) -> DesignSystem:
    for p in (cfg.manifest_path, cfg.tokens_path):
        if not Path(p).exists():
            sys.exit(f"error: missing {p} (set paths.manifest / paths.tokens in codegen.yaml)")
    return DesignSystem.load(cfg.manifest_path, cfg.tokens_path)


def cmd_spec(args, cfg) -> int:
    ds = _load_ds(cfg)
    result = simplify(ds, _load_node(args, cfg))
    text = json.dumps(result.spec, indent=2)
    if args.out:
        Path(args.out).write_text(text)
        print(f"spec written to {args.out}")
    else:
        print(text)
    if result.warnings:
        print(f"\n{len(result.warnings)} warning(s) — fix these in Figma for better output", file=sys.stderr)
    return 0


def cmd_generate(args, cfg) -> int:
    from .agent import CodegenAgent

    if args.no_tools:
        cfg.use_tools = False
    if args.repo:
        cfg.repo_root = str(Path(args.repo).resolve())
    ds = _load_ds(cfg)
    result = simplify(ds, _load_node(args, cfg))
    if result.warnings:
        print(f"Figma warnings ({len(result.warnings)}):")
        for w in result.warnings:
            print(f"  ! {w}")
        if args.strict:
            print("aborting (--strict). Fix the design or drop --strict.")
            return 2

    try:
        check_sandbox(cfg.platform(args.platform))
    except SandboxConfigError as e:
        print(f"error: {e}\n  fix platforms.{args.platform} in codegen.yaml, or remove sandbox_file/compile_cmd to skip compiling")
        return 2

    name = args.name or pascal(Path(args.out).stem) or result.spec["screen"]
    print(f"Generating {args.platform} screen '{name}' with {cfg.llm_model} @ {cfg.llm_base_url}")
    agent = CodegenAgent(cfg, ds)
    try:
        res = agent.run(result, args.platform, name, args.package)
    except SandboxConfigError as e:
        print(f"error: {e}")
        return 2
    except Exception as e:
        if type(e).__module__.startswith("openai"):
            print(f"error: LLM request failed ({type(e).__name__}: {e}). Check llm.base_url / llm.model.")
            return 3
        raise

    out = Path(args.out)
    if res.code is None:
        print("✗ the model did not return any code")
        return 1
    if res.ok:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(res.code)
        print(f"✓ {out}  (rounds: {res.rounds}, tool calls: {res.tool_calls})")
        if res.validation and res.validation.compile_skipped:
            print("  note: not compiled — configure a sandbox in codegen.yaml")
        rc = 0
    else:
        draft = out.with_name(out.name + ".draft")
        draft.write_text(res.code)
        print(f"✗ still failing after {res.rounds} rounds. Draft saved to {draft}")
        if res.validation:
            print(res.validation.feedback())
        rc = 1
    if res.trace_path:
        print(f"  trace: {res.trace_path}")
    return rc


def cmd_lint(args, cfg) -> int:
    code = Path(args.file).read_text()
    issues = lint(code, args.platform, cfg.platform(args.platform).forbidden_patterns)
    for i in issues:
        print(i)
    print("OK" if not issues else f"{len(issues)} issue(s)")
    return 1 if issues else 0


def cmd_config(args, cfg) -> int:
    print(yaml.safe_dump(describe(cfg, args.show_secrets), sort_keys=False, allow_unicode=True))
    set_vars = [v for v in ENV_VARS if os.environ.get(v)]
    print("# env overrides active: " + (", ".join(set_vars) if set_vars else "none"))
    return 0


def cmd_components(args, cfg) -> int:
    file_key, _ = parse_figma_url(args.figma_url + ("" if "node-id" in args.figma_url else "?node-id=0-1"))
    comps = FigmaClient(cfg.figma_token).list_library_components(file_key)
    rows = [{"name": c.get("name"), "key": c.get("key"),
             "componentSet": (c.get("containing_frame") or {}).get("containingStateGroup", {}).get("name")}
            for c in comps]
    print(json.dumps(rows, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="figma-codegen", description="Figma → UIKit code with your self-hosted LLM")
    p.add_argument("--config", help="path to codegen.yaml (default: $CODEGEN_CONFIG, ./codegen.yaml, "
                                    "then ~/.config/figma-codegen/codegen.yaml)")
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                   help="override any config key, e.g. --set llm.base_url=http://host:8000/v1 "
                        "--set platforms.compose.compile_timeout=900 (repeatable)")
    p.add_argument("--base-url", help="shortcut for --set llm.base_url=...")
    p.add_argument("--api-key", help="shortcut for --set llm.api_key=... (prefer $CODEGEN_LLM_API_KEY)")
    p.add_argument("--figma-token", help="shortcut for --set figma.token=... (prefer $FIGMA_TOKEN)")
    sub = p.add_subparsers(dest="cmd", required=True)

    cf = sub.add_parser("config", help="print the effective configuration and where each part came from")
    cf.add_argument("--show-secrets", action="store_true", help="do not mask api_key / figma token")
    cf.set_defaults(func=cmd_config)

    def add_source(sp):
        sp.add_argument("figma_url", nargs="?", help="Figma frame link (Copy link to selection)")
        sp.add_argument("--figma-json", help="use a saved /v1/files/:key/nodes response instead of the API")

    g = sub.add_parser("generate", help="generate screen code from a Figma frame")
    add_source(g)
    g.add_argument("--platform", choices=["compose", "swiftui"], required=True)
    g.add_argument("--out", required=True, help="output file, e.g. LoginScreen.kt")
    g.add_argument("--name", help="screen name (default: from --out file name)")
    g.add_argument("--package", help="Kotlin package for the generated file")
    g.add_argument("--repo", help="app repo root; enables search_code/read_file tools")
    g.add_argument("--model", help="override llm.model")
    g.add_argument("--no-tools", action="store_true", help="disable tool calling (if your server lacks it)")
    g.add_argument("--strict", action="store_true", help="abort if the Figma frame has warnings")
    g.set_defaults(func=cmd_generate)

    s = sub.add_parser("spec", help="print the simplified spec for a frame")
    add_source(s)
    s.add_argument("--out")
    s.set_defaults(func=cmd_spec)

    l = sub.add_parser("lint", help="design-system lint for a file")
    l.add_argument("file")
    l.add_argument("--platform", choices=["compose", "swiftui"], required=True)
    l.set_defaults(func=cmd_lint)

    c = sub.add_parser("components", help="list published components of a Figma library file")
    c.add_argument("figma_url")
    c.set_defaults(func=cmd_components)
    return p


def _collect_overrides(args, parser) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in args.overrides:
        key, sep, value = item.partition("=")
        if not sep or "." not in key.strip():
            parser.error(f"--set expects section.key=value, got {item!r}")
        overrides[key.strip()] = value
    for flag, key in (("base_url", "llm.base_url"), ("api_key", "llm.api_key"), ("figma_token", "figma.token")):
        if getattr(args, flag):
            overrides[key] = getattr(args, flag)
    if getattr(args, "model", None):
        overrides["llm.model"] = args.model
    return overrides


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config, overrides=_collect_overrides(args, parser))
    except (FileNotFoundError, ValueError, TypeError) as e:
        print(f"error: invalid configuration: {e}", file=sys.stderr)
        return 2
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
