#!/usr/bin/env python3
"""tracer: print the chain of functions called from a given Python function.

usage: tracer.py FILE FUNCTION | FILE:FUNCTION [-i] [--root DIR] [--depth N] [--external]
                 [--all-calls] [--format tree|json|mermaid|dot]

FUNCTION is `name` or `Class.method`. Calls are resolved statically (ast) to
functions/methods defined inside the project root; anything else (stdlib,
third-party, dynamic) is hidden unless --external is given.
"""
from __future__ import annotations

import argparse
import ast
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


import builtins
BUILTIN_NAMES = set(dir(builtins))

# never resolve imports into (or scan) installed packages / virtualenvs
SKIP_DIRS = {"site-packages", "dist-packages", ".venv", "venv", ".tox", ".git", "__pycache__",
             "node_modules", ".eggs"}


if __name__ == "__main__":
    sys.modules.setdefault("tracer", sys.modules[__name__])


@dataclass
class Call:
    line: int
    label: str
    target: "FuncInfo | None"
    override: bool = False      # a subclass override of the statically resolved method
    ctx: "tuple | None" = None  # (Module, class name): exact class of `self` inside the callee, if known
    ext_path: str | None = None  # for unresolved calls: best-guess importable path, e.g. "polars.scan_parquet"


@dataclass
class FuncInfo:
    qname: str                  # module:Class.method
    module: "Module"
    node: ast.AST
    cls: str | None = None

    @property
    def lib(self) -> str | None:
        """Origin label ("site-packages", "stdlib", ...) if this lives outside the project."""
        return self.module.lib


@dataclass
class Module:
    name: str
    path: Path
    tree: ast.Module
    funcs: dict[str, FuncInfo] = field(default_factory=dict)       # top-level functions
    classes: dict[str, dict[str, FuncInfo]] = field(default_factory=dict)
    bases: dict[str, list[ast.expr]] = field(default_factory=dict)
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)  # alias -> (module, symbol|None)
    # class -> self.attr -> (type expr, exact): exact=True when it came from `self.x = Cls()`
    attr_types: dict[str, dict[str, tuple[ast.expr, bool]]] = field(default_factory=dict)
    star: list[str] = field(default_factory=list)             # modules pulled in by `from x import *`
    lib: str | None = None                                    # origin label for library modules


