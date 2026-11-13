"""figma-codegen CLI.

  figma-codegen generate <figma-url> --platform compose --out LoginScreen.kt
  figma-codegen spec <figma-url>                     # inspect the simplified spec
  figma-codegen fetch <figma-url>                    # only download + save the Figma JSON
  figma-codegen compare <figma-url> --code LoginScreen.kt   # Figma export vs. rendered code, for a human
  figma-codegen lint LoginScreen.kt --platform compose
  figma-codegen components <library-file-url>        # help build the manifest
  figma-codegen import-compose <uikit-module> --app <app-repo>   # draft manifest + tokens from Kotlin
  figma-codegen import-figma banner.json <component-set-url>   # manifest names, keys, props from Figma
  figma-codegen skills                               # list skills and check they are valid
  figma-codegen status src/                          # generated screens: Figma frame, edited since?
  figma-codegen diff LoginScreen.kt                  # what changed in Figma since the code was generated
  figma-codegen update LoginScreen.kt --apply        # apply only those changes to the code
  figma-codegen readiness <figma-url> --html r.html  # is this frame ready for code? (for designers)
  figma-codegen mcp                                  # MCP server for agents (pip install 'figma-codegen[mcp]')
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import traceback
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from .baseline import (SUFFIX as BASELINE_SUFFIX, BaselineError, FigmaRef, add_header, code_path_for,
                       is_code_modified, load_baseline, sha256_file, write_baseline)
from .compare import write_compare_page
from .config import ENV_VARS, describe, load_config
from .design_system import DesignSystem
from .jsonout import EXIT_CHANGES, CliError, Report, lint_issue, skill_plan
from .gaps import Gap, blocking_node_ids, collect_gaps, render_json as render_gaps_json, render_markdown
from .figma_client import (FigmaClient, default_image_name, default_json_name, load_node_from_file,
                           parse_figma_url, save_nodes_response)
from .naming import pascal
from .progress import Progress, fmt_duration
from .render import RenderConfigError, RenderError, ScreenShape, check_render
from .render import render as render_screen
from .report import format_warnings
from .prompts import update_prompt
from .simplify import SimplifyResult, simplify
from .skills import SkillPlan, scan_skills, plan_skills
from .specdiff import diff_specs, render_changes
from .style import Style
from .validate import SandboxConfigError, check_sandbox, lint


def _err(args, st: Style, message: str, code: int = 2, kind: str = "usage") -> int:
    """Print `error: <message>` and record it for --json; returns the exit code."""
    print(f"{st.err('error:')} {message}")
    args.report.error(kind, message)
    return code


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
        raise CliError("pass a Figma URL or --figma-json", code=1)
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
        result = simplify(ds, frame.node, cfg.ignore_layers)
        n = len(result.issues)
        st.done(f"{n} warning{'s' if n != 1 else ''}" if n else "no warnings")
    args.report.warnings(result.issues)
    drafts = sorted(n for n in result.used_components if (ds.component(n) or {}).get("todo"))
    if drafts:   # import-figma drafts: their code API is a guess until someone checks it
        progress.info(Style.for_stream(sys.stderr).warn(
            f"  note: unreviewed manifest draft(s) used: {', '.join(drafts)} (`todo` in {cfg.manifest_path}); "
            "their import/signature are guesses from Figma"))
    return ds, result, frame


def cmd_fetch(args, cfg) -> int:
    with Progress().step("fetching Figma frame"):
        _, _, saved = _download(args.figma_url, cfg, args.out)
    if not saved:
        raise CliError("nowhere to save; pass --out or set paths.figma_json", kind="config")
    args.report.set(figma_json=str(saved))
    print(saved)
    return 0


def _load_ds(cfg) -> DesignSystem:
    for p in (cfg.manifest_path, cfg.tokens_path):
        if not Path(p).exists():
            raise CliError(f"missing {p} (set paths.manifest / paths.tokens in codegen.yaml)", code=1, kind="config")
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


def _gaps(args, ds: DesignSystem, result: SimplifyResult, file_key: str | None, url: str | None) -> list[Gap]:
    """Design-system gaps of the frame; written to --gaps (Markdown) / --gaps-json when given."""
    gaps = collect_gaps(result.issues, ds)
    screen = result.spec.get("screen", "Screen")
    args.report.set(gaps=[g.to_json() for g in gaps])
    for path, render in ((getattr(args, "gaps", None), render_markdown),
                         (getattr(args, "gaps_json", None), render_gaps_json)):
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(render(gaps, screen, file_key, url))
            print(f"gaps report: {path} ({len(gaps)} gap{'s' if len(gaps) != 1 else ''})", file=sys.stderr)
    return gaps


def cmd_spec(args, cfg) -> int:
    ds, result, frame = _simplify(args, cfg, Progress())
    _gaps(args, ds, result, frame.file_key, args.figma_url)
    text = json.dumps(result.spec, indent=2)
    args.report.set(spec=result.spec, spec_file=args.out)
    if args.out:
        Path(args.out).write_text(text)
        print(f"spec written to {args.out}")
    elif not args.json:   # with --json the spec is in the result object
        print(text)
    if result.issues:
        print("\n" + format_warnings(result.issues, args.verbose, Style.for_stream(sys.stderr)), file=sys.stderr)
    # Which skills `generate` would add (no LLM call). stderr, so stdout stays pure JSON.
    st = Style.for_stream(sys.stderr)
    skills, errors = scan_skills(cfg.skills_dir)
    for e in errors:
        print(st.warn(f"warning: {e}"), file=sys.stderr)
    plans = {}
    if skills:
        print(file=sys.stderr)
        for platform in [args.platform] if args.platform else ["compose", "swiftui"]:
            plan = plan_skills(skills, result.spec, platform, ds, max_chars=cfg.max_skill_chars,
                               max_examples=cfg.max_examples)
            _print_plan(plan, cfg, st, sys.stderr, label=f"skills ({platform})")
            plans[platform] = skill_plan(plan)
    args.report.set(skills=plans)
    return 0


def _llm_error(args, e: Exception, cfg, st: Style) -> int | None:
    """Print an LLM endpoint error and return exit code 3; None if `e` is not one."""
    if type(e).__module__.startswith("openai"):
        hint = ""
        if getattr(e, "status_code", None) == 504 and not cfg.stream:
            hint = " A gateway timed out waiting for the reply; turn streaming back on (llm.stream: true)."
        return _err(args, st, f"LLM request failed ({type(e).__name__}: {e}). Check llm.base_url / llm.model.{hint}",
                    3, "llm")
    if isinstance(e, httpx.HTTPError):  # the connection dropped while a streamed reply was being read
        return _err(args, st, f"LLM stream interrupted ({type(e).__name__}: {e}).", 3, "llm")
    return None


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
    _gaps(args, ds, result, frame.file_key, args.figma_url)
    if result.issues:
        print(format_warnings(result.issues, args.verbose, st))
        if args.strict:
            print(st.err("aborting (--strict).") + " Fix the design or drop --strict.")
            args.report.error("design", f"aborting (--strict): the frame has {len(result.issues)} Figma warning(s)")
            return 2

    try:
        check_sandbox(cfg.platform(args.platform))
    except SandboxConfigError as e:
        return _err(args, st, f"{e}\n  fix platforms.{args.platform} in codegen.yaml, or remove "
                              "sandbox_file/compile_cmd to skip compiling", kind="config")
    if args.compare:
        # Fail before spending model rounds, not after.
        try:
            check_render(cfg.platform(args.platform), args.platform)
        except RenderConfigError as e:
            return _err(args, st, f"--compare: {e}", kind="config")
        if not (args.figma_url or args.figma_image):
            return _err(args, st, "--compare needs the Figma frame URL (or --figma-image PNG)")

    try:
        plan = _skill_plan(cfg, ds, result.spec, args.platform, st, args.skill, not args.no_skills)
    except ValueError as e:
        return _err(args, st, f"--skill: {e}")
    args.report.set(skills=skill_plan(plan))
    if plan is not None:
        _print_plan(plan, cfg, st, sys.stdout, suffix=" (auto-selection off: --no-skills)" if args.no_skills else "")

    name = args.name or pascal(Path(args.out).stem) or result.spec["screen"]
    args.report.set(screen=name, platform=args.platform, output_file=None, draft_file=None, baseline_file=None)
    print(f"Generating {args.platform} screen {st(name, 'bold')} with {st(cfg.llm_model, 'cyan')} "
          + st.dim(f"@ {cfg.llm_base_url}"))
    agent = CodegenAgent(cfg, ds, progress=progress)
    try:
        res = agent.run(result, args.platform, name, args.package, skills=plan)
    except SandboxConfigError as e:
        return _err(args, st, str(e), kind="config")
    except Exception as e:
        rc = _llm_error(args, e, cfg, st)
        if rc is None:
            raise
        return rc

    out = Path(args.out)
    took = fmt_duration(progress.elapsed)
    _report_run(args, res)
    if res.code is None:
        print(f"{st.mark(False)} {st.err('the model did not return any code')}  {st.dim(f'(total {took})')}")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    if res.ok:
        figma_ref = FigmaRef(frame.file_key, frame.node_id, args.figma_url, frame.last_modified)
        code = res.code if args.no_baseline else add_header(res.code, figma_ref, out)
        out.write_text(code)
        args.report.set(output_file=str(out))
        print(f"{st.mark(True)} {st(str(out), 'bold', 'green')}  "
              + st.dim(f"(rounds: {res.rounds}, tool calls: {res.tool_calls}, total {took})"))
        if not args.no_baseline:
            written = write_baseline(out, result.spec, figma_ref, args.platform, name, args.package,
                                     cfg, cfg.llm_model)
            args.report.set(baseline_file=str(written))
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
        args.report.set(draft_file=str(draft))
        print(f"{st.mark(False)} {st.err(f'still failing after {res.rounds} rounds')} {st.dim(f'(total {took})')}. "
              f"Draft saved to {st(str(draft), 'bold')}")
        if res.validation:
            print(res.validation.feedback())
        rc = 1
    if res.trace_path:
        print(st.dim(f"  trace: {res.trace_path}"))
    return rc


def _report_run(args, res) -> None:
    """Rounds, tool calls and the last validation of an agent run, for --json."""
    v = res.validation
    args.report.set(rounds=res.rounds, tool_calls=res.tool_calls, trace=res.trace_path,
                    lint_issues=[lint_issue(i) for i in v.lint_issues] if v else [],
                    compile={"ok": v.compile_ok, "skipped": v.compile_skipped, "output": v.compile_output}
                    if v else None)


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
        args.report.set(compare_page=None, compare_error=str(e))
        if args.cmd == "compare":
            args.report.error("config", str(e))
        return 2
    except (RenderError, _CheckError, LookupError, ValueError, OSError, httpx.HTTPError) as e:
        print(f"{st.err('visual check failed:')} {e}")
        args.report.set(compare_page=None, compare_error=str(e))
        return 1
    args.report.set(compare_page=str(page.path), compare_notes=list(page.notes))
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
            return _err(args, st, "pass --code FILE to render, or --screenshot PNG")
        if not platform:
            return _err(args, st, f"can't tell the platform from {args.code}; pass --platform")
    if not (args.figma_url or args.figma_json or args.figma_image):
        return _err(args, st, "pass the Figma frame URL (or --figma-image PNG)")

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
        args.report.set(skills_dir=cfg.skills_dir, skills=[])
        return 0
    skills, errors = scan_skills(cfg.skills_dir)
    try:
        ds = DesignSystem.load(cfg.manifest_path, cfg.tokens_path)
    except (OSError, ValueError):
        ds = None  # manifest checks below are skipped
    print(f"skills in {cfg.skills_dir}\n")
    rows = sorted([(s.name, s) for s in skills] + [(e.name, e) for e in errors], key=lambda r: r[0])
    args.report.set(skills_dir=cfg.skills_dir, skills=[
        {"name": i.name, "valid": False, "error": i.message} if not hasattr(i, "body") else
        {"name": i.name, "valid": True, "description": i.description, "platforms": list(i.platforms),
         "priority": i.priority, "when": _when_summary(i.when), "examples": len(i.examples),
         "forbidden_patterns": len(i.forbidden_patterns)} for _, i in rows])
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
        return _err(args, st, f"{target} does not exist")
    files = _status_targets(target)
    rows: list[dict] = []
    args.report.set(files=rows)
    if not files:
        print(f"no generated screens under {target} (no *{BASELINE_SUFFIX} files)")
        return 0
    problems = 0
    for code in files:
        try:
            b = load_baseline(code)
        except BaselineError as e:
            print(f"{st.mark(False)} {code}: {e}")
            rows.append({"path": str(code), "error": str(e)})
            problems += 1
            continue
        if b is None:
            print(f"{st.mark(False)} {code}: no baseline (not generated by figma-codegen, or with --no-baseline)")
            rows.append({"path": str(code), "error": "no baseline"})
            problems += 1
            continue
        modified = None if not code.is_file() else is_code_modified(code, b)
        if modified is None:
            state = st.err("missing")
        elif modified:
            state = st.warn("modified since generation")
        else:
            state = st("unmodified", "green")
        rows.append({"path": str(code), "screen": b.screen, "platform": b.platform,
                     "figma": {"file_key": b.figma.file_key, "node_id": b.figma.node_id, "url": b.figma.url},
                     "generated_at": b.generated_at, "model": b.model, "modified": modified,
                     "missing": modified is None})
        figma = (f"Figma {b.figma.file_key} node {b.figma.node_id}" if b.figma.file_key and b.figma.node_id
                 else "Figma: saved JSON, no frame URL")
        print(f"{st(str(code), 'bold')}  {state}")
        print(st.dim(f"  {b.screen} ({b.platform}) · {figma} · generated {b.generated_at or '?'}"
                     + (f" with {b.model}" if b.model else "")))
    return 1 if problems else 0


def _read_spec_file(path: str) -> dict:
    """A spec (`spec --out`), a bare root node, or a baseline file (its stored spec)."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict) and "schema" in data and isinstance(data.get("spec"), dict):
        return data["spec"]
    if isinstance(data, dict) and ("root" in data or "type" in data or "component" in data):
        return data
    raise ValueError("expected a spec (from `figma-codegen spec --out`) or a .codegen.json baseline")


