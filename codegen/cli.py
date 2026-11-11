"""figma-codegen CLI.

  figma-codegen generate <figma-url> --platform compose --out LoginScreen.kt
  figma-codegen spec <figma-url>                     # inspect the simplified spec
  figma-codegen fetch <figma-url>                    # only download + save the Figma JSON
  figma-codegen compare <figma-url> --code LoginScreen.kt   # Figma export vs. rendered code, for a human
  figma-codegen lint LoginScreen.kt --platform compose
  figma-codegen components <library-file-url>        # help build the manifest
  figma-codegen import-compose <uikit-module> --app <app-repo>   # draft manifest + tokens from Kotlin
  figma-codegen skills                               # list skills and check they are valid
  figma-codegen status src/                          # generated screens: Figma frame, edited since?
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

from .baseline import (SUFFIX as BASELINE_SUFFIX, BaselineError, FigmaRef, add_header, code_path_for,
                       is_code_modified, load_baseline, write_baseline)
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
from .skills import SkillPlan, scan_skills, plan_skills
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
    last_modified: str | None = None   # the file's lastModified from the Figma response, if present

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
            node = load_node_from_file(args.figma_json, node_id)
            try:
                saved = json.loads(Path(args.figma_json).read_text())
                last_modified = saved.get("lastModified") if isinstance(saved, dict) else None
            except (OSError, ValueError):
                last_modified = None
            return Frame(node, file_key, node_id, last_modified)
    if not args.figma_url:
        sys.exit("error: pass a Figma URL or --figma-json")
    with progress.step(f"fetching Figma frame {node_id}"):
        data, node_id, saved = _download(args.figma_url, cfg, args.save_figma_json)
    if saved:
        # progress goes to stderr, so `spec` output on stdout stays pure JSON
        progress.info(f"  Figma JSON saved to {saved}  (reuse offline with --figma-json {saved})")
    return Frame(data["nodes"][node_id], file_key, node_id, data.get("lastModified"))


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


def _skill_plan(cfg, ds: DesignSystem, spec: dict, platform: str, st: Style,
                forced=(), auto: bool = True) -> SkillPlan | None:
    """Load skills (reporting invalid ones) and plan this run; None when there are no skills at all.
    Raises ValueError for a bad --skill name."""
    skills, errors = scan_skills(cfg.skills_dir)
    for e in errors:
        print(st.warn(f"warning: {e}"))
    if not skills and not forced:
        return None
    return plan_skills(skills, spec, platform, ds, max_chars=cfg.max_skill_chars,
                       max_examples=cfg.max_examples, forced=forced, auto=auto)


def _print_plan(plan: SkillPlan, cfg, st: Style, stream, label: str = "skills", suffix: str = "") -> None:
    print(f"{label}: {plan.summary()}{suffix}", file=stream)
    if plan.dropped:
        print(st.warn(f"  dropped (over agent.max_skill_chars={cfg.max_skill_chars}): "
                      + ", ".join(s.name for s in plan.dropped)), file=stream)


def cmd_spec(args, cfg) -> int:
    ds, result, _ = _simplify(args, cfg, Progress())
    text = json.dumps(result.spec, indent=2)
    if args.out:
        Path(args.out).write_text(text)
        print(f"spec written to {args.out}")
    else:
        print(text)
    if result.issues:
        print("\n" + format_warnings(result.issues, args.verbose, Style.for_stream(sys.stderr)), file=sys.stderr)
    # Which skills `generate` would add (no LLM call). stderr, so stdout stays pure JSON.
    st = Style.for_stream(sys.stderr)
    skills, errors = scan_skills(cfg.skills_dir)
    for e in errors:
        print(st.warn(f"warning: {e}"), file=sys.stderr)
    if skills:
        print(file=sys.stderr)
        for platform in [args.platform] if args.platform else ["compose", "swiftui"]:
            plan = plan_skills(skills, result.spec, platform, ds, max_chars=cfg.max_skill_chars,
                               max_examples=cfg.max_examples)
            _print_plan(plan, cfg, st, sys.stderr, label=f"skills ({platform})")
    return 0


def cmd_generate(args, cfg) -> int:
    from .agent import CodegenAgent

    if args.no_tools:
        cfg.use_tools = False
    if args.no_stream:
        cfg.stream = False
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

    try:
        plan = _skill_plan(cfg, ds, result.spec, args.platform, st, args.skill, not args.no_skills)
    except ValueError as e:
        print(f"{st.err('error:')} --skill: {e}")
        return 2
    if plan is not None:
        _print_plan(plan, cfg, st, sys.stdout, suffix=" (auto-selection off: --no-skills)" if args.no_skills else "")

    name = args.name or pascal(Path(args.out).stem) or result.spec["screen"]
    print(f"Generating {args.platform} screen {st(name, 'bold')} with {st(cfg.llm_model, 'cyan')} "
          + st.dim(f"@ {cfg.llm_base_url}"))
    agent = CodegenAgent(cfg, ds, progress=progress)
    try:
        res = agent.run(result, args.platform, name, args.package, skills=plan)
    except SandboxConfigError as e:
        print(f"{st.err('error:')} {e}")
        return 2
    except Exception as e:
        if type(e).__module__.startswith("openai"):
            hint = ""
            if getattr(e, "status_code", None) == 504 and not cfg.stream:
                hint = " A gateway timed out waiting for the reply; turn streaming back on (llm.stream: true)."
            print(f"{st.err('error:')} LLM request failed ({type(e).__name__}: {e}). "
                  f"Check llm.base_url / llm.model.{hint}")
            return 3
        if isinstance(e, httpx.HTTPError):  # the connection dropped while a streamed reply was being read
            print(f"{st.err('error:')} LLM stream interrupted ({type(e).__name__}: {e}).")
            return 3
        raise

    out = Path(args.out)
    took = fmt_duration(progress.elapsed)
    if res.code is None:
        print(f"{st.mark(False)} {st.err('the model did not return any code')}  {st.dim(f'(total {took})')}")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    if res.ok:
        figma_ref = FigmaRef(frame.file_key, frame.node_id, args.figma_url, frame.last_modified)
        code = res.code if args.no_baseline else add_header(res.code, figma_ref, out)
        out.write_text(code)
        print(f"{st.mark(True)} {st(str(out), 'bold', 'green')}  "
              + st.dim(f"(rounds: {res.rounds}, tool calls: {res.tool_calls}, total {took})"))
        if not args.no_baseline:
            written = write_baseline(out, result.spec, figma_ref, args.platform, name, args.package,
                                     cfg, cfg.llm_model)
            print(st.dim(f"  baseline: {written}"))
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


def _when_summary(when: dict | None) -> str:
    if not when:
        return "only with --skill (no `when`)"
    if when["always"]:
        return "always"
    parts = [f"{x}" + (f" ≥{when['min_count'][x]}" if when["min_count"].get(x, 1) > 1 else "")
             for key in ("components", "kinds") for x in when[key]]
    if when["repeated_rows"]:
        parts.append("repeated rows")
    return "any of: " + ", ".join(parts) if parts else "never (empty `when`)"


def cmd_skills(args, cfg) -> int:
    st = Style.for_stream(sys.stdout)
    if not cfg.skills_dir or not Path(cfg.skills_dir).is_dir():
        print(f"no skills directory at {cfg.skills_dir or '(paths.skills is empty)'}")
        return 0
    skills, errors = scan_skills(cfg.skills_dir)
    try:
        ds = DesignSystem.load(cfg.manifest_path, cfg.tokens_path)
    except (OSError, ValueError):
        ds = None  # manifest checks below are skipped
    print(f"skills in {cfg.skills_dir}\n")
    rows = sorted([(s.name, s) for s in skills] + [(e.name, e) for e in errors], key=lambda r: r[0])
    for _, item in rows:
        if not hasattr(item, "body"):
            print(f"{st.mark(False)} {st(item.name, 'bold')}  {st.err('invalid:')} {item.message}")
            continue
        s = item
        n_ex = len(s.examples)
        print(f"{st.mark(True)} {st(s.name, 'bold')} — {s.description}")
        print(st.dim(f"    {', '.join(s.platforms)} · priority {s.priority} · {_when_summary(s.when)}"
                     + (f" · {n_ex} example{'s' if n_ex != 1 else ''}" if n_ex else "")
                     + (f" · {len(s.forbidden_patterns)} lint pattern(s)" if s.forbidden_patterns else "")))
        if ds is not None and s.when:
            missing = [c for c in s.when["components"] if ds.component(c) is None]
            if missing:
                print(st.warn(f"    note: not in the manifest: {', '.join(missing)}"))
    print(f"\n{len(rows)} skill(s), {len(errors)} invalid")
    return 1 if errors else 0


def _status_targets(target: Path) -> list[Path]:
    """Code files to report on: the file itself, or every file under a directory that has a baseline."""
    if target.is_dir():
        return sorted(code_path_for(b) for b in target.rglob(f"*{BASELINE_SUFFIX}"))
    if target.name.endswith(BASELINE_SUFFIX):
        return [code_path_for(target)]
    return [target]


def cmd_status(args, cfg) -> int:
    st = Style.for_stream(sys.stdout)
    target = Path(args.path)
    if not target.exists():
        print(f"{st.err('error:')} {target} does not exist")
        return 2
    files = _status_targets(target)
    if not files:
        print(f"no generated screens under {target} (no *{BASELINE_SUFFIX} files)")
        return 0
    problems = 0
    for code in files:
        try:
            b = load_baseline(code)
        except BaselineError as e:
            print(f"{st.mark(False)} {code}: {e}")
            problems += 1
            continue
        if b is None:
            print(f"{st.mark(False)} {code}: no baseline (not generated by figma-codegen, or with --no-baseline)")
            problems += 1
            continue
        if not code.is_file():
            state = st.err("missing")
        elif is_code_modified(code, b):
            state = st.warn("modified since generation")
        else:
            state = st("unmodified", "green")
        figma = (f"Figma {b.figma.file_key} node {b.figma.node_id}" if b.figma.file_key and b.figma.node_id
                 else "Figma: saved JSON, no frame URL")
        print(f"{st(str(code), 'bold')}  {state}")
        print(st.dim(f"  {b.screen} ({b.platform}) · {figma} · generated {b.generated_at or '?'}"
                     + (f" with {b.model}" if b.model else "")))
    return 1 if problems else 0


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


def cmd_import_compose(args, cfg) -> int:
    from .compose_import import format_report, run_import

    module = Path(args.module)
    if not module.exists():
        print(f"error: {module} does not exist", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir or Path(cfg.manifest_path).parent / "imported")
    targets = [out_dir / n for n in ("components.json", "tokens.json", "import-report.md")]
    existing = [t for t in targets if t.exists()]
    if existing and not args.force:
        print(f"error: {', '.join(map(str, existing))} already exist; pass --force to overwrite", file=sys.stderr)
        return 2
    base = None
    if Path(cfg.manifest_path).exists():
        base = json.loads(Path(cfg.manifest_path).read_text()).get("primitives", {}).get("compose")
    manifest, tokens, rep = run_import(module, Path(args.app) if args.app else None,
                                       Path(args.figma_components) if args.figma_components else None, base)
    if rep.files == 0:
        print(f"error: no .kt files under {module}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    targets[0].write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    targets[1].write_text(json.dumps(tokens, indent=2, ensure_ascii=False) + "\n")
    targets[2].write_text(format_report(rep, manifest, tokens))
    n_tokens = sum(len(v) for v in tokens.values())
    print(f"{rep.files} Kotlin files -> {n_tokens} tokens, {len(manifest['components'])} components")
    for t in targets:
        print(f"  wrote {t}")
    todo = len(rep.unparsed) + len(rep.no_accessor) + sum(1 for c in rep.components if c["figma_src"] != "figma")
    print(f"Review {targets[2]} ({todo} items need a human), then point paths.manifest / paths.tokens at the "
          f"drafts and run `figma-codegen spec <frame>` on a real screen.")
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
    g.add_argument("--no-stream", action="store_true",
                   help="wait for the whole reply instead of streaming it (sets llm.stream=false)")
    g.add_argument("--strict", action="store_true", help="abort if the Figma frame has warnings")
    g.add_argument("--skill", action="append", default=[], metavar="NAME",
                   help="always include this skill (repeatable; see `figma-codegen skills`)")
    g.add_argument("--no-skills", action="store_true",
                   help="turn off automatic skill selection (--skill still applies)")
    g.add_argument("--no-baseline", action="store_true",
                   help="don't write <out>.codegen.json or the provenance header (no later diff/update)")
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
    s.add_argument("--platform", choices=["compose", "swiftui"],
                   help="show the skills selected for this platform only (default: both)")
    s.set_defaults(func=cmd_spec)

    sk = sub.add_parser("skills", help="list skills with their status; exits 1 if any skill is invalid (for CI)")
    sk.set_defaults(func=cmd_skills)

    stt = sub.add_parser("status", help="generated screens: which Figma frame, when, and edited since?")
    stt.add_argument("path", help="a generated code file, or a directory to scan for *.codegen.json")
    stt.set_defaults(func=cmd_status)

    l = sub.add_parser("lint", help="design-system lint for a file")
    l.add_argument("file")
    l.add_argument("--platform", choices=["compose", "swiftui"], required=True)
    l.set_defaults(func=cmd_lint)

    c = sub.add_parser("components", help="list published components of a Figma library file")
    c.add_argument("figma_url")
    c.set_defaults(func=cmd_components)

    ic = sub.add_parser("import-compose", help="draft components.json + tokens.json from a Compose UIKit module")
    ic.add_argument("module", help="UIKit module (or source) directory, e.g. ../android-app/uikit")
    ic.add_argument("--app", help="app repo root: real call sites become the examples (module files are skipped)")
    ic.add_argument("--figma-components", metavar="JSON",
                    help="output of `figma-codegen components <library-url>`: exact Figma names and keys")
    ic.add_argument("--out-dir", help="where to write the drafts (default: <manifest dir>/imported)")
    ic.add_argument("--force", action="store_true", help="overwrite existing drafts")
    ic.set_defaults(func=cmd_import_compose)
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