class Project:
    def __init__(self, root: Path, lib: str | None = None):
        self.root = root.resolve()
        self.lib = lib                  # set for projects that wrap an installed-package directory
        self.libs: list["Project"] = []     # library projects consulted when a module isn't ours
        self.modules: dict[str, Module] = {}
        self._all_loaded = False
        self._subs: dict[tuple[str, str], list[tuple[Module, str]]] | None = None

    def is_third_party(self, path: Path) -> bool:
        try:
            parts = path.resolve().relative_to(self.root).parts
        except ValueError:
            return True
        return any(p in SKIP_DIRS for p in parts)

    def all_modules(self, limit: int = 5000) -> list[Module]:
        """Every project .py file (third-party dirs skipped), loaded once."""
        if not self._all_loaded:
            self._all_loaded = True
            n = 0
            for dp, dns, fns in os.walk(self.root):
                dns[:] = [d for d in dns if d not in SKIP_DIRS]
                for f in fns:
                    if f.endswith(".py") and n < limit:
                        n += 1
                        self.load_path(Path(dp, f))
        return list(self.modules.values())

    def subclass_map(self) -> dict[tuple[str, str], list[tuple[Module, str]]]:
        if self._subs is None:
            self._subs = {}
            for m in self.all_modules():
                for cname, bases in m.bases.items():
                    for b in bases:
                        bn = dotted(b)
                        r = self.class_of(m, bn) if bn else None
                        if r:
                            self._subs.setdefault((r[0].name, r[1]), []).append((m, cname))
        return self._subs

    def overriders(self, mod: Module, cls: str, meth: str) -> list[FuncInfo]:
        """Methods named `meth` defined by (transitive) subclasses of mod.cls."""
        subs, out, seen, stack = self.subclass_map(), [], set(), [(mod.name, cls)]
        while stack:
            for m, c in subs.get(stack.pop(), []):
                if (m.name, c) in seen:
                    continue
                seen.add((m.name, c))
                stack.append((m.name, c))
                if f := m.classes.get(c, {}).get(meth):
                    out.append(f)
        return sorted(out, key=lambda f: f.qname)

    def attr_type(self, mod: Module, cls: str, attr: str, _seen=None):
        """(defining module, type expr, exact) for self.attr, searching base classes too."""
        _seen = _seen if _seen is not None else set()
        if (mod.name, cls) in _seen:
            return None
        _seen.add((mod.name, cls))
        if (hit := mod.attr_types.get(cls, {}).get(attr)) is not None:
            return mod, hit[0], hit[1]
        for b in mod.bases.get(cls, []):
            r = self.class_of(mod, dotted(b) or "")
            if r and (found := self.attr_type(r[0], r[1], attr, _seen)):
                return found
        return None

    def module_name(self, path: Path) -> str:
        rel = path.resolve().relative_to(self.root).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    def load(self, name: str, own_only: bool = False) -> Module | None:
        if name in self.modules:
            return self.modules[name]
        if not name:
            return None
        base = self.root.joinpath(*name.split("."))
        for cand in (base.with_suffix(".py"), base / "__init__.py"):
            if cand.is_file() and not self.is_third_party(cand):
                return self.load_path(cand)
        if not own_only:
            for lp in self.libs:            # library fallback, in priority order
                if m := lp.load(name, own_only=True):
                    return m
        return None

    def load_path(self, path: Path) -> Module | None:
        name = self.module_name(path)
        if name in self.modules:
            return self.modules[name]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            return None
        mod = Module(name, path, tree, lib=self.lib)
        self.modules[name] = mod
        self._index(mod)
        return mod

    def _index(self, mod: Module) -> None:
        pkg = mod.name if mod.path.name == "__init__.py" else mod.name.rpartition(".")[0]
        for node in mod.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                mod.funcs[node.name] = FuncInfo(f"{mod.name}:{node.name}", mod, node)
            elif isinstance(node, ast.ClassDef):
                mod.bases[node.name] = node.bases
                methods = mod.classes.setdefault(node.name, {})
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods[item.name] = FuncInfo(
                            f"{mod.name}:{node.name}.{item.name}", mod, item, node.name)
                mod.attr_types[node.name] = self._self_attrs(node)
        # imports anywhere (function-local imports included)
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        mod.imports[a.asname] = (a.name, None)
                    else:
                        top = a.name.split(".")[0]
                        mod.imports[top] = (top, None)
            elif isinstance(node, ast.ImportFrom):
                target = node.module or ""
                if node.level:
                    parts = pkg.split(".") if pkg else []
                    parts = parts[: len(parts) - (node.level - 1)]
                    target = ".".join(p for p in (*parts, target) if p)
                for a in node.names:
                    if a.name == "*":
                        mod.star.append(target)
                    else:
                        mod.imports[a.asname or a.name] = (target, a.name)

    @staticmethod
    def _self_attrs(cls: ast.ClassDef) -> dict[str, tuple[ast.expr, bool]]:
        """self.attr -> (type expr, exact). Sources: `self.x = Cls()` (exact), `self.x: T = ..`,
        and `self.x = param` where `param: T` is annotated (declared type, may be a subclass)."""
        out: dict[str, tuple[ast.expr, bool]] = {}
        declared: set[str] = set()
        is_self = lambda t: (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                             and t.value.id == "self")
        for item in cls.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            ann = {a.arg: a.annotation for a in (*item.args.posonlyargs, *item.args.args,
                                                 *item.args.kwonlyargs) if a.annotation}
            for n in ast.walk(item):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if not is_self(t) or t.attr in declared:
                            continue
                        if isinstance(n.value, ast.Call):
                            out[t.attr] = (n.value.func, True)
                        elif isinstance(n.value, ast.Name) and n.value.id in ann:
                            out[t.attr] = (ann[n.value.id], False)
                            declared.add(t.attr)
                elif isinstance(n, ast.AnnAssign) and is_self(n.target):
                    out[n.target.attr] = (n.annotation, False)
                    declared.add(n.target.attr)
        return out

    # ---- symbol resolution -------------------------------------------------

    def resolve_symbol(self, mod: Module, name: str, _seen=None) -> tuple[Module, str] | None:
        """Resolve a bare name in `mod` to (defining module, top-level name)."""
        _seen = _seen if _seen is not None else set()
        if (mod.name, name) in _seen:
            return None
        _seen.add((mod.name, name))
        if name in mod.funcs or name in mod.classes:
            return mod, name
        imp = mod.imports.get(name)
        if not imp:
            for star in mod.star:                      # from x import *
                if (m := self.load(star)) and (r := self.resolve_symbol(m, name, _seen)):
                    return r
            return None
        if imp[1] is None:
            return None
        target, sym = imp
        m = self.load(target)
        if m:
            r = self.resolve_symbol(m, sym, _seen)   # follows re-exports in __init__
            if r:
                return r
        sub = self.load(f"{target}.{sym}")           # `from pkg import submodule`
        return (sub, "") if sub else None

    def resolve_module(self, mod: Module, name: str) -> Module | None:
        imp = mod.imports.get(name)
        if not imp:
            return None
        target, sym = imp
        return self.load(target if sym is None else f"{target}.{sym}")

    def find_method(self, mod: Module, cls: str, meth: str, _seen=None) -> FuncInfo | None:
        _seen = _seen if _seen is not None else set()
        if (mod.name, cls) in _seen:
            return None
        _seen.add((mod.name, cls))
        if meth in mod.classes.get(cls, {}):
            return mod.classes[cls][meth]
        for b in mod.bases.get(cls, []):
            bn = dotted(b)
            if not bn:
                continue
            r = self.class_of(mod, bn)
            if r and (found := self.find_method(r[0], r[1], meth, _seen)):
                return found
        return None

    def class_of(self, mod: Module, expr_name: str) -> tuple[Module, str] | None:
        head, _, rest = expr_name.partition(".")
        if not rest:
            r = self.resolve_symbol(mod, head)
            return r if r and r[1] in r[0].classes else None
        m = self.resolve_module(mod, head)
        if m:
            r = self.resolve_symbol(m, rest.split(".")[-1])
            return r if r and r[1] in r[0].classes else None
        return None


def dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Subscript):          # Optional[Foo] -> Foo (list[Foo] etc. are not Foo)
        base = dotted(node.value) or ""
        return dotted(node.slice) if base.split(".")[-1] == "Optional" else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):      # Foo | None
        for side in (node.left, node.right):
            if not (isinstance(side, ast.Constant) and side.value is None) and (d := dotted(side)):
                return d
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class Tracer:
    def __init__(self, project: Project, max_depth: int | None, external: bool, all_calls: bool = False,
                 lib_depth: int = 2):
        self.p = project
        self.lib_depth = lib_depth          # how many levels deep to follow once inside library code
        self.all_calls = all_calls
        self.max_depth = max_depth
        self.external = external

    def local_types(self, fn: FuncInfo) -> dict[str, tuple[str, bool]]:
        """var -> (dotted class expr, exact) from `x = Cls()` (exact) and annotations (declared)."""
        types: dict[str, tuple[str, bool]] = {}
        node = fn.node
        for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            if a.annotation and (d := dotted(a.annotation)):
                types[a.arg] = (d, False)
        for n in ast.walk(node):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                d = dotted(n.value.func)
                if d:
                    for t in n.targets:
                        if isinstance(t, ast.Name) and t.id not in types:
                            types[t.id] = (d, True)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                if d := dotted(n.annotation):
                    types[n.target.id] = (d, False)
        return types

    def calls(self, fn: FuncInfo, ctx=None) -> list[Call]:
        """Source-ordered, de-duplicated calls in fn.

        ctx is the concrete class `self` is known to be here (propagated from the entry point,
        e.g. `x = Foo(); x.run()` makes `self` a Foo inside run), so `self.m()` resolves to
        Foo's own override. Without it, method calls on a merely *declared* receiver type also
        yield every subclass override as `override=True`."""
        types = self.local_types(fn)
        found = [n for n in ast.walk(fn.node) if isinstance(n, ast.Call)]
        found.sort(key=lambda c: (c.lineno, c.col_offset))
        out, seen = [], set()
        for c in found:
            label = dotted(c.func)
            if label is None:
                if isinstance(c.func, ast.Attribute):
                    label = f"….{c.func.attr}"      # e.g. foo().bar()
                else:
                    continue
            target, recv, child_ctx = self.resolve_call(fn, c.func, types, ctx)
            key = target.qname if target else label
            if key in seen and not self.all_calls:
                continue
            seen.add(key)
            ext = None if target else self.external_path(fn, c.func, types, ctx)
            out.append(Call(c.lineno, label, target, ctx=child_ctx, ext_path=ext))
            if recv is not None and isinstance(c.func, ast.Attribute):
                for f in self.p.overriders(recv[0], recv[1], c.func.attr):
                    if f.qname not in seen or self.all_calls:
                        seen.add(f.qname)
                        out.append(Call(c.lineno, label, f, override=True, ctx=(f.module, f.cls)))
        return out

    def resolve_call(self, fn: FuncInfo, func: ast.expr, types: dict[str, tuple[str, bool]], ctx=None):
        """-> (target FuncInfo | None, receiver class | None, ctx for the callee).
        receiver is returned only when its type is declared rather than exact, i.e. when a
        subclass override might be what actually runs. ctx is the exact class of the callee's
        `self` when that is known."""
        mod = fn.module
        none = (None, None, None)
        if isinstance(func, ast.Name):
            return self._by_symbol(mod, func.id)
        if not isinstance(func, ast.Attribute):
            return none
        meth, recv = func.attr, func.value
        own = ctx or ((mod, fn.cls) if fn.cls else None)      # class whose MRO `self` uses
        # self.method() / cls.method()
        if isinstance(recv, ast.Name) and recv.id in ("self", "cls") and own:
            t = self.p.find_method(own[0], own[1], meth)
            return (t, None, ctx) if ctx else (t, own, None)
        # super().method()
        if (isinstance(recv, ast.Call) and isinstance(recv.func, ast.Name)
                and recv.func.id == "super" and fn.cls):
            for b in mod.bases.get(fn.cls, []):
                r = self.p.class_of(mod, dotted(b) or "")
                if r and (m := self.p.find_method(r[0], r[1], meth)):
                    return m, None, ctx
            return none
        # self.attr.method()   (attr type may come from a base class's __init__ annotation)
        if (isinstance(recv, ast.Attribute) and isinstance(recv.value, ast.Name)
                and recv.value.id == "self" and own):
            at = self.p.attr_type(own[0], own[1], recv.attr)
            if at and (d := dotted(at[1])):
                return self._method_on(at[0], d, meth, exact=at[2])
            return none
        if isinstance(recv, ast.Name):
            m = self.p.resolve_module(mod, recv.id)          # imported module: mod.func()
            if m:
                return self._by_symbol(m, meth)
            if recv.id in types:                              # typed variable
                d, exact = types[recv.id]
                res = self._method_on(mod, d, meth, exact=exact)
                if res[0] or res[1]:
                    return res
            t, _, _ = self._method_on(mod, recv.id, meth, exact=True)   # Class.method()
            return t, None, None
        d = dotted(recv)                                      # pkg.sub.func()
        if d:
            head, _, rest = d.partition(".")
            m = self.p.resolve_module(mod, head)
            if m:
                m2 = self.p.load(f"{m.name}.{rest}") if rest else m
                if m2:
                    return self._by_symbol(m2, meth)
        return none

    def import_path(self, mod: Module, name: str) -> str | None:
        """Dotted name as written in `mod` -> importable path (`pl.col` -> `polars.col`).
        Names that resolve into this project are not external, so they give None."""
        head, _, rest = name.partition(".")
        imp = mod.imports.get(head)
        if imp:
            base = imp[0] if imp[1] is None else f"{imp[0]}.{imp[1]}"
            path = f"{base}.{rest}" if rest else base
        elif head in BUILTIN_NAMES and not rest:
            path = f"builtins.{head}"
        else:
            return None
        return None if self.p.load(path.split(".")[0], own_only=True) else path

    def external_path(self, fn: FuncInfo, func: ast.expr, types, ctx=None) -> str | None:
        """Best static guess of where an unresolved call's definition lives (library/stdlib)."""
        mod = fn.module
        if isinstance(func, ast.Name):
            return self.import_path(mod, func.id)
        if not isinstance(func, ast.Attribute):
            return None
        label, recv = dotted(func), func.value
        own = ctx or ((mod, fn.cls) if fn.cls else None)
        if (isinstance(recv, ast.Attribute) and isinstance(recv.value, ast.Name)
                and recv.value.id == "self" and own):                  # self.attr.method()
            at = self.p.attr_type(own[0], own[1], recv.attr)
            if at and (d := dotted(at[1])) and (base := self.import_path(at[0], d)):
                return f"{base}.{func.attr}"
            return None
        if isinstance(recv, ast.Name) and recv.id in types:            # typed variable
            if base := self.import_path(mod, types[recv.id][0]):
                return f"{base}.{func.attr}"
            return None
        return self.import_path(mod, label) if label else None         # module.func / pkg.sub.func

    def _method_on(self, mod: Module, class_expr: str, meth: str, exact: bool):
        r = self.p.class_of(mod, class_expr)
        if not r:
            return None, None, None
        t = self.p.find_method(r[0], r[1], meth)
        return (t, None, r) if exact else (t, r, None)

    def _by_symbol(self, mod: Module, name: str):
        """-> (target, None, ctx): functions have no ctx; a constructor call makes ctx the class."""
        r = self.p.resolve_symbol(mod, name)
        if not r or not r[1]:
            return None, None, None
        m, sym = r
        if sym in m.funcs:
            return m.funcs[sym], None, None
        if sym in m.classes:                       # constructor call
            return self.p.find_method(m, sym, "__init__"), None, (m, sym)
        return None, None, None

    def build(self, fn: FuncInfo, depth: int = 0, stack: tuple[str, ...] = (), ctx=None,
              lib_level: int = 0) -> dict:
        node = {"name": fn.qname, "file": str(fn.module.path), "line": fn.node.lineno,
                "calls": [], "status": None, "lib": fn.lib}
        lib_level = lib_level + 1 if fn.lib else 0
        if ctx and ctx[1] != fn.cls:
            node["as"] = ctx[1]                    # running as a subclass of the defining class
        if fn.qname in stack:
            node["status"] = "recursive"
            return node
        if self.max_depth is not None and depth >= self.max_depth:
            node["status"] = "depth-limit"
            return node
        if lib_level > self.lib_depth:
            node["status"] = "lib-depth-limit"
            return node
        for c in self.calls(fn, ctx):
            if c.target is not None:
                child = self.build(c.target, depth + 1, (*stack, fn.qname), c.ctx, lib_level)
                child["call_line"], child["label"], child["override"] = c.line, c.label, c.override
                node["calls"].append(child)
            elif self.external:
                node["calls"].append({"name": c.label, "call_line": c.line, "label": c.label,
                                      "external": True, "path": c.ext_path, "calls": [], "status": None})
        return node


