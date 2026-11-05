"""Index of a Kotlin module: top-level/object vals, classes with primary constructors, enums."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..kotlin_source import KtFile, expr_end, is_hidden, kotlin_files, match_bracket, split_args


@dataclass
class Sym:
    file: KtFile
    text: str
    owner: tuple            # object path the symbol lives in
    where: str
    hidden: bool = False    # private/internal: resolvable, but never a token


@dataclass
class ClassInfo:
    name: str
    file: KtFile
    params: list            # [(name, type, default_text|None, is_property)]
    body: list              # [(name, type, init_text)]


class Module:
    def __init__(self, root: Path):
        self.root = root
        self.files = [KtFile.load(p) for p in kotlin_files(root)]
        self.symbols: dict[str, Sym] = {}
        self.classes: dict[str, ClassInfo] = {}
        self.enums: dict[str, list[str]] = {}
        self._ctor_ranges: dict = {}               # path -> [(open, close)] of primary constructors
        for f in self.files:
            self._index(f)

    def where(self, f: KtFile, offset: int) -> str:
        try:
            rel = f.path.relative_to(self.root)
        except ValueError:
            rel = f.path
        return f"{rel}:{f.line(offset)}"

    def owner_path(self, f: KtFile, offset: int) -> Optional[list]:
        """Object path for a declaration at `offset`; None if inside a fun/lambda/class body."""
        path = []
        scope = f.scope(offset)
        for i, b in enumerate(scope):
            if b.kind == "object":
                if b.name == "Companion" and i > 0 and scope[i - 1].kind == "class":
                    continue
                if not b.name:
                    return None
                path.append(b.name)
            elif b.kind == "class" and i + 1 < len(scope) and scope[i + 1].kind == "object" \
                    and scope[i + 1].name == "Companion":
                path.append(b.name)
            else:
                return None
        return path

    def _index(self, f: KtFile) -> None:
        m = f.masked
        # classes with primary constructors
        for cm in re.finditer(r"\bclass\s+([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*"
                              r"(?:(?:public|private|internal|protected)\s+)?(?:@\w+\s+)*(?:constructor\s*)?\(", m):
            open_ = cm.end() - 1
            close = match_bracket(m, open_)
            params = []
            for piece, off in split_args(f.code[open_ + 1:close], m[open_ + 1:close]):
                pm = re.match(r"\s*(?:@[\w.]+(?:\([^)]*\))?\s*)*((?:\w+\s+)*?)(va[lr]\s+)?([A-Za-z_]\w*)\s*:\s*"
                              r"([^=]+?)\s*(?:=\s*(.*))?\s*$", piece, re.S)
                if not pm:
                    continue
                hidden = is_hidden(pm.group(1) or "")
                params.append((pm.group(3), pm.group(4).strip(), (pm.group(5) or "").strip() or None,
                               bool(pm.group(2)) and not hidden))
            self.classes[cm.group(1)] = ClassInfo(cm.group(1), f, params, [])
            self._ctor_ranges.setdefault(f.path, []).append((open_, close))
        # enums, sealed hierarchies
        for em in re.finditer(r"\benum\s+class\s+([A-Za-z_]\w*)[^{]*\{", m):
            close = match_bracket(m, em.end() - 1)
            body = m[em.end():close].split(";")[0]
            entries = [re.match(r"\s*(?:@\w+\s*)*([A-Za-z_]\w*)", p) for p, _ in split_args(body, body)]
            self.enums[em.group(1)] = [e.group(1) for e in entries if e]
        for sm in re.finditer(r"\bobject\s+([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*)", m):
            self.enums.setdefault(sm.group(2), []).append(sm.group(1))
        # vals
        for vm in re.finditer(r"(?<![\w.])va[lr]\s+([A-Za-z_]\w*)\s*(?::\s*([^=\n{]+?))?\s*(=|\bby\b)", m):
            if any(o < vm.start() < c for o, c in self._ctor_ranges.get(f.path, [])):
                continue  # `class X(val a: Dp = 4.dp)`: a property of X, handled with the class
            name, typ, start = vm.group(1), (vm.group(2) or "").strip(), vm.end()
            end = expr_end(m, start)
            text = f.code[start:end].strip()
            prefix = f.prefix_before(vm.start())
            scope = f.scope(vm.start())
            if scope and scope[-1].kind == "class" and scope[-1].name in self.classes:
                if not is_hidden(prefix):
                    self.classes[scope[-1].name].body.append((name, typ, text))
                continue
            if scope and scope[-1].kind == "object" and scope[-1].name == "Companion" and len(scope) > 1 \
                    and scope[-2].kind == "class":
                self.enums.setdefault(scope[-2].name, []).append(name)   # companion "constants" of a style class
            path = self.owner_path(f, vm.start())
            if path is None or vm.group(3) != "=":
                continue
            q = ".".join(path + [name])
            sym = Sym(f, text, tuple(path), self.where(f, vm.start()), is_hidden(prefix))
            if q not in self.symbols or self.symbols[q].hidden:
                self.symbols[q] = sym

    def lookup(self, ref: str, owner: tuple = ()) -> Optional[Sym]:
        if ref in self.symbols:
            return self.symbols[ref]
        for i in range(len(owner), 0, -1):
            q = ".".join(owner[:i]) + "." + ref
            if q in self.symbols:
                return self.symbols[q]
        hits = [s for q, s in self.symbols.items() if q.endswith("." + ref)]
        if len(hits) > 1:
            hits = [s for s in hits if not s.hidden]
        return hits[0] if len(hits) == 1 else None

    def object_package(self, name: str) -> Optional[str]:
        for f in self.files:
            if re.search(rf"\b(object|class)\s+{re.escape(name)}\b", f.masked) or \
                    re.search(rf"(?<![\w.])va[lr]\s+{re.escape(name)}\b", f.masked):
                return f.package
        return None
