"""figma-codegen CLI.

  figma-codegen generate <figma-url> --platform compose --out LoginScreen.kt
  figma-codegen spec <figma-url>                     # inspect the simplified spec
  figma-codegen fetch <figma-url>                    # only download + save the Figma JSON
  figma-codegen compare <figma-url> --code LoginScreen.kt   # Figma export vs. rendered code, for a human
  figma-codegen lint LoginScreen.kt --platform compose
  figma-codegen components <library-file-url>        # help build the manifest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from .compare import write_compare_page
from .config import ENV_VARS, describe, load_config
from .design_system import DesignSystem
from .figma_client import (FigmaClient, default_image_name, default_json_name, load_node_from_file,
                           parse_figma_url, save_nodes_response)
from .naming import pascal
from .progress import Progress, fmt_duration
from .render import RenderConfigError, RenderError, ScreenShape, check_render
from .render import render as render_screen
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


@dataclass
class Frame:
    """The Figma frame being worked on: its node JSON and, when known, where it lives."""
    node: dict
    file_key: str | None = None
    node_id: str | None = None

    @property
    def size(self) -> tuple[float, float] | None:
        box = (self.node.get("document") or {}).get("absoluteBoundingBox") or {}
        return (box["width"], box["height"]) if box.get("width") and box.get("height") else None


def _load_node(args, cfg, progress: Progress) -> Frame:
    file_key = node_id = None
    if args.figma_url:
        file_key, node_id = parse_figma_url(args.figma_url)
    if args.figma_json:
        with progress.step(f"reading {args.figma_json}"):
            return Frame(load_node_from_file(args.figma_json, node_id), file_key, node_id)
    if not args.figma_url:
        sys.exit("error: pass a Figma URL or --figma-json")
    with progress.step(f"fetching Figma frame {node_id}"):
        data, node_id, saved = _download(args.figma_url, cfg, args.save_figma_json)
    if saved:
        # progress goes to stderr, so `spec` output on stdout stays pure JSON
        progress.info(f"  Figma JSON saved to {saved}  (reuse offline with --figma-json {saved})")
    return Frame(data["nodes"][node_id], file_key, node_id)


def _simplify(args, cfg, progress: Progress) -> tuple[DesignSystem, SimplifyResult, Frame]:
    with progress.step("loading design system"):
        ds = _load_ds(cfg)
    frame = _load_node(args, cfg, progress)
    with progress.step("simplifying") as st:
        result = simplify(ds, frame.node)
        n = len(result.issues)
        st.done(f"{n} warning{'s' if n != 1 else ''}" if n else "no warnings")
    return ds, result, frame


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
    _, result, _ = _simplify(args, cfg, Progress())
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
    ds, result, frame = _simplify(args, cfg, progress)
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
    if args.compare:
        # Fail before spending model rounds, not after.
        try:
            check_render(cfg.platform(args.platform), args.platform)
        except RenderConfigError as e:
            print(f"{st.err('error:')} --compare: {e}")
            return 2
        if not (args.figma_url or args.figma_image):
            print(f"{st.err('error:')} --compare needs the Figma frame URL (or --figma-image PNG)")
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
        if args.compare:
            # A failed visual check doesn't fail the generation; the code is valid and written.
            shape = ScreenShape.from_spec(ds, result.spec["root"], args.platform, name)
            _visual_check(args, cfg, progress, st, frame=frame, platform=args.platform, code=res.code,
                          shape=shape, name=name, code_label=str(out))
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


def _figma_image(args, cfg, progress: Progress, frame: Frame | None, out_dir: Path) -> Path:
    """The designer's view of the frame: --figma-image, else a Figma export (cached next to the JSON)."""
    if args.figma_image:
        return Path(args.figma_image)
    if not (frame and frame.file_key and frame.node_id):
        raise _CheckError("the Figma image needs the frame URL (or pass --figma-image PNG)")
    cached = (Path(cfg.figma_json_dir) / default_image_name(frame.file_key, frame.node_id, args.scale)
              if cfg.figma_json_dir else out_dir / "figma.png")
    if args.figma_json and cached.exists():  # offline run: reuse the last export
        progress.info(f"  using cached Figma image {cached}")
        return cached
    with progress.step(f"exporting Figma image @{args.scale:g}x"):
        png = FigmaClient(cfg.figma_token).export_image(frame.file_key, frame.node_id, args.scale)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(png)
    return cached


class _CheckError(Exception):
    pass