def _current_vs_baseline(args, cfg, st: Style, progress: Progress):
    """Load the baseline of args.code_file and simplify the current frame (URL from the baseline unless one
    is given). Returns (baseline, design system, SimplifyResult, Frame), or an exit code after an error.
    Notes and Figma warnings go to stderr, so the change list on stdout stays clean."""
    err = Style.for_stream(sys.stderr)
    try:
        b = load_baseline(args.code_file)
    except BaselineError as e:
        return _err(args, st, str(e), kind="baseline")
    if b is None:
        return _err(args, st, f"{args.code_file} has no baseline; run `figma-codegen generate` first", kind="baseline")
    if not args.figma_url and not args.figma_json:
        if not b.figma.url:
            return _err(args, st, "the baseline has no Figma URL (generated from --figma-json); "
                                  "pass the frame URL or --figma-json", kind="baseline")
        args.figma_url = b.figma.url
    args.report.set(screen=b.screen, platform=b.platform)
    ds, result, frame = _simplify(args, cfg, progress)
    if frame.node_id and b.figma.node_id and frame.node_id != b.figma.node_id:
        print(err.warn(f"note: comparing with node {frame.node_id}, but the code was generated from "
                       f"node {b.figma.node_id}"), file=sys.stderr)
    for label, path, old_hash in (("manifest", cfg.manifest_path, b.manifest_sha256),
                                  ("tokens", cfg.tokens_path, b.tokens_sha256)):
        if old_hash and Path(path).is_file() and sha256_file(path) != old_hash:
            print(err.warn(f"note: the {label} file changed since generation; mapping changes can show up "
                           "as design changes"), file=sys.stderr)
    if result.issues:
        print(format_warnings(result.issues, args.verbose, err), file=sys.stderr)
    return b, ds, result, frame


