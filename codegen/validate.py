"""Deterministic checks: design-system lint + compile in a sandbox project."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import PlatformConfig
from .render import write_stub_entry

# (regex, message, skip_if_group1_is_zero)
DEFAULT_RULES: dict[str, list[tuple[str, str, bool]]] = {
    "compose": [
        (r"\b(\d+(?:\.\d+)?)f?\.dp\b", "raw dp value; use AppTheme.spacing / AppTheme.radius tokens", True),
        (r"\b(\d+(?:\.\d+)?)f?\.sp\b", "raw sp value; use AppTheme.typography tokens", True),
        (r"Color\s*\(\s*0x", "raw color; use AppTheme.colors tokens", False),
        (r"\bColor\.(Red|Blue|Green|Black|White|Gray|Grey|Yellow|Cyan|Magenta|DarkGray|LightGray)\b",
         "hardcoded color; use AppTheme.colors tokens", False),
        (r"import\s+androidx\.compose\.material3?\.(Button|TextButton|OutlinedButton|TextField|OutlinedTextField)\b",
         "Material component imported; use the UIKit component", False),
        (r"\bfontSize\s*=", "fontSize set directly; use AppTheme.typography tokens", False),
    ],
    "swiftui": [
        (r"\.padding\(\s*(?:\.\w+\s*,\s*)?(\d+(?:\.\d+)?)\s*\)", "raw padding; use Theme.spacing tokens", True),
        (r"spacing:\s*(\d+(?:\.\d+)?)\b", "raw spacing; use Theme.spacing tokens", True),
        (r"cornerRadius\(\s*(\d+(?:\.\d+)?)", "raw corner radius; use Theme.radius tokens", True),
        (r"Color\(\s*(?:red:|hex:|\"#|#)", "raw color; use Theme.colors tokens", False),
        (r"\bColor\.(red|blue|green|black|white|gray|yellow|orange|pink|purple)\b",
         "hardcoded color; use Theme.colors tokens", False),
        (r"\.font\(\s*\.system\(", "system font; use Theme.typography tokens", False),
        (r"\bButton\s*\(", "SwiftUI Button; use the UIKit button component", False),
        (r"\bTextField\s*\(", "SwiftUI TextField; use the UIKit text field component", False),
    ],
}


@dataclass
class ValidationResult:
    lint_issues: list[str]
    compile_ok: bool
    compile_output: str
    compile_skipped: bool

    @property
    def ok(self) -> bool:
        return not self.lint_issues and self.compile_ok

    def feedback(self) -> str:
        parts = []
        if self.lint_issues:
            parts.append("Design-system lint violations:\n" + "\n".join(f"- {i}" for i in self.lint_issues))
        if not self.compile_ok:
            parts.append("Compiler errors:\n" + self.compile_output)
        return "\n\n".join(parts)


def lint(code: str, platform: str, extra_patterns: list[str] | None = None) -> list[str]:
    rules = list(DEFAULT_RULES.get(platform, []))
    rules += [(p, f"forbidden pattern '{p}'", False) for p in extra_patterns or []]
    issues = []
    in_preview = False
    for lineno, line in enumerate(code.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(("//", "*", "/*")):
            continue
        # Preview scaffolding may use plain values; don't fail on it.
        if re.search(r"@Preview|#Preview|PreviewProvider", line):
            in_preview = True
        if in_preview:
            continue
        for pattern, msg, skip_zero in rules:
            for m in re.finditer(pattern, line):
                if skip_zero and m.groups() and m.group(1) and float(m.group(1)) == 0:
                    continue
                issues.append(f"line {lineno}: {msg}: `{stripped[:120]}`")
    return issues


def _trim_compiler_output(out: str, limit: int = 4000) -> str:
    err_lines = [l for l in out.splitlines() if re.search(r"(^e: |error:|\berror\b)", l)]
    if err_lines:
        text = "\n".join(err_lines[:40])
        return text[:limit]
    return out[-limit:]


class SandboxConfigError(RuntimeError):
    """The compile sandbox is misconfigured; the model can't fix this, so abort."""


def check_sandbox(cfg: PlatformConfig) -> None:
    if not cfg.sandbox_file or not cfg.compile_cmd:
        return
    if cfg.compile_cwd and not Path(cfg.compile_cwd).is_dir():
        raise SandboxConfigError(f"compile_cwd does not exist: {cfg.compile_cwd}")
    exe = cfg.compile_cmd[0]
    base = Path(cfg.compile_cwd or ".")
    if not (shutil.which(exe) or (base / exe).exists()):
        raise SandboxConfigError(f"compile command not found: {exe}")


def compile_code(code: str, cfg: PlatformConfig, platform: str = "compose") -> tuple[bool, str, bool]:
    """Returns (ok, output, skipped)."""
    if not cfg.sandbox_file or not cfg.compile_cmd:
        return True, "compile skipped: no sandbox configured", True
    target = Path(cfg.sandbox_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code)
    write_stub_entry(cfg, platform)
    try:
        r = subprocess.run(
            cfg.compile_cmd,
            cwd=cfg.compile_cwd,
            capture_output=True,
            text=True,
            timeout=cfg.compile_timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"compile timed out after {cfg.compile_timeout}s", False
    except FileNotFoundError as e:
        raise SandboxConfigError(f"compile command not found: {e}") from e
    if r.returncode == 0:
        return True, "", False
    return False, _trim_compiler_output(r.stdout + "\n" + r.stderr), False


def validate(code: str, platform: str, cfg: PlatformConfig) -> ValidationResult:
    issues = lint(code, platform, cfg.forbidden_patterns)
    ok, out, skipped = compile_code(code, cfg, platform)
    return ValidationResult(issues, ok, out, skipped)
