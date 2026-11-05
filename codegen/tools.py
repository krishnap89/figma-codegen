"""Read-only lookup tools the model may call during generation.

The harness, not the model, writes files and runs the compiler. Keeping the model's
tools read-only and few (2–4) is what makes tool calling reliable on mid-size models.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .design_system import DesignSystem

CODE_EXTS = {".kt", ".kts", ".swift"}
MAX_OUT = 4000


class Tools:
    def __init__(self, ds: DesignSystem, platform: str, repo_root: str | None = None):
        self.ds = ds
        self.platform = platform
        self.repo_root = Path(repo_root).resolve() if repo_root else None

    # ---------- tool implementations ----------
    def get_component_docs(self, name: str) -> str:
        doc = self.ds.component_doc(name, self.platform)
        if doc:
            return json.dumps(doc, indent=1)
        names = ", ".join(c["figma"] for c in self.ds.components)
        return f"Unknown component '{name}'. Available: {names}"

    def list_tokens(self, category: str) -> str:
        paths = self.ds.tokens_in(category)
        if not paths:
            cats = sorted({p.split('.', 1)[0] for p in self.ds.tokens})
            return f"Unknown category '{category}'. Categories: {', '.join(cats)}"
        return "\n".join(f"{p} -> {self.ds.token_code(p, self.platform)}" for p in paths)

    def search_code(self, pattern: str, max_results: int = 30) -> str:
        assert self.repo_root
        if shutil.which("rg"):
            r = subprocess.run(
                ["rg", "-n", "--no-heading", "-g", "*.kt", "-g", "*.swift", "-m", "3", pattern, str(self.repo_root)],
                capture_output=True, text=True, timeout=30,
            )
            lines = r.stdout.splitlines()
        else:
            rx = re.compile(pattern)
            lines = []
            for f in self.repo_root.rglob("*"):
                if f.suffix in CODE_EXTS and f.is_file():
                    for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                        if rx.search(line):
                            lines.append(f"{f}:{i}:{line.strip()}")
                if len(lines) >= max_results:
                    break
        lines = [l.replace(str(self.repo_root) + "/", "") for l in lines[:max_results]]
        return ("\n".join(lines) or "no matches")[:MAX_OUT]

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 200) -> str:
        assert self.repo_root
        p = (self.repo_root / path).resolve()
        if self.repo_root not in p.parents and p != self.repo_root:
            return "error: path outside repository"
        if not p.is_file():
            return f"error: file not found: {path}"
        lines = p.read_text(errors="ignore").splitlines()
        start = max(1, int(start_line))
        chunk = lines[start - 1 : start - 1 + int(max_lines)]
        return "\n".join(f"{start + i}: {l}" for i, l in enumerate(chunk))[:MAX_OUT * 2]

    # ---------- schemas / dispatch ----------
    def schemas(self) -> list[dict]:
        s = [
            _fn("get_component_docs", "Get the API signature, props and usage example of a UIKit component by its Figma name.",
                {"name": {"type": "string", "description": "Figma component name, e.g. 'Button'"}}, ["name"]),
            _fn("list_tokens", "List valid design tokens and their code expressions for one category.",
                {"category": {"type": "string", "enum": sorted({p.split('.', 1)[0] for p in self.ds.tokens})}}, ["category"]),
        ]
        if self.repo_root:
            s += [
                _fn("search_code", "Regex search over the app's Kotlin/Swift code. Returns file:line:text.",
                    {"pattern": {"type": "string"}}, ["pattern"]),
                _fn("read_file", "Read a source file from the app repository (path relative to repo root).",
                    {"path": {"type": "string"}, "start_line": {"type": "integer"}, "max_lines": {"type": "integer"}}, ["path"]),
            ]
        return s

    def dispatch(self, name: str, arguments: str | dict) -> str:
        allowed = {s["function"]["name"] for s in self.schemas()}
        if name not in allowed:
            return f"error: unknown tool '{name}'. Available: {', '.join(sorted(allowed))}"
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        except json.JSONDecodeError as e:
            return f"error: arguments are not valid JSON ({e}). Retry with a JSON object."
        try:
            return getattr(self, name)(**args)
        except TypeError as e:
            return f"error: bad arguments for {name}: {e}"
        except Exception as e:  # tool errors go back to the model, never crash the run
            return f"error: {type(e).__name__}: {e}"


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }
