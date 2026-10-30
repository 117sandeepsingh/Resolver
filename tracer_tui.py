#!/usr/bin/env python3
"""Interactive, colourful terminal explorer for a traced call chain (curses, stdlib only).

Left pane: lazily expanded call tree.  Right pane: syntax-highlighted source of the
selected function.  Jump from a call site in the code straight into the callee, and press
`d` for the docstring of any function (library docs are read from the installed sources).

    python3 tracer_tui.py FILE FUNCTION            (or FILE:FUNCTION)
    python3 tracer.py FILE:FUNCTION -i
"""
from __future__ import annotations

import argparse
import builtins
import curses
import io
import keyword
import locale
import os
import subprocess
import sys
import tokenize
from pathlib import Path

import tracer as core
import tracer_docs as tdocs

# ── palette: name -> (xterm-256 index, 8-colour fallback) ───────────────────────
PALETTE = {
    "text": (252, 7), "kw": (204, 5), "str": (150, 2), "num": (215, 3), "comment": (244, 4),
    "call": (117, 6), "defname": (114, 2), "builtin": (81, 6), "self": (175, 5),
    "const": (209, 1), "deco": (221, 3), "op": (246, 7), "gutter": (240, 4),
    "marker": (214, 3), "dim": (244, 4), "accent": (39, 6), "ok": (114, 2), "warn": (214, 3),
    "err": (203, 1), "title": (231, 7), "class": (80, 6), "fn": (114, 2), "dunder": (177, 5),
    "line": (221, 3), "white": (231, 7),
    # backgrounds
    "bg_header": (24, 4), "bg_footer": (236, 0), "bg_sel": (238, 4), "bg_sel_dim": (236, 0),
    "bg_cursor": (237, 0), "bg_key": (31, 6), "bg_popup": (235, 0),
}


class Theme:
    def __init__(self) -> None:
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        self.rich = curses.COLORS >= 256
        self._pairs: dict[tuple[int, int], int] = {}

    def _c(self, name: str | None) -> int:
        if name is None:
            return -1
        return PALETTE[name][0 if self.rich else 1]

    def attr(self, fg: str, bg: str | None = None, bold: bool = False,
             dim: bool = False, ul: bool = False, rev: bool = False) -> int:
        key = (self._c(fg), self._c(bg))
        if key not in self._pairs:
            pid = len(self._pairs) + 1
            if pid >= curses.COLOR_PAIRS:
                return 0
            curses.init_pair(pid, *key)
            self._pairs[key] = pid
        a = curses.color_pair(self._pairs[key])
        for on, flag in ((bold, curses.A_BOLD), (dim, curses.A_DIM),
                         (ul, curses.A_UNDERLINE), (rev, curses.A_REVERSE)):
            if on:
                a |= flag
        return a


# ── syntax highlighting (stdlib tokenize) ───────────────────────────────────────

BUILTINS = set(dir(builtins))
_FSTR = {getattr(tokenize, n) for n in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
         if hasattr(tokenize, n)}


class Highlighter:
    """Per-file token spans: lineno -> [(start, end, style)]."""

    def __init__(self, path: Path):
        self.lines = path.read_text(encoding="utf-8").splitlines()
        self.spans: dict[int, list[tuple[int, int, str]]] = {}
        try:
            self._scan("\n".join(self.lines) + "\n")
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass

    def _add(self, row: int, a: int, b: int, style: str) -> None:
        if b > a:
            self.spans.setdefault(row, []).append((a, b, style))

    def _scan(self, src: str) -> None:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
        prev_kw = None
        deco_row = None
        for i, t in enumerate(toks):
            (sr, sc), (er, ec) = t.start, t.end
            style = None
            if t.type == tokenize.NAME:
                s = t.string
                nxt = toks[i + 1] if i + 1 < len(toks) else None
                if prev_kw in ("def", "class"):
                    style = "defname"
                elif s in ("True", "False", "None"):
                    style = "const"
                elif keyword.iskeyword(s):
                    style = "kw"
                elif s in ("self", "cls"):
                    style = "self"
                elif s in BUILTINS and nxt and nxt.string == "(":
                    style = "builtin"
                elif nxt and nxt.string == "(" and nxt.start == t.end:
                    style = "call"
                elif s.startswith("__") and s.endswith("__"):
                    style = "dunder"
                if deco_row == sr and style in (None, "call"):
                    style = "deco"
                prev_kw = s if s in ("def", "class") else None
            else:
                prev_kw = None
                if t.type == tokenize.STRING or t.type in _FSTR:
                    style = "str"
                elif t.type == tokenize.NUMBER:
                    style = "num"
                elif t.type == tokenize.COMMENT:
                    style = "comment"
                elif t.type == tokenize.OP:
                    if t.string == "@" and t.line.lstrip().startswith("@"):
                        deco_row, style = sr, "deco"
                    else:
                        style = "op"
            if style is None:
                continue
            if sr == er:
                self._add(sr, sc, ec, style)
            else:                                    # multi-line token (docstrings)
                self._add(sr, sc, len(self.lines[sr - 1]), style)
                for r in range(sr + 1, er):
                    self._add(r, 0, len(self.lines[r - 1]), style)
                self._add(er, 0, ec, style)
        for v in self.spans.values():
            v.sort()

    def segments(self, row: int) -> list[tuple[str, str]]:
        text = self.lines[row - 1].replace("\t", " ")
        out, pos = [], 0
        for a, b, st in self.spans.get(row, []):
            if a < pos:
                continue
            if a > pos:
                out.append((text[pos:a], "text"))
            out.append((text[a:b], st))
            pos = b
        if pos < len(text):
            out.append((text[pos:], "text"))
        return out or [("", "text")]


# ── lazy call tree ──────────────────────────────────────────────────────────────

class Node:
    def __init__(self, fn, parent=None, call_line=None, label=None, external=False,
                 recursive=False, override=False, ctx=None, ext_path=None):
        self.ctx = ctx          # exact class of `self` in this call, when known
        self.ext_path = ext_path    # external calls: importable path for docs lookup
        self.fn, self.parent, self.call_line, self.label = fn, parent, call_line, label
        self.external, self.recursive, self.override = external, recursive, override
        self.expanded = False
        self.kids: list[Node] | None = None

    @property
    def name(self) -> str:
        return self.label if self.fn is None else self.fn.qname.split(":", 1)[1]

    @property
    def module(self) -> str:
        return "" if self.fn is None else self.fn.qname.split(":", 1)[0]

    def chain(self) -> list["Node"]:
        out, n = [], self
        while n:
            out.append(n)
            n = n.parent
        return out[::-1]

    def ancestors_q(self) -> set[str]:
        return {n.fn.qname for n in self.chain() if n.fn}


class CallTree:
    def __init__(self, tracer: core.Tracer, root_fn: core.FuncInfo):
        self.tracer = tracer
        self.root = Node(root_fn)
        self.root.expanded = True
        self.show_external = False
        self._calls: dict[str, list] = {}

    def calls_of(self, fn, ctx=None) -> list:
        key = (fn.qname, ctx[1] if ctx else None)
        if key not in self._calls:
            self._calls[key] = self.tracer.calls(fn, ctx)
        return self._calls[key]

    def kids(self, n: Node) -> list[Node]:
        if n.kids is None:
            n.kids = []
            if n.fn is not None and not n.recursive:
                seen = n.ancestors_q()
                for c in self.calls_of(n.fn, n.ctx):
                    if c.target is None:
                        n.kids.append(Node(None, n, c.line, c.label, external=True, ext_path=c.ext_path))
                    else:
                        n.kids.append(Node(c.target, n, c.line, c.label,
                                           recursive=c.target.qname in seen, override=c.override,
                                           ctx=c.ctx))
        return n.kids

    def visible_kids(self, n: Node) -> list[Node]:
        return [k for k in self.kids(n) if self.show_external or not k.external]

    def flatten(self) -> list[Node]:
        rows: list[Node] = []

        def rec(n: Node) -> None:
            rows.append(n)
            if n.expanded:
                for k in self.visible_kids(n):
                    rec(k)
        rec(self.root)
        return rows

    def walk_all(self, limit: int = 20000, max_depth: int = 40):
        """DFS over the complete chain (for search / expand-all), capped."""
        count = 0
        stack = [(self.root, 0)]
        while stack and count < limit:
            n, d = stack.pop()
            count += 1
            yield n
            if d < max_depth:
                for k in reversed(self.visible_kids(n)):
                    stack.append((k, d + 1))


# ── the app ─────────────────────────────────────────────────────────────────────

HELP = [
    ("Tree pane", ""),
    ("↑ ↓   j k", "move"), ("→  l", "open node; on an open node step into its first call"),
    ("←  h", "close node; on a closed node go to its caller"), ("Enter  Tab", "open the code pane"),
    ("Space", "expand / collapse"), ("*   C", "expand everything below / collapse all"),
    ("PgUp PgDn  g G", "page / top / bottom"),
    ("", ""), ("Code pane", ""),
    ("↑ ↓   PgUp PgDn", "move the cursor"), ("Space  Ctrl-D/U", "page down / half pages"),
    ("Enter", "jump into the function called on this line"), ("u  Backspace", "go up to the caller"),
    ("← at left edge", "back to the tree (otherwise scrolls sideways)"), ("→  h l", "scroll sideways"),
    ("Tab  Esc", "back to the tree"),
    ("", ""), ("Everywhere", ""),
    ("d   K", "show docs: library docstring, or this project's docstring"),
    ("/   n N", "search by name; next / previous match"),
    ("[   ]", "history: back / forward through your jumps"),
    ("x", "show / hide calls into libraries"), ("e", "open in $EDITOR at the cursor line"),
    ("mouse", "click select · double-click open/jump · wheel scroll · click breadcrumbs"),
    ("?", "this help"), ("q   Ctrl-C", "quit"),
]
HOME_KEYS = (curses.KEY_HOME, curses.KEY_FIND, ord("g"))
END_KEYS = (curses.KEY_END, curses.KEY_SELECT, ord("G"))
ENTER_KEYS = (10, 13, curses.KEY_ENTER)
BACKSPACE_KEYS = (curses.KEY_BACKSPACE, 127, 8)


class App:
    def __init__(self, scr, tree: CallTree, libdocs: tdocs.LibraryDocs):
        self.scr, self.tree, self.libdocs = scr, tree, libdocs
        self.th = Theme()
        self.hl: dict[Path, Highlighter] = {}
        self.sel = tree.root
        self.tree_top = 0
        self.focus = "tree"
        self.cursor = 0          # code pane: index into the function's lines
        self.code_top = 0
        self.hscroll = 0
        self.msg = "Tab switches pane  ·  d shows docs  ·  / searches  ·  ? for help"
        self.matches: list[Node] = []
        self.match_i = 0
        self.last_query = ""
        self.back: list[tuple[Node, int]] = []
        self.fwd: list[tuple[Node, int]] = []
        self.rects: dict[str, tuple[int, int, int, int]] = {}
        self.crumbs: list[tuple[int, int, Node]] = []

    # ---------- primitives
    def put(self, y: int, x: int, text: str, attr: int = 0, maxw: int | None = None) -> None:
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w or x < 0:
            return
        room = min(w - x, maxw if maxw is not None else w - x)
        if room <= 0:
            return
        try:
            self.scr.addstr(y, x, text[:room], attr)
        except curses.error:
            pass

    def fill(self, y: int, x: int, w: int, attr: int) -> None:
        self.put(y, x, " " * w, attr)

    def highlighter(self, path: Path) -> Highlighter:
        if path not in self.hl:
            self.hl[path] = Highlighter(path)
        return self.hl[path]

    def fn_range(self, fn) -> tuple[int, int]:
        node = fn.node
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        return start, node.end_lineno

    def box(self, y: int, x: int, h: int, w: int, title: str, active: bool, right: str = "") -> None:
        col = self.th.attr("accent", bold=True) if active else self.th.attr("gutter")
        self.put(y, x, "╭" + "─" * (w - 2) + "╮", col)
        for r in range(1, h - 1):
            self.put(y + r, x, "│", col)
            self.put(y + r, x + w - 1, "│", col)
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", col)
        if title:
            self.put(y, x + 2, f" {title} "[: w - 4], self.th.attr("title" if active else "dim", bold=active))
        if right and len(right) + 6 < w:
            self.put(y + h - 1, x + w - len(right) - 4, f" {right} ", self.th.attr("dim"))

    # ---------- drawing
    def draw(self) -> None:
        scr, th = self.scr, self.th
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 8 or w < 40:
            self.put(0, 0, "terminal too small (need 40x8)", th.attr("err"))
            scr.refresh()
            return
        self.draw_header(w)
        top, bottom = 1, h - 2
        lw = min(max(32, int(w * 0.38)), w - 30)
        ph = bottom - top
        self.rects = {"tree": (top, 0, ph, lw), "code": (top, lw, ph, w - lw)}
        self.draw_tree(*self.rects["tree"])
        self.draw_code(*self.rects["code"])
        self.draw_footer(h, w)
        scr.refresh()

    def draw_header(self, w: int) -> None:
        th, bg = self.th, "bg_header"
        self.fill(0, 0, w, th.attr("title", bg))
        self.put(0, 1, " TRACER ", th.attr("white", "bg_key", bold=True))
        chain = self.sel.chain()
        names = [n.name.split(".")[-1] if n.fn is None else n.name for n in chain]
        budget = w - 12
        first = 0
        while len(" ▸ ".join(names[first:])) > budget and len(names) - first > 2:
            first += 1
        x = 11
        self.crumbs = []
        if first:
            self.put(0, x, "… ▸ ", th.attr("dim", bg))
            x += 4
        for i in range(first, len(names)):
            last = i == len(names) - 1
            self.put(0, x, names[i], th.attr("white" if last else "text", bg, bold=last, ul=not last))
            self.crumbs.append((x, x + len(names[i]), chain[i]))
            x += len(names[i])
            if not last:
                self.put(0, x, " ▸ ", th.attr("accent", bg))
                x += 3

    def hints(self) -> list[tuple[str, str]]:
        n = self.sel
        if self.focus == "tree":
            left = [("↑↓", "move"), ("→", "open"), ("←", "close"), ("Enter", "code"), ("d", "docs"),
                    ("/", "search")]
        else:
            left = [("↑↓", "cursor"), ("Enter", "jump in"), ("d", "docs"), ("u", "caller"), ("←", "tree")]
        if self.matches:
            left.append(("n N", "match"))
        if self.back or self.fwd:
            left.append(("[ ]", "history"))
        left += [("x", "externals"), ("e", "editor")]
        return left

    def draw_footer(self, h: int, w: int) -> None:
        th = self.th
        self.fill(h - 2, 0, w, th.attr("text", "bg_footer"))
        warn = self.msg != "" and not self.msg.startswith("Tab switches")
        self.put(h - 2, 1, self.status_text(), th.attr("warn" if warn else "text", "bg_footer"), w - 2)
        self.fill(h - 1, 0, w, th.attr("text", "bg_footer"))
        right = [("?", "help"), ("q", "quit")]
        rw = sum(len(k) + len(d) + 5 for k, d in right)
        x = 1
        for k, d in self.hints():
            need = len(k) + len(d) + 5
            if x + need > w - rw - 1:
                break
            self.put(h - 1, x, f" {k} ", th.attr("white", "bg_key", bold=True))
            self.put(h - 1, x + len(k) + 2, f" {d} ", th.attr("text", "bg_footer"))
            x += need
        x = w - rw
        for k, d in right:
            self.put(h - 1, x, f" {k} ", th.attr("white", "bg_key", bold=True))
            self.put(h - 1, x + len(k) + 2, f" {d} ", th.attr("text", "bg_footer"))
            x += len(k) + len(d) + 5

    def status_text(self) -> str:
        if self.msg:
            return self.msg
        n = self.sel
        if n.fn is None:
            if n.ext_path:
                return f"call into a library: {n.ext_path}   — press d for its documentation"
            return f"call into a library: {n.label}   — receiver type unknown, no docs available"
        if self.focus == "code":
            ln = self.cursor_lineno()
            on_line = self.nodes_on_line(ln, include_external=True)
            parts = []
            inner = [k for k in on_line if not k.external]
            ext = [k for k in on_line if k.external]
            if inner:
                parts.append("calls → " + ", ".join(k.name + (" (override)" if k.override else "") for k in inner)
                             + "   (Enter jumps)")
            if ext:
                parts.append("library: " + ", ".join(k.label for k in ext) + "   (d for docs)")
            return f"line {ln}" + (": " + "  ·  ".join(parts) if parts else "")
        extra = "  ↻ recursive" if n.recursive else ("  ◇ subclass override" if n.override else "")
        return (f"{n.module}  {n.fn.module.path}:{n.fn.node.lineno}  "
                f"({len(self.tree.visible_kids(n))} calls){extra}")

    def draw_tree(self, y: int, x: int, h: int, w: int) -> None:
        th = self.th
        active = self.focus == "tree"
        rows = self.tree.flatten()
        idx = rows.index(self.sel) if self.sel in rows else 0
        self.box(y, x, h, w, "Call chain", active, f"{idx + 1}/{len(rows)}")
        inner = h - 2
        if idx < self.tree_top:
            self.tree_top = idx
        elif idx >= self.tree_top + inner:
            self.tree_top = idx - inner + 1
        self.tree_top = max(0, min(self.tree_top, max(0, len(rows) - inner)))
        for r in range(inner):
            i = self.tree_top + r
            if i >= len(rows):
                break
            n = rows[i]
            is_sel = n is self.sel
            bg = ("bg_sel" if active else "bg_sel_dim") if is_sel else None
            yy = y + 1 + r
            if bg:
                self.fill(yy, x + 1, w - 2, th.attr("text", bg))
            depth = len(n.chain()) - 1
            col = x + 1 + depth * 2
            if n.external:
                glyph, gst = "·", "dim"
            elif n.recursive:
                glyph, gst = "↻", "err"
            elif self.tree.visible_kids(n):
                glyph, gst = ("▾" if n.expanded else "▸"), "accent"
            elif n.override:
                glyph, gst = "◇", "warn"
            else:
                glyph, gst = "•", "gutter"
            self.put(yy, col, glyph, th.attr(gst, bg, bold=True), x + w - 1 - col)
            col += 2
            room = x + w - 1 - col
            if room <= 0:
                continue
            name = n.name
            if n.external:
                self.put(yy, col, name, th.attr("dim", bg, dim=True), room)
                continue
            cls, _, meth = name.rpartition(".")
            if cls:
                self.put(yy, col, cls + ".", th.attr("class", bg), room)
                col += len(cls) + 1
            style = "dunder" if meth.startswith("__") and meth.endswith("__") else "fn"
            self.put(yy, col, meth, th.attr(style, bg, bold=True), x + w - 1 - col)
            col += len(meth)
            if n.call_line is not None:
                self.put(yy, col, f" :{n.call_line}", th.attr("line", bg), x + w - 1 - col)
                col += len(str(n.call_line)) + 2
            if n.ctx and n.fn.cls != n.ctx[1]:
                t = f" (as {n.ctx[1]})"
                self.put(yy, col, t, th.attr("dim", bg), x + w - 1 - col)
                col += len(t)
            if n.override:
                self.put(yy, col, " ◇ override", th.attr("warn", bg), x + w - 1 - col)
        if len(rows) > inner:                           # scrollbar
            bar_h = max(1, inner * inner // len(rows))
            bar_y = (inner - bar_h) * self.tree_top // max(1, len(rows) - inner)
            for r in range(bar_h):
                self.put(y + 1 + bar_y + r, x + w - 1, "┃", th.attr("accent"))

    def cursor_lineno(self) -> int:
        start, end = self.fn_range(self.sel.fn)
        return min(start + self.cursor, end)

    def nodes_on_line(self, lineno: int, include_external: bool = False) -> list[Node]:
        if self.sel.fn is None:
            return []
        return [k for k in self.tree.kids(self.sel)
                if k.call_line == lineno and (include_external or not k.external)]

    def draw_code(self, y: int, x: int, h: int, w: int) -> None:
        th = self.th
        active = self.focus == "code"
        n = self.sel
        if n.fn is None:
            self.box(y, x, h, w, n.label, active)
            self.put(y + 2, x + 3, "A call into a library — there is no project source to show.", th.attr("dim"))
            if n.ext_path:
                self.put(y + 4, x + 3, n.ext_path, th.attr("call", bold=True), w - 6)
                self.put(y + 6, x + 3, "Press ", th.attr("text"))
                self.put(y + 6, x + 9, " d ", th.attr("white", "bg_key", bold=True))
                self.put(y + 6, x + 12, " to read its documentation.", th.attr("text"))
            else:
                self.put(y + 4, x + 3, "The receiver's type isn't known statically, so no documentation lookup is possible.",
                         th.attr("dim"), w - 6)
            return
        fn = n.fn
        start, end = self.fn_range(fn)
        total = end - start + 1
        self.cursor = max(0, min(self.cursor, total - 1))
        self.box(y, x, h, w, f"{n.name}   {fn.module.path.name}:{start}-{end}", active,
                 f"{self.cursor + 1}/{total}")
        hl = self.highlighter(fn.module.path)
        inner = h - 2
        if self.cursor < self.code_top:
            self.code_top = self.cursor
        elif self.cursor >= self.code_top + inner:
            self.code_top = self.cursor - inner + 1
        self.code_top = max(0, min(self.code_top, max(0, total - inner)))
        kids = self.tree.kids(n)
        call_lines = {k.call_line for k in kids if not k.external}
        ext_lines = {k.call_line for k in kids if k.external and k.ext_path}
        nw = len(str(end))
        gutter = nw + 3
        for r in range(inner):
            i = self.code_top + r
            if i >= total:
                break
            ln = start + i
            yy = y + 1 + r
            is_cur = i == self.cursor
            bg = ("bg_cursor" if active else "bg_sel_dim") if is_cur else None
            if bg:
                self.fill(yy, x + 1, w - 2, th.attr("text", bg))
            if ln in call_lines:
                self.put(yy, x + 1, "▶", th.attr("marker", bg, bold=True))
            elif ln in ext_lines and self.tree.show_external:
                self.put(yy, x + 1, "▷", th.attr("dim", bg))
            self.put(yy, x + 2, f"{ln:>{nw}}", th.attr("line" if is_cur else "gutter", bg, bold=is_cur))
            self.put(yy, x + 2 + nw, " │", th.attr("gutter", bg))
            cx = x + 1 + gutter + 1
            room = x + w - 1 - cx
            skip = self.hscroll
            for text, st in hl.segments(ln):
                if skip >= len(text):
                    skip -= len(text)
                    continue
                piece = text[skip:]
                skip = 0
                if room <= 0:
                    break
                self.put(yy, cx, piece, th.attr(st, bg, bold=st in ("kw", "defname"), dim=st == "comment"), room)
                cx += len(piece)
                room = x + w - 1 - cx
        if total > inner:
            bar_h = max(1, inner * inner // total)
            bar_y = (inner - bar_h) * self.code_top // max(1, total - inner)
            for r in range(bar_h):
                self.put(y + 1 + bar_y + r, x + w - 1, "┃", th.attr("accent"))

    # ---------- navigation actions
    def select(self, node: Node, cursor: int = 0) -> None:
        self.sel = node
        self.cursor, self.code_top, self.hscroll = cursor, 0, 0
        for a in node.chain()[:-1]:
            a.expanded = True

    def jump(self, node: Node, cursor: int = 0) -> None:
        """A deliberate jump: remember where we were so [ and ] can retrace it."""
        if node is not self.sel:
            self.back.append((self.sel, self.cursor))
            self.fwd.clear()
        self.select(node, cursor)

    def history(self, direction: int) -> None:
        src, dst = (self.back, self.fwd) if direction < 0 else (self.fwd, self.back)
        if not src:
            self.msg = "nothing " + ("behind" if direction < 0 else "ahead") + " in the jump history"
            return
        dst.append((self.sel, self.cursor))
        node, cur = src.pop()
        self.select(node, cur)

    def move(self, delta: int) -> None:
        rows = self.tree.flatten()
        i = rows.index(self.sel) if self.sel in rows else 0
        j = max(0, min(len(rows) - 1, i + delta))
        if rows[j] is not self.sel:
            self.select(rows[j])

    def go_up(self) -> None:
        n = self.sel
        if n.parent:
            self.jump(n.parent, cursor=max(0, (n.call_line or 0) - self.fn_range(n.parent.fn)[0]))
        else:
            self.msg = "already at the root of the chain"

    def jump_into(self) -> None:
        if self.sel.fn is None:
            return
        tg = self.nodes_on_line(self.cursor_lineno())
        if not tg:
            ext = self.nodes_on_line(self.cursor_lineno(), include_external=True)
            self.msg = ("this line only calls into libraries — press d for their docs" if ext
                        else "no function is called on this line")
            return
        pick = tg[0] if len(tg) == 1 else self.choose(
            "Jump to which call?", [k.name + ("   ◇ override" if k.override else "") for k in tg], tg)
        if pick is not None:
            self.sel.expanded = True
            self.jump(pick)

    def toggle_external(self) -> None:
        self.tree.show_external = not self.tree.show_external
        if not self.tree.show_external and self.sel.external:
            self.select(self.sel.parent)
        self.msg = "calls into libraries " + ("shown" if self.tree.show_external else "hidden")

    # ---------- popups & prompts
    def choose(self, title: str, labels: list[str], items: list):
        i = 0
        while True:
            self.draw()
            h, w = self.scr.getmaxyx()
            bw = min(w - 4, max(len(title), *(len(l) for l in labels)) + 8)
            bh = len(labels) + 2
            by, bx = (h - bh) // 2, (w - bw) // 2
            for r in range(bh):
                self.fill(by + r, bx, bw, self.th.attr("text", "bg_popup"))
            self.box(by, bx, bh, bw, title, True)
            for k, l in enumerate(labels):
                a = self.th.attr("white", "bg_sel", bold=True) if k == i else self.th.attr("text", "bg_popup")
                self.fill(by + 1 + k, bx + 1, bw - 2, a)
                self.put(by + 1 + k, bx + 2, f"{k + 1} " if k < 9 else "  ", self.th.attr("marker", None, bold=True)
                         if k != i else a)
                self.put(by + 1 + k, bx + 4, l, a, bw - 6)
            self.scr.refresh()
            ch = self.scr.getch()
            if ch in (curses.KEY_UP, ord("k")):
                i = (i - 1) % len(labels)
            elif ch in (curses.KEY_DOWN, ord("j"), 9):
                i = (i + 1) % len(labels)
            elif ord("1") <= ch <= ord("9") and ch - ord("1") < len(labels):
                return items[ch - ord("1")]
            elif ch in ENTER_KEYS:
                return items[i]
            elif ch in (27, ord("q")):
                return None

    def prompt(self, label: str) -> str | None:
        buf = ""
        curses.curs_set(1)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                a = self.th.attr("white", "bg_key", bold=True)
                self.fill(h - 2, 0, w, a)
                self.put(h - 2, 1, f"{label}{buf}", a, w - 2)
                hint = "Enter go · Esc cancel · Ctrl-U clear"
                self.put(h - 2, max(len(label) + len(buf) + 4, w - len(hint) - 2), hint, self.th.attr("text", "bg_key"))
                self.scr.move(h - 2, min(w - 1, 1 + len(label) + len(buf)))
                self.scr.refresh()
                ch = self.scr.getch()
                if ch in ENTER_KEYS:
                    return buf
                if ch == 27:
                    return None
                if ch in BACKSPACE_KEYS:
                    buf = buf[:-1]
                elif ch == 21:
                    buf = ""
                elif 32 <= ch < 127:
                    buf += chr(ch)
        finally:
            curses.curs_set(0)

    def search(self) -> None:
        q = self.prompt("/ ")
        if not q:
            return
        self.last_query = q.lower()
        self.matches = [n for n in self.tree.walk_all() if self.last_query in n.name.lower()]
        if not self.matches:
            self.msg = f"no function matching '{q}'"
            return
        after = [i for i, n in enumerate(self.matches) if n is self.sel]
        self.match_i = (after[0] + 1) % len(self.matches) if after else 0
        self.goto_match()

    def goto_match(self) -> None:
        self.jump(self.matches[self.match_i])
        self.msg = f"match {self.match_i + 1}/{len(self.matches)} for '{self.last_query}'  (n / N for next / previous)"

    def next_match(self, step: int) -> None:
        if not self.matches:
            self.msg = "no active search — press /"
            return
        self.match_i = (self.match_i + step) % len(self.matches)
        self.goto_match()

    def expand_all(self) -> None:
        stack, count = [self.sel], 0
        while stack and count < 3000:
            n = stack.pop()
            count += 1
            if self.tree.visible_kids(n):
                n.expanded = True
                stack.extend(self.tree.visible_kids(n))

    def popup_frame(self, title: str, right: str = "") -> tuple[int, int, int, int]:
        h, w = self.scr.getmaxyx()
        bw, bh = min(w - 4, 100), h - 6
        by, bx = 2, (w - bw) // 2
        for r in range(bh):
            self.fill(by + r, bx, bw, self.th.attr("text", "bg_popup"))
        self.box(by, bx, bh, bw, title, True, right)
        return by, bx, bh, bw

    def help(self) -> None:
        top = 0
        while True:
            self.draw()
            by, bx, bh, bw = self.popup_frame("Help — ↑↓ scroll, any other key closes")
            inner = bh - 2
            top = max(0, min(top, max(0, len(HELP) - inner)))
            for k, (a, b) in enumerate(HELP[top: top + inner]):
                if not b:
                    self.put(by + 1 + k, bx + 2, a, self.th.attr("accent", "bg_popup", bold=True))
                else:
                    self.put(by + 1 + k, bx + 3, a, self.th.attr("marker", "bg_popup", bold=True), 20)
                    self.put(by + 1 + k, bx + 24, b, self.th.attr("text", "bg_popup"), bw - 26)
            self.scr.refresh()
            ch = self.scr.getch()
            if ch in (curses.KEY_DOWN, ord("j")):
                top += 1
            elif ch in (curses.KEY_UP, ord("k")):
                top -= 1
            elif ch in (curses.KEY_RESIZE, curses.KEY_MOUSE):
                continue
            else:
                return

    # ---------- documentation
    def show_docs(self, node: Node) -> None:
        if node.fn is not None:
            doc = tdocs.doc_for_project(node.fn)
        elif node.ext_path:
            self.msg = f"looking up {node.ext_path} …"
            self.draw()
            doc = self.libdocs.lookup(node.ext_path)
            self.msg = ""
        else:
            self.msg = "no documentation target: the receiver's type isn't known statically"
            return
        self.pager(doc)

    def docs_key(self) -> None:
        if self.focus == "code" and self.sel.fn is not None:
            tg = self.nodes_on_line(self.cursor_lineno(), include_external=True)
            tg = [k for k in tg if not k.external or k.ext_path]
            if len(tg) > 1:
                pick = self.choose("Docs for which call?",
                                   [k.name if not k.external else f"{k.ext_path}   (library)" for k in tg], tg)
                if pick is not None:
                    self.show_docs(pick)
                return
            if tg:
                self.show_docs(tg[0])
                return
            # no call on this line: document the function being read
        self.show_docs(self.sel)

    def pager(self, doc: tdocs.Doc) -> None:
        top = 0
        while True:
            self.draw()
            by, bx, bh, bw = self.popup_frame(f"Docs · {doc.title}"[: 90])
            lines = tdocs.format_doc(doc, bw - 6)
            inner = bh - 3
            top = max(0, min(top, max(0, len(lines) - inner)))
            for r, segs in enumerate(lines[top: top + inner]):
                x = bx + 3
                for text, st in segs:
                    self.put(by + 1 + r, x, text, self.th.attr(st, "bg_popup", bold=st in ("fn", "kw")), bx + bw - 3 - x)
                    x += len(text)
            hint = f" ↑↓ PgUp PgDn scroll · g G ends · q Esc close   {top + 1}-{min(len(lines), top + inner)}/{len(lines)} "
            self.put(by + bh - 2, bx + 2, hint, self.th.attr("dim", "bg_popup"), bw - 4)
            if len(lines) > inner:
                bar_h = max(1, inner * inner // len(lines))
                bar_y = (inner - bar_h) * top // max(1, len(lines) - inner)
                for r in range(bar_h):
                    self.put(by + 1 + bar_y + r, bx + bw - 1, "┃", self.th.attr("accent", "bg_popup"))
            self.scr.refresh()
            ch = self.scr.getch()
            page = max(1, inner - 1)
            if ch in (curses.KEY_DOWN, ord("j")):
                top += 1
            elif ch in (curses.KEY_UP, ord("k")):
                top -= 1
            elif ch in (curses.KEY_NPAGE, ord(" "), 6):
                top += page
            elif ch in (curses.KEY_PPAGE, ord("b"), 2):
                top -= page
            elif ch == 4:
                top += page // 2
            elif ch == 21:
                top -= page // 2
            elif ch in HOME_KEYS:
                top = 0
            elif ch in END_KEYS:
                top = 10**6
            elif ch == curses.KEY_MOUSE:
                try:
                    bstate = curses.getmouse()[4]
                except curses.error:
                    continue
                if bstate & getattr(curses, "BUTTON4_PRESSED", 0):
                    top -= 3
                elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
                    top += 3
            elif ch == curses.KEY_RESIZE:
                continue
            elif ch in (27, ord("q"), ord("d"), ord("K")) or ch in ENTER_KEYS:
                return

    def open_editor(self) -> None:
        if self.sel.fn is None:
            self.msg = "nothing to open: this is a call into a library"
            return
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        line = self.cursor_lineno() if self.focus == "code" else self.sel.fn.node.lineno
        curses.def_prog_mode()
        curses.endwin()
        try:
            subprocess.run([*editor.split(), f"+{line}", str(self.sel.fn.module.path)])
        except OSError as e:
            self.msg = f"cannot launch editor: {e}"
        curses.reset_prog_mode()
        self.scr.refresh()

    # ---------- input
    def handle_mouse(self) -> None:
        try:
            _, mx, my, _, b = curses.getmouse()
        except curses.error:
            return
        up = b & getattr(curses, "BUTTON4_PRESSED", 0)
        down = b & getattr(curses, "BUTTON5_PRESSED", 0)
        press = b & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED)
        double = b & curses.BUTTON1_DOUBLE_CLICKED
        if my == 0 and (press or double):                      # breadcrumb: jump to that ancestor
            for x0, x1, node in self.crumbs:
                if x0 <= mx < x1:
                    self.jump(node)
            return
        for name, (ry, rx, rh, rw) in self.rects.items():
            if not (ry <= my < ry + rh and rx <= mx < rx + rw):
                continue
            inside = ry + 1 <= my <= ry + rh - 2 and rx + 1 <= mx <= rx + rw - 2   # not on the border
            if name == "tree":
                if up or down:
                    self.move(-3 if up else 3)
                elif (press or double) and inside:
                    rows = self.tree.flatten()
                    i = self.tree_top + my - ry - 1
                    if 0 <= i < len(rows):
                        self.focus = "tree"
                        node = rows[i]
                        glyph_x = rx + 1 + (len(node.chain()) - 1) * 2
                        if node is not self.sel:
                            self.select(node)
                        if double:
                            if node.fn is not None:
                                self.focus = "code"
                            else:
                                self.show_docs(node)
                        elif glyph_x <= mx <= glyph_x + 1 and self.tree.visible_kids(node):
                            node.expanded = not node.expanded
            elif self.sel.fn is not None:
                if up or down:
                    self.cursor += -3 if up else 3
                elif (press or double) and inside:
                    self.focus = "code"
                    self.cursor = self.code_top + my - ry - 1
                    if double:
                        self.jump_into()
            return

    def key_common(self, ch: int) -> bool:
        """Keys that mean the same in both panes."""
        if ch in (ord("d"), ord("K")):
            self.docs_key()
        elif ch == ord("/"):
            self.search()
        elif ch == ord("n"):
            self.next_match(1)
        elif ch == ord("N"):
            self.next_match(-1)
        elif ch == ord("["):
            self.history(-1)
        elif ch == ord("]"):
            self.history(1)
        elif ch == ord("x"):
            self.toggle_external()
        elif ch == ord("e"):
            self.open_editor()
        else:
            return False
        return True

    def key_tree(self, ch: int) -> None:
        n = self.sel
        kids = self.tree.visible_kids(n)
        page = max(1, self.rects["tree"][2] - 3) if self.rects else 10
        if ch in (curses.KEY_UP, ord("k")):
            self.move(-1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.move(1)
        elif ch == curses.KEY_PPAGE:
            self.move(-page)
        elif ch == curses.KEY_NPAGE:
            self.move(page)
        elif ch in HOME_KEYS:
            self.move(-10**6)
        elif ch in END_KEYS:
            self.move(10**6)
        elif ch in (curses.KEY_RIGHT, ord("l")):
            if kids and not n.expanded:
                n.expanded = True
            elif kids:
                self.select(kids[0])
            elif n.fn is not None:
                self.focus = "code"
        elif ch in (curses.KEY_LEFT, ord("h")):
            if n.expanded and kids:
                n.expanded = False
            elif n.parent is not None:
                self.select(n.parent)
        elif ch in ENTER_KEYS or ch == 9:
            if n.fn is not None:
                self.focus = "code"
            else:
                self.show_docs(n)
        elif ch == ord(" "):
            if kids:
                n.expanded = not n.expanded
        elif ch == ord("*"):
            self.expand_all()
        elif ch == ord("C"):
            for k in self.tree.walk_all():
                k.expanded = False
            self.tree.root.expanded = True
            self.select(self.tree.root)
        elif ch == 27:
            self.matches, self.msg = [], ""
        else:
            self.key_common(ch)

    def key_code(self, ch: int) -> None:
        page = max(1, self.rects["code"][2] - 3) if self.rects else 10
        if ch in (curses.KEY_UP, ord("k")):
            self.cursor -= 1
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.cursor += 1
        elif ch == curses.KEY_PPAGE:
            self.cursor -= page
        elif ch in (curses.KEY_NPAGE, ord(" ")):
            self.cursor += page
        elif ch == 4:
            self.cursor += page // 2
        elif ch == 21:
            self.cursor -= page // 2
        elif ch in HOME_KEYS:
            self.cursor = 0
        elif ch in END_KEYS:
            self.cursor = 10**6
        elif ch in (curses.KEY_RIGHT, ord("l")):
            self.hscroll += 8
        elif ch in (curses.KEY_LEFT, ord("h")):
            if self.hscroll:
                self.hscroll = max(0, self.hscroll - 8)
            else:
                self.focus = "tree"
        elif ch in ENTER_KEYS:
            self.jump_into()
        elif ch in (ord("u"), *BACKSPACE_KEYS):
            self.go_up()
        elif ch in (9, 27):
            self.focus = "tree"
        else:
            self.key_common(ch)

    def run(self) -> None:
        curses.curs_set(0)
        self.scr.keypad(True)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass
        while True:
            self.draw()
            ch = self.scr.getch()
            if ch != curses.KEY_RESIZE:
                self.msg = ""
            if ch in (ord("q"), ord("Q")):
                return
            if ch == curses.KEY_RESIZE:
                continue
            if ch == curses.KEY_MOUSE:
                self.handle_mouse()
            elif ch == ord("?"):
                self.help()
            elif self.focus == "tree":
                self.key_tree(ch)
            else:
                self.key_code(ch)


def run(tracer: core.Tracer, fn: core.FuncInfo, external: bool = False) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("error: interactive mode needs a terminal", file=sys.stderr)
        return 2
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    tree = CallTree(tracer, fn)
    tree.show_external = external
    libdocs = tdocs.LibraryDocs(tracer.p.root)

    def _main(scr) -> None:
        App(scr, tree, libdocs).run()
    try:
        curses.wrapper(_main)
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("function", nargs="?")
    ap.add_argument("--root", type=Path)
    ap.add_argument("--external", action="store_true", help="start with calls into libraries visible")
    a = ap.parse_args()
    if a.function is None:
        f, sep, name = str(a.file).rpartition(":")
        if not sep or not f or not name:
            ap.error("give FILE FUNCTION or FILE:FUNCTION")
        a.file, a.function = Path(f), name
    if not a.file.is_file():
        print(f"error: no such file: {a.file}", file=sys.stderr)
        return 2
    proj = core.Project(a.root or core.project_root(a.file))
    mod = proj.load_path(a.file.resolve())
    if mod is None:
        print(f"error: could not parse {a.file}", file=sys.stderr)
        return 2
    fn = core.find_function(mod, a.function)
    if fn is None:
        print(f"error: {a.function!r} not found (or ambiguous) in {a.file}", file=sys.stderr)
        return 1
    return run(core.Tracer(proj, None, True), fn, a.external)


if __name__ == "__main__":
    sys.exit(main())
