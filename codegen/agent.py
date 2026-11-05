"""The agent loop.

  round:  model (may call read-only lookup tools) -> final code block
          harness lints + compiles
          on failure: errors go back to the model, next round
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .design_system import DesignSystem
from .prompts import (PLATFORMS, fix_prompt, load_examples, no_code_prompt,
                      system_prompt, user_prompt)
from .simplify import SimplifyResult
from .tools import Tools
from .validate import ValidationResult, validate


@dataclass
class AgentResult:
    ok: bool
    code: str | None
    rounds: int
    validation: ValidationResult | None
    trace_path: str | None = None
    tool_calls: int = 0
    notes: list[str] = field(default_factory=list)


class Tracer:
    """JSONL trace of every request/response/tool call/validation, for debugging prompts."""

    def __init__(self, trace_dir: str | None, screen: str):
        self.path = None
        if trace_dir:
            d = Path(trace_dir)
            d.mkdir(parents=True, exist_ok=True)
            self.path = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{screen}.jsonl"

    def log(self, event: str, **data: Any) -> None:
        if self.path:
            with self.path.open("a") as f:
                f.write(json.dumps({"t": round(time.time(), 3), "event": event, **data}, default=str) + "\n")


def extract_code(content: str, platform: str) -> str | None:
    fence = PLATFORMS[platform]["fence"]
    blocks = re.findall(r"```([a-zA-Z]*)\s*\n(.*?)```", content or "", re.S)
    preferred = [b for lang, b in blocks if lang.lower() in (fence, "kt", "swift")]
    candidates = preferred or [b for _, b in blocks]
    if candidates:
        return max(candidates, key=len).strip() + "\n"
    text = (content or "").strip()
    if text.startswith(("package ", "import ")):
        return text + "\n"
    return None


def _msg_to_dict(msg: Any) -> dict:
    d: dict = {"role": "assistant", "content": msg.content or ""}
    if getattr(msg, "tool_calls", None):
        d["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in msg.tool_calls
        ]
    return d


class CodegenAgent:
    def __init__(self, cfg: Config, ds: DesignSystem, client: Any = None, log: Callable[[str], None] = print):
        self.cfg = cfg
        self.ds = ds
        self.log = log
        if client is None:
            from openai import OpenAI
            client = OpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key)
        self.client = client

    def run(self, result: SimplifyResult, platform: str, screen_name: str, package: str | None = None) -> AgentResult:
        tracer = Tracer(self.cfg.trace_dir, screen_name)
        tools = Tools(self.ds, platform, self.cfg.repo_root)
        examples = load_examples(self.cfg.examples_dir, platform, self.cfg.max_examples)
        messages: list[dict] = [
            {"role": "system", "content": system_prompt(platform)},
            {"role": "user", "content": user_prompt(self.ds, result, platform, screen_name, package, examples)},
        ]
        tracer.log("start", platform=platform, screen=screen_name, model=self.cfg.llm_model,
                   spec=result.spec, examples=len(examples))

        out = AgentResult(ok=False, code=None, rounds=0, validation=None,
                          trace_path=str(tracer.path) if tracer.path else None)
        for rnd in range(1, self.cfg.max_rounds + 1):
            out.rounds = rnd
            self.log(f"round {rnd}: generating…")
            content, n_calls = self._complete(messages, tools, tracer)
            out.tool_calls += n_calls
            code = extract_code(content, platform)
            if not code:
                self.log("  no code block in answer; asking again")
                tracer.log("no_code", content=content)
                messages.append({"role": "user", "content": no_code_prompt(platform)})
                continue

            out.code = code
            v = validate(code, platform, self.cfg.platform(platform))
            out.validation = v
            tracer.log("validation", round=rnd, lint=v.lint_issues, compile_ok=v.compile_ok,
                       compile_output=v.compile_output, compile_skipped=v.compile_skipped)
            if v.ok:
                self.log(f"  ✓ valid{' (compile skipped: no sandbox configured)' if v.compile_skipped else ''}")
                out.ok = True
                return out
            self.log(f"  ✗ {len(v.lint_issues)} lint issue(s), compile {'ok' if v.compile_ok else 'failed'}")
            messages.append({"role": "user", "content": fix_prompt(v.feedback(), platform)})
            self._compact(messages)
        return out

    def _complete(self, messages: list[dict], tools: Tools, tracer: Tracer) -> tuple[str, int]:
        """One model turn, allowing up to N lookup tool calls before the final answer."""
        calls = 0
        schemas = tools.schemas() if self.cfg.use_tools else None
        for i in range(self.cfg.max_tool_calls_per_round + 1):
            kwargs: dict = dict(model=self.cfg.llm_model, messages=messages,
                                temperature=self.cfg.temperature, max_tokens=self.cfg.max_tokens)
            offer_tools = schemas and i < self.cfg.max_tool_calls_per_round
            if offer_tools:
                kwargs["tools"] = schemas
            resp = self.client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            messages.append(_msg_to_dict(msg))
            tracer.log("response", content=msg.content,
                       tool_calls=[c.function.name for c in (msg.tool_calls or [])])
            if not msg.tool_calls:
                return msg.content or "", calls
            for call in msg.tool_calls:
                calls += 1
                result = tools.dispatch(call.function.name, call.function.arguments)
                self.log(f"  tool {call.function.name}({call.function.arguments})")
                tracer.log("tool", name=call.function.name, args=call.function.arguments, result=result[:2000])
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            if not offer_tools:
                # Model called tools although none were offered; force a final answer.
                messages.append({"role": "user", "content": "Stop calling tools and give the final code block now."})
        return "", calls

    @staticmethod
    def _compact(messages: list[dict]) -> None:
        """Keep context small: system + task, the latest code answer, the latest feedback.

        Mid-size models get worse as earlier failed attempts and tool chatter pile up.
        """
        last_code_idx = max(
            (i for i, m in enumerate(messages) if m["role"] == "assistant" and "```" in (m.get("content") or "")),
            default=None,
        )
        kept = messages[:2]
        if last_code_idx is not None:
            kept.append(messages[last_code_idx])
        kept.append(messages[-1])
        messages[:] = kept
