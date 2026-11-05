"""figma-codegen CLI.

  figma-codegen generate <figma-url> --platform compose --out LoginScreen.kt
  figma-codegen spec <figma-url>                     # inspect the simplified spec
  figma-codegen fetch <figma-url>                    # only download + save the Figma JSON
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
from .figma_client import (FigmaClient, default_json_name, load_node_from_file, parse_figma_url,
                           save_nodes_response)
from .naming import pascal
from .progress import Progress, fmt_duration
from .report import format_warnings
from .simplify import SimplifyResult, simplify
from .style import Style
from .validate import SandboxConfigError, check_sandbox, lint


def _download(url: str, cfg, save_to: str | None) -> tuple[dict, str, Path | None]:
    """Download one frame from the Figma API and save the raw response.

    Saved to `save_to` if given, else to paths.figma_json/<fileKey>_<nodeId>.json
    (nothing is saved when paths.figma_json is empty). Returns (response, node_id, saved_path).
    """
    file_key, node_id = parse_figma_url(url)
    data = FigmaClient(cfg.figma_token).get_nodes_response(file_key, node_id)
    target = save_to or (str(Path(cfg.figma_json_dir) / default_json_name(file_key, node_id))
                         if cfg.figma_json_dir else None)
    saved = save_nodes_response(data, target) if target else None
    return data, node_id, saved


def _load_node(args, cfg, progress: Progress) -> dict:
    node_id = None
    if args.figma_url:
        _, node_id = parse_figma_url(args.figma_url)
    if args.figma_json:
        with progress.step(f"reading {args.figma_json}"):
            return load_node_from_file(args.figma_json, node_id)
    if not args.figma_url:
        sys.exit("error: pass a Figma URL or --figma-json")
    with progress.step(f"fetching Figma frame {node_id}"):
        data, node_id, saved = _download(args.figma_url, cfg, args.save_figma_json)
    if saved:
        # progress goes to stderr, so `spec` output on stdout stays pure JSON
        progress.info(f"  Figma JSON saved to {saved}  (reuse offline with --figma-json {saved})")
    return data["nodes"][node_id]


def _simplify(args, cfg, progress: Progress) -> tuple[DesignSystem, SimplifyResult]:
    with progress.step("loading design system"):
        ds = _load_ds(cfg)
    node = _load_node(args, cfg, progress)
    with progress.step("simplifying") as st:
        result = simplify(ds, node)
        n = len(result.issues)
        st.done(f"{n} warning{'s' if n != 1 else ''}" if n else "no warnings")
    return ds, result


def cmd_fetch(args, cfg) -> int:
    with Progress().step("fetching Figma frame"):
        _, _, saved = _download(args.figma_url, cfg, args.out)
    if not saved:
        print("error: nowhere to save; pass --out or set paths.figma_json", file=sys.stderr)
        return 2
    print(saved)
    return 0


def _load_ds(cfg) -> DesignSystem:
    for p in (cfg.manifest_path, cfg.tokens_path):
        if not Path(p).exists():
            sys.exit(f"error: missing {p} (set paths.manifest / paths.tokens in codegen.yaml)")
    return DesignSystem.load(cfg.manifest_path, cfg.tokens_path)


def cmd_spec(args, cfg) -> int:
    _, result = _simplify(args, cfg, Progress())
    text = json.dumps(result.spec, indent=2)
    if args.out:
        Path(args.out).write_text(text)
        print(f"spec written to {args.out}")
    else:
        print(text)
    if result.issues:
        print("\n" + format_warnings(result.issues, args.verbose, Style.for_stream(sys.stderr)), file=sys.stderr)
    return 0


def cmd_generate(args, cfg) -> int:
    from .agent import CodegenAgent

    if args.no_tools:
        cfg.use_tools = False
    if args.repo:
        cfg.repo_root = str(Path(args.repo).resolve())
    progress = Progress()
    st = Style.for_stream(sys.stdout)
    ds, result = _simplify(args, cfg, progress)
    if result.issues:
        print(format_warnings(result.issues, args.verbose, st))
        if args.strict:
            print(st.err("aborting (--strict).") + " Fix the design or drop --strict.")
            return 2

    try:
        check_sandbox(cfg.platform(args.platform))
    except SandboxConfigError as e:
        print(f"{st.err('error:')} {e}\n  fix platforms.{args.platform} in codegen.yaml, or remove sandbox_file/compile_cmd to skip compiling")
        return 2

    name = args.name or pascal(Path(args.out).stem) or result.spec["screen"]
    print(f"Generating {args.platform} screen {st(name, 'bold')} with {st(cfg.llm_model, 'cyan')} "
          + st.dim(f"@ {cfg.llm_base_url}"))
    agent = CodegenAgent(cfg, ds, progress=progress)
    try:
        res = agent.run(result, args.platform, name, args.package)
    except SandboxConfigError as e:
        print(f"{st.err('error:')} {e}")
        return 2
    except Exception as e:
        if type(e).__module__.startswith("openai"):
            print(f"{st.err('error:')} LLM request failed ({type(e).__name__}: {e}). Check llm.base_url / llm.model.")
            return 3
        raise

    out = Path(args.out)
    took = fmt_duration(progress.elapsed)
    if res.code is None:
        print(f"{st.mark(False)} {st.err('the model did not return any code')}  {st.dim(f'(total {took})')}")
        return 1
    if res.ok:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(res.code)
        print(f"{st.mark(True)} {st(str(out), 'bold', 'green')}  "
              + st.dim(f"(rounds: {res.rounds}, tool calls: {res.tool_calls}, total {took})"))
        if res.validation and res.validation.compile_skipped:
            print(st.warn("  note: not compiled — configure a sandbox in codegen.yaml"))
        rc = 0
    else:
        draft = out.with_name(out.name + ".draft")
        draft.write_text(res.code)
        print(f"{st.mark(False)} {st.err(f'still failing after {res.rounds} rounds')} {st.dim(f'(total {took})')}. "
              f"Draft saved to {st(str(draft), 'bold')}")
        if res.validation:
            print(res.validation.feedback())
        rc = 1
    if res.trace_path:
        print(st.dim(f"  trace: {res.trace_path}"))
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
        sp.add_argument("--save-figma-json", metavar="PATH",
                        help="where to save the downloaded Figma JSON (default: paths.figma_json/<fileKey>_<nodeId>.json)")
        sp.add_argument("-v", "--verbose", action="store_true",
                        help="list every Figma warning instead of a summary grouped by type")

    fe = sub.add_parser("fetch", help="download a frame's Figma JSON only (for offline use with --figma-json)")
    fe.add_argument("figma_url", help="Figma frame link (Copy link to selection)")
    fe.add_argument("--out", help="output file (default: paths.figma_json/<fileKey>_<nodeId>.json)")
    fe.set_defaults(func=cmd_fetch)

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