def render(node: dict, prefix: str = "", last: bool = True, top: bool = True) -> list[str]:
    if top:
        line = f"{node['name']}  ({node['file']}:{node['line']})"
        branch, child_prefix = "", ""
    else:
        branch = "└── " if last else "├── "
        child_prefix = prefix + ("    " if last else "│   ")
        if node.get("external"):
            line = f"{node['label']}  [external]  :{node['call_line']}"
        else:
            line = f"{node['name']}  :{node['call_line']}"
    if node.get("as"):
        line += f"  (as {node['as']})"
    if node.get("lib"):
        line += f"  ⬡ library: {node['lib']}"
    note = {"recursive": "  ↻ recursive", "depth-limit": "  … depth limit",
            "lib-depth-limit": "  … library depth limit"}.get(node["status"], "")
    if node.get("override"):
        note = "  ◇ override" + note
    out = [prefix + branch + line + note]
    kids = node["calls"]
    for i, k in enumerate(kids):
        out += render(k, child_prefix, i == len(kids) - 1, False)
    return out


def _walk(node: dict, ids: dict, edges: list, nodes: dict) -> str:
    """Flatten the tree into a graph: one vertex per function, one edge per distinct caller->callee."""
    key = ("ext:" + node["name"]) if node.get("external") else node["name"]
    if key not in ids:
        ids[key] = f"n{len(ids)}"
        nodes[ids[key]] = (("⬡ " if node.get("lib") else "") + node["name"]
                           if not node.get("external") else node["label"] + " [ext]")
    me = ids[key]
    for k in node["calls"]:
        child = _walk(k, ids, edges, nodes)
        if not any(e[:2] == (me, child) for e in edges):
            edges.append((me, child, bool(k.get("override"))))
    return me


