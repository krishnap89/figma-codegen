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
from typing import Any

from .config import Config
from .design_system import DesignSystem
from .progress import Progress
from .prompts import (PLATFORMS, fix_prompt, load_examples, no_code_prompt,
                      system_prompt, user_prompt)
from .simplify import SimplifyResult
from .skills import SkillLoader, SkillPlan
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
    def __init__(self, cfg: Config, ds: DesignSystem, client: Any = None, progress: Progress | None = None):
        self.cfg = cfg
        self.ds = ds
        self.progress = progress or Progress(enabled=False)
        if client is None:
            from openai import OpenAI
            client = OpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key)
        self.client = client

    def run(self, result: SimplifyResult, platform: str, screen_name: str, package: str | None = None,
            skills: SkillPlan | None = None) -> AgentResult:
        """skills: the skill plan for this run (cli.py builds it with skills.plan_skills); None = no skills."""
        tracer = Tracer(self.cfg.trace_dir, screen_name)
        loader = SkillLoader(skills) if skills is not None and self.cfg.skill_tool else None
        tools = Tools(self.ds, platform, self.cfg.repo_root, skill_loader=loader)
        # Skill examples take precedence; global examples fill the remaining few-shot slots.
        n_global = self.cfg.max_examples - (skills.examples_used if skills else 0)
        examples = load_examples(self.cfg.examples_dir, platform, n_global)
        catalog = [(s.name, s.description) for s in loader.catalog()] if loader and self.cfg.use_tools else None
        messages: list[dict] = [
            {"role": "system", "content": system_prompt(platform, catalog)},
            {"role": "user", "content": user_prompt(self.ds, result, platform, screen_name, package, examples,
                                                    skills.blocks() if skills else None)},
        ]
        empty = SkillPlan(platform)
        tracer.log("start", platform=platform, screen=screen_name, model=self.cfg.llm_model,
                   spec=result.spec, examples=len(examples), **(skills or empty).trace())

        out = AgentResult(ok=False, code=None, rounds=0, validation=None,
                          trace_path=str(tracer.path) if tracer.path else None)
        plat = self.cfg.platform(platform)
        check_label = "lint + compile" if plat.sandbox_file and plat.compile_cmd else "lint"
        for rnd in range(1, self.cfg.max_rounds + 1):
            out.rounds = rnd
            tag = f"round {rnd}/{self.cfg.max_rounds}"
            with self.progress.step(f"{tag} · generating") as st:
                content, n_calls = self._complete(messages, tools, tracer)
                out.tool_calls += n_calls
                code = extract_code(content, platform)
                calls = f", {n_calls} tool call{'s' if n_calls != 1 else ''}" if n_calls else ""
                if code:
                    st.done(f"{len(code.splitlines())} lines{calls}")
                else:
                    st.done(f"no code block in answer{calls}; asking again", ok=False)
            if not code:
                tracer.log("no_code", content=content)
                messages.append({"role": "user", "content": no_code_prompt(platform)})
                continue

            out.code = code
            with self.progress.step(f"{tag} · {check_label}") as st:
                patterns = {**(skills.patterns() if skills else {}), **(loader.patterns() if loader else {})}
                v = validate(code, platform, plat, patterns)
                if v.ok:
                    st.done("valid" + (" (compile skipped: no sandbox configured)" if v.compile_skipped else ""))
                else:
                    compiled = "skipped" if v.compile_skipped else ("ok" if v.compile_ok else "failed")
                    st.done(f"{len(v.lint_issues)} lint issue(s), compile {compiled}", ok=False)
            out.validation = v
            tracer.log("validation", round=rnd, lint=v.lint_issues, compile_ok=v.compile_ok,
                       compile_output=v.compile_output, compile_skipped=v.compile_skipped)
            if v.ok:
                out.ok = True
                return out
            messages.append({"role": "user", "content": fix_prompt(v.feedback(), platform)})
            self._compact(messages)
            if loader and loader.loaded:
                # Compaction dropped the load_skill results; the conventions still apply.
                messages[-1]["content"] = ("Team conventions you loaded with load_skill (still apply):\n\n"
                                           + loader.loaded_text() + "\n\n" + messages[-1]["content"])
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
            self.progress.update(f"waiting for {self.cfg.llm_model}"
                                 + (f" (after {calls} tool call{'s' if calls != 1 else ''})" if calls else ""))
            resp = self.client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            messages.append(_msg_to_dict(msg))
            tracer.log("response", content=msg.content,
                       tool_calls=[c.function.name for c in (msg.tool_calls or [])])
            if not msg.tool_calls:
                return msg.content or "", calls
            for call in msg.tool_calls:
                calls += 1
                self.progress.update(f"tool {call.function.name}")
                result = tools.dispatch(call.function.name, call.function.arguments)
                args = call.function.arguments or ""
                self.progress.info(f"    tool {call.function.name}({args if len(args) <= 80 else args[:79] + '…'})",
                                   dim=True)
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
