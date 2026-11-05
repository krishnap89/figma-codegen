"""Render the generated screen to a PNG in the sandbox project, for a human visual check.

The generated screen's own preview is hard to call from a test (`private @Preview` in Compose,
the `#Preview` macro in SwiftUI). So the harness writes one more file into the sandbox,
`CodegenRenderTarget`, which builds the screen with default state and no-op callbacks. The
sandbox's screenshot test renders `CodegenRenderTarget()` and never has to change:

    sandbox_file          <- generated code
    render_entry_file     <- CodegenRenderTarget (this module)
    render_cmd            -> screenshot test writes $CODEGEN_RENDER_OUT

While generating, the entry file holds an empty stub so the sandbox always compiles.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import PlatformConfig
from .design_system import DesignSystem
from .prompts import PLATFORMS, typed_bindings
from .simplify import collect

ENTRY_NAME = "CodegenRenderTarget"
COMPOSE_ENTRY_PACKAGE = "codegen.render"
_HEADER = "// Written by figma-codegen for the visual check. Overwritten on every run; do not edit."


class RenderConfigError(RuntimeError):
    """Rendering is not set up for this platform; says what to configure."""


class RenderError(RuntimeError):
    """The render command failed or produced no image."""


@dataclass
class ScreenShape:
    """What the entry needs to call the screen: name, state bindings and events, in spec order."""
    name: str
    bindings: list[str]
    events: list[str]

    @classmethod
    def from_spec(cls, ds: DesignSystem, spec_root: dict, platform: str, name: str) -> "ScreenShape":
        return cls(name, [b for b, _, _ in typed_bindings(ds, spec_root, platform)], collect(spec_root, "event"))


def entry_file(cfg: PlatformConfig, platform: str) -> Path | None:
    if cfg.render_entry_file:
        return Path(cfg.render_entry_file)
    if cfg.sandbox_file:
        return Path(cfg.sandbox_file).with_name(ENTRY_NAME + PLATFORMS[platform]["ext"])
    return None


def render_enabled(cfg: PlatformConfig) -> bool:
    return bool(cfg.render_cmd and cfg.sandbox_file)


def check_render(cfg: PlatformConfig, platform: str) -> None:
    if not cfg.render_cmd:
        raise RenderConfigError(
            f"no render_cmd for {platform}: set platforms.{platform}.render_cmd in codegen.yaml "
            "(see README, 'Visual check'), or pass --screenshot with a PNG you captured yourself")
    if not cfg.sandbox_file:
        raise RenderConfigError(f"render_cmd needs platforms.{platform}.sandbox_file")
    cwd = cfg.render_cwd or cfg.compile_cwd
    if cwd and not Path(cwd).is_dir():
        raise RenderConfigError(f"render_cwd does not exist: {cwd}")
    exe = cfg.render_cmd[0]
    if not (shutil.which(exe) or (Path(cwd or ".") / exe).exists()):
        raise RenderConfigError(f"render command not found: {exe}")


def _code_package(code: str) -> str | None:
    m = re.search(r"^\s*package\s+([\w.]+)", code, re.M)
    return m.group(1) if m else None


def entry_code(platform: str, shape: ScreenShape | None, code_package: str | None = None) -> str:
    """CodegenRenderTarget source. shape=None gives the empty stub used while generating."""
    if platform == "compose":
        lines = [_HEADER, f"package {COMPOSE_ENTRY_PACKAGE}", "", "import androidx.compose.runtime.Composable"]
        if shape is None:
            return "\n".join(lines + ["", "@Composable", f"fun {ENTRY_NAME}() {{}}", ""])
        if code_package and code_package != COMPOSE_ENTRY_PACKAGE:
            lines += [f"import {code_package}.{shape.name}", f"import {code_package}.{shape.name}State"]
        args = [f"state = {shape.name}State()"]
        args += [f"on{b[:1].upper() + b[1:]}Change = {{}}" for b in shape.bindings]
        args += [f"{e} = {{}}" for e in shape.events]
        call = f"    {shape.name}(\n" + "".join(f"        {a},\n" for a in args) + "    )"
        return "\n".join(lines + ["", "@Composable", f"fun {ENTRY_NAME}() {{", call, "}", ""])

    lines = [_HEADER, "import SwiftUI", ""]
    if shape is None:
        return "\n".join(lines + [f"struct {ENTRY_NAME}: View {{", "    var body: some View { EmptyView() }", "}", ""])
    # Memberwise init: `state` first, then the event closures in the order the prompt listed them.
    args = ["state: $state"] + [f"{e}: {{}}" for e in shape.events]
    return "\n".join(lines + [
        f"struct {ENTRY_NAME}: View {{",
        f"    @State private var state = {shape.name}State()",
        "",
        "    var body: some View {",
        f"        {shape.name}({', '.join(args)})",
        "    }",
        "}",
        "",
    ])


def write_stub_entry(cfg: PlatformConfig, platform: str) -> None:
    """Keep the sandbox compiling while a new screen is generated (the old entry may call the old screen)."""
    target = entry_file(cfg, platform) if render_enabled(cfg) else None
    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(entry_code(platform, None))


def _find_output(cfg: PlatformConfig, cwd: str | None, out: Path, started: float) -> Path | None:
    if not cfg.render_output:
        return out if out.exists() else None
    pattern = cfg.render_output
    if not os.path.isabs(pattern):
        pattern = str(Path(cwd or ".") / pattern)
    fresh = [Path(p) for p in glob.glob(pattern) if os.path.getmtime(p) >= started - 1]
    return max(fresh, key=os.path.getmtime) if fresh else None


def _trim(output: str, limit: int = 3000) -> str:
    errs = [l for l in output.splitlines() if re.search(r"(^e: |error:|\berror\b|FAILED|Exception)", l)]
    return ("\n".join(errs[:30]) if errs else output[-limit:])[:limit]


def render(code: str, platform: str, cfg: PlatformConfig, shape: ScreenShape, out: Path,
           size: tuple[float, float] | None = None) -> Path:
    """Write the code + entry into the sandbox, run render_cmd, return the PNG copied to `out`.

    The command gets the frame size (dp/pt) and the expected output path as env vars and as
    {width} {height} {out} {name} placeholders. xcodebuild only forwards TEST_RUNNER_-prefixed
    vars to the test process, so those are set too.
    """
    check_render(cfg, platform)
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    raw_out = out.with_name(out.stem + ".raw.png")
    raw_out.unlink(missing_ok=True)

    pkg = _code_package(code)
    sandbox_code = code
    if platform == "compose" and not pkg:
        # Kotlin can't import from the default package; put a package-less screen next to the entry.
        sandbox_code, pkg = f"package {COMPOSE_ENTRY_PACKAGE}\n\n{code}", COMPOSE_ENTRY_PACKAGE
    Path(cfg.sandbox_file).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.sandbox_file).write_text(sandbox_code)
    entry = entry_file(cfg, platform)
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(entry_code(platform, shape, pkg))

    w, h = size or (0, 0)
    values = {"out": str(raw_out), "width": f"{w:g}", "height": f"{h:g}", "name": shape.name}
    env = dict(os.environ)
    for key, val in (("CODEGEN_RENDER_OUT", values["out"]), ("CODEGEN_RENDER_WIDTH", values["width"]),
                     ("CODEGEN_RENDER_HEIGHT", values["height"]), ("CODEGEN_RENDER_NAME", shape.name)):
        env[key] = env["TEST_RUNNER_" + key] = val
    cmd = []
    for part in cfg.render_cmd:
        for k, v in values.items():
            part = part.replace("{" + k + "}", v)
        cmd.append(part)
    cwd = cfg.render_cwd or cfg.compile_cwd

    started = time.time()
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=cfg.render_timeout)
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"render timed out after {cfg.render_timeout}s") from e
    except FileNotFoundError as e:
        raise RenderConfigError(f"render command not found: {e}") from e
    if r.returncode != 0:
        raise RenderError(f"render command failed (exit {r.returncode}):\n{_trim(r.stdout + chr(10) + r.stderr)}")
    produced = _find_output(cfg, cwd, raw_out, started)
    if not produced:
        where = cfg.render_output or "$CODEGEN_RENDER_OUT"
        raise RenderError(f"render command succeeded but wrote no new PNG at {where}")
    shutil.copyfile(produced, out)
    raw_out.unlink(missing_ok=True)
    return out