def render_graph(tree: dict, fmt: str) -> str:
    edges, nodes = [], {}
    _walk(tree, {}, edges, nodes)
    esc = lambda t: t.replace('"', "'")
    if fmt == "mermaid":
        # backslash-escaped underscores keep __init__ from being parsed as markdown bold; <br/> splits module from function
        mm = lambda n: esc(n).replace("_", "\\_").replace(":", "<br/>", 1)
        out = ["graph LR"]
        out += [f'    {i}["{mm(n)}"]' for i, n in nodes.items()]
        out += [f"    {a} {'-.->' if dashed else '-->'} {b}" for a, b, dashed in edges]
    else:
        out = ["digraph trace {", "    rankdir=LR;", "    node [shape=box, fontname=monospace];"]
        out += [f'    {i} [label="{esc(n)}"];' for i, n in nodes.items()]
        out += [f"    {a} -> {b}{' [style=dashed]' if dashed else ''};" for a, b, dashed in edges]
        out.append("}")
    return "\n".join(out)


def render_png(mermaid_src: str, out: Path) -> None:
    """Render Mermaid source to PNG with headless Chrome + the vendored mermaid.js."""
    chrome = next((c for c in ("google-chrome", "chromium", "chromium-browser") if shutil.which(c)), None)
    js = Path(__file__).parent / "vendor" / "mermaid.min.js"
    if not chrome or not js.is_file():
        sys.exit("error: PNG rendering needs Chrome/Chromium on PATH and vendor/mermaid.min.js")
    page = (f'<html><body style="margin:0;background:#fff"><pre class="mermaid">{html.escape(mermaid_src)}</pre>'
            f'<script src="{js.resolve().as_uri()}"></script>'
            '<script>mermaid.initialize({startOnLoad:true,maxTextSize:900000,flowchart:{useMaxWidth:false}});</script>'
            '</body></html>')
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "g.html"
        f.write_text(page)
        subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                        "--window-size=2000,3300", "--virtual-time-budget=10000",
                        f"--screenshot={out.resolve()}", f.as_uri()],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def find_function(mod: Module, name: str) -> FuncInfo | None:
    if "." in name:
        cls, _, meth = name.partition(".")
        return mod.classes.get(cls, {}).get(meth)
    if name in mod.funcs:
        return mod.funcs[name]
    hits = [m[name] for m in mod.classes.values() if name in m]
    return hits[0] if len(hits) == 1 else None