def cmd_diff(args, cfg) -> int:
    st = Style.for_stream(sys.stdout)
    if args.specs:
        if args.gaps or args.gaps_json:
            return _err(args, st, "--gaps needs the Figma frame (its warnings), not --specs")
        if args.code_file or args.figma_url or args.figma_json:
            return _err(args, st, "--specs compares two files; drop the code file / Figma source")
        try:
            old, new = (_read_spec_file(f) for f in args.specs)
        except (OSError, ValueError) as e:
            return _err(args, st, f"--specs: {e}")
    else:
        if not args.code_file:
            return _err(args, st, "pass a generated code file (or --specs OLD.json NEW.json)")
        loaded = _current_vs_baseline(args, cfg, st, Progress())
        if isinstance(loaded, int):
            return loaded
        b, ds, result, frame = loaded
        _gaps(args, ds, result, frame.file_key or b.figma.file_key, args.figma_url or b.figma.url)
        old, new = b.spec, result.spec
    changes = diff_specs(old, new)
    args.report.set(changes=[c.to_json() for c in changes])
    args.report.status = "changes" if changes else "up_to_date"
    print(render_changes(changes, st))
    return EXIT_CHANGES if changes else 0


def _print_diff(text: str, st: Style) -> None:
    for line in text.splitlines():
        if line.startswith(("+++", "---")):
            print(st(line, "bold"))
        elif line.startswith("+"):
            print(st.ok(line))
        elif line.startswith("-"):
            print(st.err(line))
        elif line.startswith("@@"):
            print(st(line, "cyan"))
        else:
            print(line)


