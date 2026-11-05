"""Progress feedback on stderr: which step is running, how long it has taken, total time.

On a terminal the current step is a live spinner line that redraws in place:

    ⠼ round 2/4 · generating — waiting for qwen2.5-coder  41s · total 1m 12s

When stderr is not a terminal (CI, `2> log`), a step that runs longer than 2s prints a start
line, then a "still running" heartbeat every 30s so long model calls don't look hung.
Finished steps always leave one line behind:

    ✓ round 2/4 · generating — 182 lines, 2 tool calls  (41s)
"""
from __future__ import annotations

import shutil
import sys
import threading
import time
from contextlib import contextmanager
from typing import IO, Iterator

from .style import Style

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ANNOUNCE_AFTER_S = 2.0   # non-live: print a start line only for steps that run longer than this
HEARTBEAT_S = 30.0


def fmt_duration(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


class Step:
    def __init__(self, label: str):
        self.label = label
        self.detail = ""          # live-only sub-status, e.g. "tool get_component_docs"
        self.status: str | None = None
        self.ok = True
        self.announced = False    # non-live: start line printed (only for steps that take a while)
        self.start = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def done(self, status: str | None = None, ok: bool = True) -> None:
        """Set the text shown on the finish line and whether it gets ✓ or ✗."""
        self.status = status
        self.ok = ok


class Progress:
    def __init__(self, stream: IO[str] | None = None, enabled: bool = True, live: bool | None = None,
                 color: bool | None = None):
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = enabled
        self.style = Style.for_stream(self.stream) if color is None else Style(color)
        if live is None:
            try:
                live = self.stream.isatty()
            except (AttributeError, ValueError):
                live = False
        self.live = enabled and live
        self.started = time.monotonic()
        self._lock = threading.RLock()
        self._step: Step | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame = 0
        self._last_beat = 0.0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    # ---------- public ----------
    def info(self, msg: str, dim: bool = False) -> None:
        """A permanent line; on a terminal it is printed above the spinner."""
        if not self.enabled:
            return
        with self._lock:
            self._clear()
            self._write((self.style.dim(msg) if dim else msg) + "\n")
            self._draw()

    def update(self, detail: str) -> None:
        """Change the sub-status of the running step (live line only)."""
        with self._lock:
            if self._step:
                self._step.detail = detail
                self._draw()

    @contextmanager
    def step(self, label: str) -> Iterator[Step]:
        step = Step(label)
        if not self.enabled:
            yield step
            return
        with self._lock:
            self._step = step
            self._last_beat = step.start
            if self.live:
                self._draw()
        self._start_ticker()
        try:
            yield step
        except BaseException as e:
            if step.ok:
                step.done("interrupted" if isinstance(e, KeyboardInterrupt) else f"failed: {type(e).__name__}",
                          ok=False)
            raise
        finally:
            self._stop_ticker()
            with self._lock:
                self._clear()
                self._step = None
                st = self.style
                tail = ""
                if step.status:
                    tail = " — " + (step.status if step.ok else st.err(step.status))
                took = st.dim(f"({fmt_duration(step.elapsed)})")
                self._write(f"{st.mark(step.ok)} {step.label}{tail}  {took}\n")

    # ---------- internals ----------
    def _write(self, text: str) -> None:
        try:
            sys.stdout.flush()   # keep ordering with normal output when both go to one file
        except (AttributeError, ValueError):
            pass
        self.stream.write(text)
        self.stream.flush()

    def _clear(self) -> None:
        if self.live and self._step is not None:
            self.stream.write("\r\x1b[2K")

    def _draw(self) -> None:
        if not self.live or self._step is None:
            return
        s = self._step
        segments = [  # (text, styles)
            (SPINNER[self._frame % len(SPINNER)] + " ", ("cyan",)),
            (s.label, ()),
            (f" — {s.detail}" if s.detail else "", ("dim",)),
            (f"  {fmt_duration(s.elapsed)}", ("cyan",)),
            (f" · total {fmt_duration(self.elapsed)}", ("dim",)),
        ]
        # A wrapped line can't be redrawn with \r, so never exceed the terminal width.
        # Measure plain text; color codes take no columns.
        room = shutil.get_terminal_size((100, 20)).columns - 1
        line = ""
        for text, styles in segments:
            if len(text) > room:
                text = text[: max(room - 1, 0)] + "…" if room > 0 else ""
            room -= len(text)
            line += self.style(text, *styles)
            if room <= 0:
                break
        self.stream.write("\r\x1b[2K" + line)
        self.stream.flush()

    def _tick(self) -> None:
        interval = 0.1 if self.live else 1.0
        while not self._stop.wait(interval):
            with self._lock:
                s = self._step
                if s is None:
                    continue
                if self.live:
                    self._frame += 1
                    self._draw()
                    continue
                detail = self.style.dim(f" — {s.detail}") if s.detail else ""
                if not s.announced and s.elapsed >= ANNOUNCE_AFTER_S:
                    s.announced = True
                    self._last_beat = time.monotonic()
                    self._write(f"{self.style('…', 'cyan')} {s.label}{detail}\n")
                elif s.announced and time.monotonic() - self._last_beat >= HEARTBEAT_S:
                    self._last_beat = time.monotonic()
                    took = self.style(f"({fmt_duration(s.elapsed)})", "cyan")
                    self._write(f"  {self.style('… still running:', 'dim')} {s.label}{detail} {took}\n")

    def _start_ticker(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._tick, name="progress", daemon=True)
        self._thread.start()

    def _stop_ticker(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None