def project_root(file: Path) -> Path:
    d = file.resolve().parent
    while (d / "__init__.py").is_file() and d.parent != d:
        d = d.parent
    return d


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("function", nargs="?", help="name or Class.method (or use FILE:FUNCTION)")
    ap.add_argument("--root", type=Path, help="import root (default: first dir above the file's package)")
    ap.add_argument("--depth", type=int, help="max call depth")
    ap.add_argument("--external", action="store_true", help="also list unresolved/external calls (leaf nodes)")
    ap.add_argument("--all-calls", action="store_true", help="show every call site, not one per distinct callee")
    ap.add_argument("--format", choices=["tree", "json", "mermaid", "dot"], default="tree")
    ap.add_argument("-o", "--output", type=Path,
                    help="write to file instead of stdout; a .png path renders the Mermaid graph (needs Chrome)")
    ap.add_argument("--libs", action="store_true",
                    help="also follow calls into installed libraries (site-packages, stdlib), marked ⬡")
    ap.add_argument("--lib-path", type=Path, action="append", default=[], metavar="DIR",
                    help="extra directory (or conda/venv root) to search for libraries; repeatable")
    ap.add_argument("--lib-depth", type=int, default=2,
                    help="with --libs: levels to follow inside library code (default 2)")
    ap.add_argument("-i", "--interactive", action="store_true",
                    help="open the colourful interactive explorer (curses)")
    ap.add_argument("--json", action="store_true", help="alias for --format json")
    a = ap.parse_args()
    if a.function is None:
        f, sep, fn_name = str(a.file).rpartition(":")
        if not sep or not f or not fn_name:
            ap.error("give FILE FUNCTION or FILE:FUNCTION")
        a.file, a.function = Path(f), fn_name
    if a.json:
        a.format = "json"
    if a.output:   # the output extension picks the format
        a.format = {".png": "mermaid", ".mmd": "mermaid", ".mermaid": "mermaid",
                    ".dot": "dot", ".gv": "dot", ".json": "json"}.get(a.output.suffix, a.format)

    if not a.file.is_file():
        print(f"error: no such file: {a.file}", file=sys.stderr)
        return 2
    root = a.root or project_root(a.file)
    proj = Project(root)
    try:
        mod = proj.load_path(a.file.resolve())
    except ValueError:
        print(f"error: {a.file} is not under root {root}", file=sys.stderr)
        return 2
    if mod is None:
        print(f"error: could not parse {a.file}", file=sys.stderr)
        return 2
    fn = find_function(mod, a.function)
    if fn is None:
        avail = sorted([*mod.funcs, *(f"{c}.{m}" for c, ms in mod.classes.items() for m in ms)])
        print(f"error: {a.function!r} not found (or ambiguous) in {a.file}\navailable: {', '.join(avail)}",
              file=sys.stderr)
        return 1
    if a.interactive:
        from tracer_tui import run
        return run(Tracer(proj, None, True, lib_depth=a.lib_depth), fn, a.external, a.libs, a.lib_path)
    if a.libs:
        from tracer_docs import LibraryDocs
        proj.libs = LibraryDocs(root, a.lib_path).projects
    tree = Tracer(proj, a.depth, a.external, a.all_calls, a.lib_depth).build(fn)
    if a.format == "json":
        text = json.dumps(tree, indent=2)
    elif a.format == "tree":
        text = "\n".join(render(tree))
    else:
        text = render_graph(tree, a.format)
    if a.output and a.output.suffix == ".png":
        render_png(text, a.output)
        print(f"wrote {a.output}")
    elif a.output:
        a.output.write_text(text + "\n")
        print(f"wrote {a.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