def _visual_check(args, cfg, progress: Progress, st: Style, *, frame: Frame | None, platform: str | None,
                  code: str | None, shape: ScreenShape | None, name: str, code_label: str | None) -> int:
    """Figma export + rendered screen -> compare page. Returns an exit code; prints its own errors."""
    out_dir = Path(args.compare_out) if args.compare_out else Path(cfg.compare_dir) / name
    size = frame.size if frame else None
    try:
        figma_png = _figma_image(args, cfg, progress, frame, out_dir)
        if args.screenshot:
            render_png = Path(args.screenshot)
        else:
            with progress.step(f"rendering {name} ({platform}) in the sandbox"):
                render_png = render_screen(code, platform, cfg.platform(platform), shape,
                                           out_dir / "render.png", size)
        with progress.step("writing comparison page"):
            meta = {"Platform": platform or "—", "Code": code_label or "—"}
            if args.figma_url:
                meta["Figma"] = args.figma_url
            meta["Rendered from"] = str(args.screenshot) if args.screenshot else "sandbox render_cmd"
            page = write_compare_page(figma_png, render_png, out_dir, name, frame_size=size,
                                      figma_scale=args.scale, meta=meta)
    except RenderConfigError as e:
        print(f"{st.err('visual check:')} {e}")
        return 2
    except (RenderError, _CheckError, LookupError, ValueError, OSError, httpx.HTTPError) as e:
        print(f"{st.err('visual check failed:')} {e}")
        return 1
    print(f"{st.mark(True)} compare: {st(str(page.path), 'bold')}")
    for n in page.notes:
        print(st.warn(f"  note: {n}"))
    if args.open:
        webbrowser.open(page.path.resolve().as_uri())
    return 0


def cmd_compare(args, cfg) -> int:
    st = Style.for_stream(sys.stdout)
    progress = Progress()
    platform = args.platform
    if not platform and args.code:
        platform = {".kt": "compose", ".swift": "swiftui"}.get(Path(args.code).suffix)
    if not args.screenshot:
        if not args.code:
            print(f"{st.err('error:')} pass --code FILE to render, or --screenshot PNG")
            return 2
        if not platform:
            print(f"{st.err('error:')} can't tell the platform from {args.code}; pass --platform")
            return 2
    if not (args.figma_url or args.figma_json or args.figma_image):
        print(f"{st.err('error:')} pass the Figma frame URL (or --figma-image PNG)")
        return 2

    code = Path(args.code).read_text() if args.code else None
    frame = shape = None
    name = args.name or (pascal(Path(args.code).stem) if args.code else None)
    if not args.screenshot:
        # Rendering needs the spec: the entry passes default state for every binding.
        ds, result, frame = _simplify(args, cfg, progress)
        name = name or result.spec["screen"]
        shape = ScreenShape.from_spec(ds, result.spec["root"], platform, name)
    elif args.figma_json:
        frame = _load_node(args, cfg, progress)  # offline and cheap; gives the frame size
    elif args.figma_url:
        frame = Frame({}, *parse_figma_url(args.figma_url))  # the image export needs no node JSON
    name = name or "Screen"
    return _visual_check(args, cfg, progress, st, frame=frame, platform=platform, code=code, shape=shape,
                         name=name, code_label=args.code)


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

    def add_visual(sp):
        sp.add_argument("--figma-image", metavar="PNG", help="use this Figma export instead of the Figma API")
        sp.add_argument("--scale", type=float, default=2, help="Figma export scale (default 2)")
        sp.add_argument("--compare-out", metavar="DIR",
                        help="where to write the compare page (default: paths.compare/<Screen>/)")
        sp.add_argument("--open", action="store_true", help="open the compare page in the browser")

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
    g.add_argument("--compare", action="store_true",
                   help="after success, render the screen in the sandbox and write a Figma-vs-code page")
    add_visual(g)
    g.set_defaults(func=cmd_generate, screenshot=None)

    cp = sub.add_parser("compare", help="Figma image export next to the rendered screen, for a human to compare")
    add_source(cp)
    cp.add_argument("--code", help="generated file to render in the sandbox (LoginScreen.kt / .swift)")
    cp.add_argument("--platform", choices=["compose", "swiftui"], help="default: from the --code extension")
    cp.add_argument("--screenshot", metavar="PNG",
                    help="use this image of the screen (e.g. an Android Studio / Xcode preview export) "
                         "instead of rendering in the sandbox")
    cp.add_argument("--name", help="screen name (default: from --code file name)")
    add_visual(cp)
    cp.set_defaults(func=cmd_compare)

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