def cmd_update(args, cfg) -> int:
    from .agent import CodegenAgent
    from .update import SafetyCheck, plan_update, unified_diff

    if args.no_tools:
        cfg.use_tools = False
    if args.no_stream:
        cfg.stream = False
    if args.repo:
        cfg.repo_root = str(Path(args.repo).resolve())
    st = Style.for_stream(sys.stdout)
    progress = Progress()
    code_path = Path(args.code_file)
    if not code_path.is_file():
        return _err(args, st, f"{code_path} does not exist")
    loaded = _current_vs_baseline(args, cfg, st, progress)
    if isinstance(loaded, int):
        return loaded
    b, ds, result, frame = loaded
    gaps = _gaps(args, ds, result, frame.file_key or b.figma.file_key, args.figma_url or b.figma.url)
    changes = diff_specs(b.spec, result.spec)
    args.report.set(changes=[c.to_json() for c in changes], applied=[], skipped=[], conflicts=[],
                    updated_file=None, overwritten=False, baseline_file=None, diff=None)
    if not changes:
        args.report.status = "up_to_date"
        print("up to date")
        return 0
    current = code_path.read_text()
    plan = plan_update(ds, b.spec, result.spec, changes, current, b.platform, blocking_node_ids(gaps))
    print(render_changes(changes, st))
    for i, note in sorted(plan.conflicts.items()):
        print(st.warn(f"  {changes[i].node_name}: {note}"))
    if plan.deferred:
        print(st.warn("needs a UIKit change first (not applied; a TODO(design-system) comment marks the spot):"))
        for line in plan.deferred_lines():
            print(st.warn(f"  {line}"))
    args.report.set(
        applied=[c.to_json() for i, c in enumerate(changes) if i not in plan.deferred],
        skipped=[{**changes[i].to_json(), "reason": "needs a UIKit change first"} for i in plan.deferred],
        conflicts=[{**changes[i].to_json(), "note": note} for i, note in sorted(plan.conflicts.items())])
    if args.dry_run:
        args.report.status = "changes"
        return EXIT_CHANGES

    try:
        check_sandbox(cfg.platform(b.platform))
    except SandboxConfigError as e:
        return _err(args, st, f"{e}\n  fix platforms.{b.platform} in codegen.yaml", kind="config")
    modified = is_code_modified(code_path, b)
    if modified:
        print(st.warn("note: file was edited since generation; developer changes are preserved, review the diff"))

    notes = ["Changes marked 'possible conflict' touch code a developer has changed: apply them only if the "
             "intent is clear, otherwise add the TODO(design-update) comment."] if plan.conflicts else None
    prompt = update_prompt(ds, b.platform, current, plan.change_lines(), plan.components, plan.tokens, notes,
                           plan.deferred_lines())
    check = SafetyCheck(current, plan, b.platform, b.screen)
    print(f"Updating {b.platform} screen {st(b.screen, 'bold')} ({len(changes)} change"
          f"{'s' if len(changes) != 1 else ''}) with {st(cfg.llm_model, 'cyan')} " + st.dim(f"@ {cfg.llm_base_url}"))
    try:
        res = CodegenAgent(cfg, ds, progress=progress).run_update(prompt, b.platform, b.screen, check, plan.trace())
    except SandboxConfigError as e:
        return _err(args, st, str(e), kind="config")
    except Exception as e:
        rc = _llm_error(args, e, cfg, st)
        if rc is None:
            raise
        return rc

    took = fmt_duration(progress.elapsed)
    _report_run(args, res)
    args.report.set(safety_issues=res.check_issues, modified_since_generation=modified)
    if res.code is None:
        print(f"{st.mark(False)} {st.err('the model did not return any code')}  {st.dim(f'(total {took})')}. "
              f"{code_path} is unchanged.")
        return 1
    if not res.ok:
        draft = code_path.with_name(code_path.name + ".updated.draft")
        draft.write_text(res.code)
        args.report.set(draft_file=str(draft))
        print(f"{st.mark(False)} {st.err(f'still failing after {res.rounds} rounds')} {st.dim(f'(total {took})')}. "
              f"{code_path} is unchanged; draft saved to {st(str(draft), 'bold')}")
        print(res.feedback())
        if res.trace_path:
            print(st.dim(f"  trace: {res.trace_path}"))
        return 1

    figma_ref = FigmaRef(frame.file_key or b.figma.file_key, frame.node_id or b.figma.node_id,
                         args.figma_url or b.figma.url, frame.last_modified)
    new_code = add_header(res.code, figma_ref, code_path)
    apply = args.apply and (not modified or args.force)
    target = code_path if apply else code_path.with_name(code_path.name + ".updated")
    diff_text = unified_diff(current, new_code, str(code_path), str(target))
    _print_diff(diff_text, st)
    target.write_text(new_code)
    args.report.set(updated_file=str(target), overwritten=apply, diff=diff_text)
    print(f"{st.mark(True)} {st(str(target), 'bold', 'green')}  "
          + st.dim(f"(rounds: {res.rounds}, tool calls: {res.tool_calls}, total {took})"))
    if apply:
        stale = code_path.with_name(code_path.name + ".updated")
        if stale.exists():
            stale.unlink()
        written = write_baseline(code_path, result.spec, figma_ref, b.platform, b.screen, b.package, cfg,
                                 cfg.llm_model)
        args.report.set(baseline_file=str(written))
        print(st.dim(f"  baseline: {written}"))
    elif args.apply:
        print(st.warn(f"  not applied: {code_path} was edited since generation. Review the diff above, then "
                      "pass --apply --force to overwrite it."))
    else:
        print(st.dim(f"  review the diff, then run again with --apply to write {code_path} and refresh the baseline"))
    if res.validation and res.validation.compile_skipped:
        print(st.warn("  note: not compiled — configure a sandbox in codegen.yaml"))
    if res.trace_path:
        print(st.dim(f"  trace: {res.trace_path}"))
    return 0


