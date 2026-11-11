"""`--json`: one machine-readable result object per command, for CI, the MCP server and IDE plugins.

With --json, stdout carries exactly one JSON object, printed when the command ends; everything a
human would read (progress, change lists, diffs, errors) goes to stderr. Every object has:

  schema, command, status, exit_code, duration_s, warnings {total, groups: [{kind, count, nodes}]}
  error {type, message}                         only when status is "error"

plus the command's own fields (see README). status is one of
  "success" | "failed" | "changes" | "up_to_date" | "error".
"""
from __future__ import annotations

import re
from typing import Any

from .report import group_warnings
from .simplify import FigmaWarning

SCHEMA = 1
EXIT_CHANGES = 10


class CliError(Exception):
    """A clean failure with a message for the user (no traceback), an exit code and an error type."""

    def __init__(self, message: str, code: int = 2, kind: str = "usage"):
        super().__init__(message)
        self.code = code
        self.kind = kind


class Report:
    """Collects a command's result. Commands always fill it; it is only printed with --json."""

    def __init__(self, command: str):
        self.command = command
        self.data: dict[str, Any] = {}
        self.status: str | None = None
        self.err: dict | None = None
        self.issues: list[FigmaWarning] = []

    def set(self, **fields: Any) -> None:
        self.data.update(fields)

    def error(self, kind: str, message: str) -> None:
        if self.err is None:          # the first error is the cause; later ones are consequences
            self.err = {"type": kind, "message": _plain(message)}

    def warnings(self, issues: list[FigmaWarning]) -> None:
        self.issues = list(issues)

    def finish(self, exit_code: int, duration_s: float) -> dict:
        status = self.status
        if status is None:
            if self.err is not None:
                status = "error"
            elif exit_code == 0:
                status = "success"
            elif exit_code == EXIT_CHANGES:
                status = "changes"
            elif exit_code == 1:
                status = "failed"
            else:
                status = "error"
        if status == "error" and self.err is None:
            self.err = {"type": "unknown", "message": f"exit code {exit_code}"}
        groups = group_warnings(self.issues)
        out: dict[str, Any] = {
            "schema": SCHEMA, "command": self.command, "status": status, "exit_code": exit_code,
            "duration_s": round(duration_s, 3),
            "warnings": {"total": len(self.issues), "groups": [
                {"kind": kind, "count": len(ws), "nodes": [{"name": w.node_name, "id": w.node_id} for w in ws]}
                for kind, ws in groups.items()]},
        }
        if status == "error":
            out["error"] = self.err
        out.update(self.data)
        return out


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    return _ANSI.sub("", text).strip()


_LINT = re.compile(r"^line (\d+): (.*)$", re.S)


def lint_issue(text: str) -> dict:
    """'line 12: raw dp value; ...: `code`' -> {"line": 12, "message": "raw dp value; ...: `code`"}"""
    m = _LINT.match(text)
    return {"line": int(m.group(1)), "message": m.group(2)} if m else {"line": None, "message": text}


def skill_plan(plan) -> dict:
    """A SkillPlan as data: what was selected and why, what was dropped over budget."""
    if plan is None:
        return {"selected": [], "dropped": []}
    return {"selected": [{"name": i.name, "reason": i.selected.reason} for i in plan.included],
            "dropped": [s.name for s in plan.dropped]}
