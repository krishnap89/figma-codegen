"""Just enough Kotlin source analysis to read a Compose UIKit: comments, strings, blocks,
declarations and balanced expressions. Regex + bracket matching, no real parser.

Two views of every file share the same offsets:
  code    comments blanked (strings kept) -> text of expressions and default values
  masked  comments and string contents blanked -> structure (brackets, keywords)
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP_DIRS = {"build", "test", "androidTest", "testDebug", "testRelease", "generated", ".gradle", ".idea"}


def kotlin_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*.kt") if not (set(p.relative_to(root).parts[:-1]) & SKIP_DIRS))


def _blank(s: str) -> str:
    return "".join("\n" if c == "\n" else " " for c in s)


def strip(src: str) -> tuple[str, str, list[tuple[int, int, str]]]:
    """Returns (code, masked, kdocs[(start, end, text)])."""
    code, masked, kdocs = [], [], []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            j = n if j < 0 else j
            code.append(_blank(src[i:j])); masked.append(_blank(src[i:j])); i = j
        elif two == "/*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            if src.startswith("/**", i):
                kdocs.append((i, j, src[i:j]))
            code.append(_blank(src[i:j])); masked.append(_blank(src[i:j])); i = j
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j + 3
            while j < n and src[j] == '"':
                j += 1
            code.append(src[i:j]); masked.append('"' + _blank(src[i + 1:j - 1]) + '"'); i = j
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
            code.append(src[i:j]); masked.append(c + _blank(src[i + 1:j - 1]) + (src[j - 1] if j - 1 > i else ""))
            i = j
        else:
            code.append(c); masked.append(c); i += 1
    return "".join(code), "".join(masked), kdocs


OPEN, CLOSE = "([{", ")]}"


def match_bracket(masked: str, i: int) -> int:
    """Index of the bracket closing the one at `i` (or len(masked) - 1)."""
    depth = 0
    for j in range(i, len(masked)):
        ch = masked[j]
        if ch in OPEN:
            depth += 1
        elif ch in CLOSE:
            depth -= 1
            if depth == 0:
                return j
    return len(masked) - 1


_CONT_START = re.compile(r"\s*(\.|\?\.|\?:|&&|\|\||[-+*/]\s|\)|\])")


def expr_end(masked: str, i: int) -> int:
    """End of the expression starting at `i`: stops at a depth-0 `,` `)` `}` `;` or at a
    newline that does not continue the expression (next line starting with `.`, `?:` ...)."""
    depth, j, n = 0, i, len(masked)
    last = ""
    while j < n:
        ch = masked[j]
        if ch in OPEN:
            depth += 1
        elif ch in CLOSE:
            if depth == 0:
                return j
            depth -= 1
        elif depth == 0 and ch in ",;":
            return j
        elif depth == 0 and ch == "\n" and last and last not in "=(,.+-*/:&|{" and j > i:
            if not _CONT_START.match(masked, j + 1):
                return j
        if not ch.isspace():
            last = ch
        j += 1
    return n


def split_args(text: str, masked: str) -> list[tuple[str, int]]:
    """Split a bracket's inner text at depth-0 commas -> [(piece, offset)]."""
    out, depth, start = [], 0, 0
    for j, ch in enumerate(masked):
        if ch in OPEN:
            depth += 1
        elif ch in CLOSE:
            depth -= 1
        elif ch == "," and depth == 0:
            out.append((text[start:j], start)); start = j + 1
    out.append((text[start:], start))
    return [(p, o) for p, o in out if p.strip()]


@dataclass
class Block:
    start: int          # index of "{"
    end: int            # index of matching "}"
    kind: str           # "object" | "class" | "other"
    name: str = ""


_HEAD = re.compile(r"\b(?:(companion)\s+)?object\b(?:\s+([A-Za-z_]\w*))?|\b(?:class|interface)\s+([A-Za-z_]\w*)|\bfun\b|=")