def cmd_readiness(args, cfg) -> int:
    from .readiness import compute, render_html, render_text

    st = Style.for_stream(sys.stdout)
    _, result, frame = _simplify(args, cfg, Progress())
    r = compute(result, cfg.readiness_weights)
    screen = result.spec.get("screen", "Screen")
    args.report.set(screen=screen, **r.to_json(frame.file_key), min_score=args.min_score, html_file=args.html)
    print(render_text(r, screen, frame.file_key, st, args.verbose))
    if args.html:
        Path(args.html).parent.mkdir(parents=True, exist_ok=True)
        Path(args.html).write_text(render_html(r, screen, frame.file_key, args.figma_url))
        print(st.dim(f"\n  page: {args.html}"))
    if args.min_score is not None and r.score < args.min_score:
        print(st.err(f"\nscore {r.score} is below --min-score {args.min_score}"))
        args.report.status = "failed"
        return 2
    return 0


def cmd_mcp(args, cfg) -> int:
    from .mcp_server import INSTALL_HINT, serve

    try:
        import mcp  # noqa: F401
    except ImportError:
        raise CliError(INSTALL_HINT, kind="config") from None
    if args.http and ":" not in args.http:
        raise CliError(f"--http expects HOST:PORT, got {args.http!r}")
    serve(args.config, args.http, args.workspace)
    return 0


def cmd_lint(args, cfg) -> int:
    code = Path(args.file).read_text()
    issues = lint(code, args.platform, cfg.platform(args.platform).forbidden_patterns)
    args.report.set(file=args.file, platform=args.platform, issues=[lint_issue(i) for i in issues])
    for i in issues:
        print(i)
    print("OK" if not issues else f"{len(issues)} issue(s)")
    return 1 if issues else 0


def cmd_config(args, cfg) -> int:
    config = describe(cfg, args.show_secrets)
    print(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    set_vars = [v for v in ENV_VARS if os.environ.get(v)]
    args.report.set(config=config, env_overrides=set_vars)
    print("# env overrides active: " + (", ".join(set_vars) if set_vars else "none"))
    return 0


def cmd_components(args, cfg) -> int:
    file_key, _ = parse_figma_url(args.figma_url + ("" if "node-id" in args.figma_url else "?node-id=0-1"))
    comps = FigmaClient(cfg.figma_token).list_library_components(file_key)
    rows = [{"name": c.get("name"), "key": c.get("key"),
             "componentSet": (c.get("containing_frame") or {}).get("containingStateGroup", {}).get("name")}
            for c in comps]
    args.report.set(components=rows)
    print(json.dumps(rows, indent=2))
    return 0


def cmd_import_compose(args, cfg) -> int:
    from .compose_import import format_report, run_import

    module = Path(args.module)
    if not module.exists():
        raise CliError(f"{module} does not exist")
    out_dir = Path(args.out_dir or Path(cfg.manifest_path).parent / "imported")
    targets = [out_dir / n for n in ("components.json", "tokens.json", "import-report.md")]
    existing = [t for t in targets if t.exists()]
    if existing and not args.force:
        raise CliError(f"{', '.join(map(str, existing))} already exist; pass --force to overwrite")
    base = None
    if Path(cfg.manifest_path).exists():
        base = json.loads(Path(cfg.manifest_path).read_text()).get("primitives", {}).get("compose")
    manifest, tokens, rep = run_import(module, Path(args.app) if args.app else None,
                                       Path(args.figma_components) if args.figma_components else None, base)
    if rep.files == 0:
        raise CliError(f"no .kt files under {module}")
    out_dir.mkdir(parents=True, exist_ok=True)
    targets[0].write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    targets[1].write_text(json.dumps(tokens, indent=2, ensure_ascii=False) + "\n")
    targets[2].write_text(format_report(rep, manifest, tokens))
    n_tokens = sum(len(v) for v in tokens.values())
    args.report.set(files=[str(t) for t in targets], kotlin_files=rep.files, tokens=n_tokens,
                    components=len(manifest["components"]))
    print(f"{rep.files} Kotlin files -> {n_tokens} tokens, {len(manifest['components'])} components")
    for t in targets:
        print(f"  wrote {t}")
    todo = len(rep.unparsed) + len(rep.no_accessor) + sum(1 for c in rep.components if c["figma_src"] != "figma")
    print(f"Review {targets[2]} ({todo} items need a human), then point paths.manifest / paths.tokens at the "
          f"drafts and run `figma-codegen spec <frame>` on a real screen.")
    return 0


def _figma_sources(sources: list[str], cfg, progress: Progress) -> list[tuple[dict, str]]:
    """Figma JSON files, directories of them, or Figma links -> [(node entry, label)]."""
    from .figma_import import load_entries

    entries: list[tuple[dict, str]] = []
    for src in sources:
        if src.startswith(("http://", "https://")):
            with progress.step(f"fetching {src}"):
                try:
                    data, _, saved = _download(src, cfg, None)
                except ValueError as e:
                    raise CliError(str(e)) from None
            if saved:
                progress.info(f"  Figma JSON saved to {saved}")
            entries += [(e, src) for e in data["nodes"].values() if e and e.get("document")]
            continue
        path = Path(src)
        if not path.exists():
            raise CliError(f"{src} does not exist")
        files = sorted(path.glob("*.json")) if path.is_dir() else [path]
        for f in files:
            try:
                entries += [(e, str(f)) for e in load_entries(f)]
            except ValueError as e:
                if path.is_dir():       # other JSON in the folder (a spec, a baseline): not Figma
                    progress.info(f"  skipped {f}: {e}")
                    continue
                raise CliError(str(e)) from None
    if not entries:
        raise CliError("no Figma node JSON found in " + ", ".join(sources))
    return entries


def cmd_import_figma(args, cfg) -> int:
    from .compose_import.components import DEFAULT_PRIMITIVES
    from .figma_import import check, format_check, format_report, import_figma

    progress = Progress()
    base_path = None if args.no_base else Path(args.base or cfg.manifest_path)
    if args.base and not base_path.exists():
        raise CliError(f"--base {base_path} does not exist")
    base = json.loads(base_path.read_text()) if base_path and base_path.exists() else None
    if args.check:
        res = check(_figma_sources(args.sources, cfg, progress), base, cfg.figma_import, cfg.ignore_layers)
        print(format_check(res, Style.for_stream(sys.stdout)))
        args.report.set(checked=[{"figma": n, "key": k} for n, k in res.checked],
                        problems=[p.to_json() for p in res.problems], partial=res.partial,
                        skipped=[{"figma": n, "reason": why} for n, why in res.skipped])
        if res.problems:
            args.report.status = "failed"
        return 1 if res.problems else 0
    out = Path(args.out or Path(cfg.manifest_path).parent / "imported" / "components.json")
    report_path = out.parent / "figma-import-report.md"
    in_place = base is not None and out.resolve() == base_path.resolve()
    existing = [t for t in (out, report_path) if t.exists() and not (in_place and t == out)]
    if existing and not args.force:
        raise CliError(f"{', '.join(map(str, existing))} already exist; pass --force to overwrite")

    entries = _figma_sources(args.sources, cfg, progress)
    res = import_figma(entries, base, args.platform, {"compose": DEFAULT_PRIMITIVES}, cfg.ignore_layers,
                       cfg.figma_import)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res.manifest, indent=2, ensure_ascii=False) + "\n")
    report_path.write_text(format_report(res))
    args.report.set(files=[str(out), str(report_path)], base=str(base_path) if base is not None else None,
                    components=[{"figma": r.figma, "key": r.key, "status": r.status, "kind": r.kind,
                                 "props": r.props, "changes": r.changes, "review": r.review} for r in res.rows],
                    skipped=[{"figma": n, "reason": why} for n, why in res.skipped],
                    needs_review=res.needs_review)
    print(f"{len(entries)} Figma node(s) -> {res.count('new')} new, {res.count('updated')} updated, "
          f"{res.count('unchanged')} unchanged component(s); {len(res.skipped)} skipped (icons, private)")
    print(f"  base  {base_path if base is not None else '(none: new manifest)'}")
    print(f"  wrote {out}")
    print(f"  wrote {report_path}")
    print(f"Review {report_path} ({res.needs_review} items need a human), then point paths.manifest at "
          f"{out} (or copy the entries over) and run `figma-codegen spec <frame>` on a real screen.")
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
    p.add_argument("--json", action="store_true",
                   help="print one JSON result object on stdout (human output goes to stderr); for CI and tools")
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

    def add_gaps(sp):
        sp.add_argument("--gaps", metavar="FILE.md",
                        help="write a design-system gap report (missing components, variants, tokens)")
        sp.add_argument("--gaps-json", metavar="FILE", help="the same gap report as JSON (for ticket automation)")

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
    add_gaps(g)
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
    add_gaps(s)
    s.add_argument("--out")
    s.add_argument("--platform", choices=["compose", "swiftui"],
                   help="show the skills selected for this platform only (default: both)")
    s.set_defaults(func=cmd_spec)

    sk = sub.add_parser("skills", help="list skills with their status; exits 1 if any skill is invalid (for CI)")
    sk.set_defaults(func=cmd_skills)

    stt = sub.add_parser("status", help="generated screens: which Figma frame, when, and edited since?")
    stt.add_argument("path", help="a generated code file, or a directory to scan for *.codegen.json")
    stt.set_defaults(func=cmd_status)

    d = sub.add_parser("diff", help="what changed in the Figma frame since the code was generated (no AI); "
                                    "exit 10 when there are changes")
    d.add_argument("code_file", nargs="?", help="a generated file with a baseline (<file>.codegen.json)")
    add_source(d)
    add_gaps(d)
    d.add_argument("--specs", nargs=2, metavar=("OLD", "NEW"),
                   help="compare two spec files (`spec --out`, or .codegen.json baselines) instead")
    d.set_defaults(func=cmd_diff)

    u = sub.add_parser("update", help="apply what changed in Figma to an existing screen, keeping developer edits")
    u.add_argument("code_file", help="a generated file with a baseline (<file>.codegen.json)")
    add_source(u)
    add_gaps(u)
    u.add_argument("--dry-run", action="store_true", help="only print the change list (exit 10 if there are changes)")
    u.add_argument("--apply", action="store_true",
                   help="overwrite the file and refresh the baseline (default: write <file>.updated)")
    u.add_argument("--force", action="store_true",
                   help="with --apply: overwrite even if the file was edited since generation")
    u.add_argument("--repo", help="app repo root; enables search_code/read_file tools")
    u.add_argument("--model", help="override llm.model")
    u.add_argument("--no-tools", action="store_true", help="disable tool calling (if your server lacks it)")
    u.add_argument("--no-stream", action="store_true", help="wait for the whole reply instead of streaming it")
    u.set_defaults(func=cmd_update)

    rd = sub.add_parser("readiness", help="score a Figma frame 0–100 for code readiness, with how to fix it in Figma")
    add_source(rd)
    rd.add_argument("--html", metavar="FILE", help="also write a self-contained page designers can open")
    rd.add_argument("--min-score", type=int, metavar="N",
                    help="exit 2 if the score is below N (for CI or handoff gates)")
    rd.set_defaults(func=cmd_readiness)

    m = sub.add_parser("mcp", help="run the MCP server (stdio; --http for streamable HTTP) for IDE agents")
    m.add_argument("--http", metavar="HOST:PORT", help="serve streamable HTTP instead of stdio, e.g. 127.0.0.1:8765")
    m.add_argument("--workspace", metavar="DIR",
                   help="only files under DIR may be read or written (default: mcp.workspace_root, else cwd)")
    m.set_defaults(func=cmd_mcp)

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

    imf = sub.add_parser("import-figma", help="draft or update components.json from Figma component JSON "
                                              "(names, keys, variants, props)")
    imf.add_argument("sources", nargs="+", metavar="SOURCE",
                     help="saved Figma JSON (`fetch` output), a directory of them, or a Figma link to a "
                          "component set, component or page")
    imf.add_argument("--base", metavar="JSON",
                     help="manifest to update; its code sections are kept (default: paths.manifest, if it exists)")
    imf.add_argument("--no-base", action="store_true", help="start a new manifest instead of updating one")
    imf.add_argument("--out", metavar="JSON",
                     help="where to write the manifest (default: <manifest dir>/imported/components.json); "
                          "the same path as --base updates it in place")
    imf.add_argument("--platform", choices=["compose", "swiftui"], action="append",
                     help="platform section(s) to draft for new components (default: the base's primitives)")
    imf.add_argument("--force", action="store_true", help="overwrite existing output")
    imf.add_argument("--check", action="store_true",
                     help="write nothing; list where the library breaks the naming conventions (figma_import in "
                          "codegen.yaml), with how to fix each in Figma. Exit 1 if there are problems")
    imf.set_defaults(func=cmd_import_figma)
    p.commands = list(sub.choices)
    for sp in sub.choices.values():   # `figma-codegen spec --json ...` works too
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
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