@dataclass
class KtFile:
    path: Path
    src: str
    code: str
    masked: str
    kdocs: list
    package: str
    blocks: list = field(default_factory=list)
    _lines: list = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "KtFile":
        src = path.read_text(encoding="utf-8", errors="replace")
        code, masked, kdocs = strip(src)
        m = re.search(r"^\s*package\s+([\w.]+)", masked, re.M)
        f = cls(path, src, code, masked, kdocs, m.group(1) if m else "")
        f._lines = [i for i, ch in enumerate(src) if ch == "\n"]
        f._index_blocks()
        return f

    def line(self, offset: int) -> int:
        return bisect.bisect_left(self._lines, offset) + 1

    def _index_blocks(self) -> None:
        stack = []
        for i, ch in enumerate(self.masked):
            if ch == "{":
                stack.append(i)
            elif ch == "}" and stack:
                s = stack.pop()
                self.blocks.append(self._classify(s, i))
        self.blocks.sort(key=lambda b: b.start)

    def _classify(self, start: int, end: int) -> Block:
        k = start - 1
        while k >= 0 and self.masked[k] not in "{};":
            k -= 1
        head = self.masked[k + 1:start]
        prev = None
        while prev != head:  # drop (...) groups: `class A(val x: Int = 1) {` is a class, not `=`
            prev, head = head, re.sub(r"\([^()]*\)", "()", head)
        last = None
        for m in _HEAD.finditer(head):
            last = m
        if last is None or last.group(0) in ("fun", "="):
            return Block(start, end, "other")
        if last.group(3):
            return Block(start, end, "class", last.group(3))
        rest = head[last.end():].strip()
        if rest.startswith(":") and not last.group(2):          # `object : Foo {` (anonymous)
            return Block(start, end, "other")
        return Block(start, end, "object", last.group(2) or ("Companion" if last.group(1) else ""))

    def scope(self, offset: int) -> list[Block]:
        """Enclosing blocks, outermost first."""
        return [b for b in self.blocks if b.start < offset < b.end]

    def kdoc_before(self, offset: int) -> str:
        """KDoc that ends right before `offset` with only annotations / whitespace in between."""
        for s, e, text in reversed(self.kdocs):
            if e <= offset:
                between = self.masked[e:offset]
                if re.fullmatch(r"(\s|@[\w.]+(\([^)]*\))?|\b(?:public|internal|private|inline)\b)*", between):
                    return text
                return ""
        return ""

    def prefix_before(self, offset: int) -> str:
        """Annotations and modifiers written before the declaration at `offset`."""
        line_start = self.masked.rfind("\n", 0, offset) + 1
        parts = [self.masked[line_start:offset]]
        pos = line_start - 1
        balance = 0
        pending: list[str] = []   # lines inside a multi-line annotation argument list, e.g. @Preview(\n ... \n)
        for _ in range(60):       # annotations never span more; also keeps this O(1) per declaration
            if pos <= 0:
                break
            ls = self.masked.rfind("\n", 0, pos) + 1
            line = self.masked[ls:pos].strip()
            pos = ls - 1
            balance += line.count(")") - line.count("(")
            if balance > 0:
                pending.append(line)
                continue
            if pending:            # the group is an annotation only if it opens with `@Name(`
                if not line.startswith("@"):
                    break
                parts.extend(pending)
                pending = []
                parts.append(line)
                continue
            if not line:
                break
            if line.startswith("@") or all(w in MODIFIERS for w in line.split()):
                parts.append(line)
                continue
            break
        return " ".join(reversed(parts))


MODIFIERS = {"public", "private", "internal", "protected", "inline", "override", "open", "final", "abstract",
             "const", "suspend", "actual", "expect", "lateinit", "data", "sealed", "operator", "infix"}


def is_hidden(prefix: str) -> bool:
    return bool(re.search(r"\b(private|internal|protected)\b", prefix))


def collapse(text: str) -> str:
    """One line, single spaces, no space inside brackets, no trailing comma."""
    t = re.sub(r"\s+", " ", text).strip()
    t = re.sub(r"([(\[]) ", r"\1", t)
    t = re.sub(r",?\s*([)\]])", r"\1", t)
    return t