def _run(args, parser) -> int:
    try:
        cfg = load_config(args.config, overrides=_collect_overrides(args, parser))
    except (FileNotFoundError, ValueError, TypeError) as e:
        print(f"error: invalid configuration: {e}", file=sys.stderr)
        args.report.error("config", f"invalid configuration: {e}")
        return 2
    try:
        return args.func(args, cfg)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        args.report.error(e.kind, str(e))
        return e.code


def _json_main(argv: list[str] | None, parser: argparse.ArgumentParser) -> int:
    """--json: human output to stderr, one result object on stdout, never a traceback on stdout."""
    real_stdout = sys.stdout
    start = time.monotonic()
    raw = sys.argv[1:] if argv is None else argv
    report = Report(next((a for a in raw if a in parser.commands), None))
    with contextlib.redirect_stdout(sys.stderr):
        try:
            args = parser.parse_args(argv)
            report.command = args.cmd
            args.report = report
            rc = _run(args, parser)
        except SystemExit as e:              # argparse error, or --help
            rc = e.code if isinstance(e.code, int) else 2
            if rc:
                report.error("usage", "invalid arguments (see the usage message on stderr)")
        except Exception as e:
            traceback.print_exc()
            kind = ("figma" if isinstance(e, (httpx.HTTPError, LookupError)) else
                    "io" if isinstance(e, OSError) else "internal")
            report.error(kind, f"{type(e).__name__}: {e}")
            rc = 1
    print(json.dumps(report.finish(rc, time.monotonic() - start), ensure_ascii=False, default=str),
          file=real_stdout)
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if "--json" in (sys.argv[1:] if argv is None else argv):
        return _json_main(argv, parser)
    args = parser.parse_args(argv)
    args.report = Report(args.cmd)
    return _run(args, parser)


if __name__ == "__main__":
    raise SystemExit(main())
