#!/usr/bin/env python3
"""tracer: print the chain of functions called from a given Python function.

usage: tracer.py FILE FUNCTION | FILE:FUNCTION [--root DIR] [--depth N] [--external]
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
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FuncInfo:
    qname: str                  # module:Class.method
    module: "Module"
    node: ast.AST
    cls: str | None = None


@dataclass
class Module:
    name: str
    path: Path
    tree: ast.Module
    funcs: dict[str, FuncInfo] = field(default_factory=dict)       # top-level functions
    classes: dict[str, dict[str, FuncInfo]] = field(default_factory=dict)
    bases: dict[str, list[ast.expr]] = field(default_factory=dict)
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)  # alias -> (module, symbol|None)
    attr_types: dict[str, dict[str, ast.expr]] = field(default_factory=dict)  # class -> self.attr -> ctor expr


class Project:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.modules: dict[str, Module] = {}

    def module_name(self, path: Path) -> str:
        rel = path.resolve().relative_to(self.root).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    def load(self, name: str) -> Module | None:
        if name in self.modules:
            return self.modules[name]
        if not name:
            return None
        base = self.root.joinpath(*name.split("."))
        for cand in (base.with_suffix(".py"), base / "__init__.py"):
            if cand.is_file():
                return self.load_path(cand)
        return None

    def load_path(self, path: Path) -> Module | None:
        name = self.module_name(path)
        if name in self.modules:
            return self.modules[name]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            return None
        mod = Module(name, path, tree)
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
                    mod.imports[a.asname or a.name] = (target, a.name)

    @staticmethod
    def _self_attrs(cls: ast.ClassDef) -> dict[str, ast.expr]:
        out: dict[str, ast.expr] = {}
        for n in ast.walk(cls):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                for t in n.targets:
                    if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                            and t.value.id == "self"):
                        out[t.attr] = n.value.func
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Attribute):
                t = n.target
                if isinstance(t.value, ast.Name) and t.value.id == "self":
                    out[t.attr] = n.annotation
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
        if not imp or imp[1] is None:
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
    if isinstance(node, ast.Subscript):          # Optional[Foo] / list[Foo] -> Foo
        return dotted(node.slice)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class Tracer:
    def __init__(self, project: Project, max_depth: int | None, external: bool, all_calls: bool = False):
        self.p = project
        self.all_calls = all_calls
        self.max_depth = max_depth
        self.external = external

    def local_types(self, fn: FuncInfo) -> dict[str, str]:
        """var name -> dotted class expression, from `x = Cls()` and annotations."""
        types: dict[str, str] = {}
        node = fn.node
        for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            if a.annotation and (d := dotted(a.annotation)):
                types[a.arg] = d
        for n in ast.walk(node):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                d = dotted(n.value.func)
                if d:
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            types[t.id] = d
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                if d := dotted(n.annotation):
                    types[n.target.id] = d
        return types

    def calls(self, fn: FuncInfo) -> list[tuple[int, str, FuncInfo | None]]:
        """Source-ordered, de-duplicated (line, label, resolved target) for calls in fn."""
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
            target = self.resolve_call(fn, c.func, types)
            key = target.qname if target else label
            if key in seen and not self.all_calls:
                continue
            seen.add(key)
            out.append((c.lineno, label, target))
        return out

    def resolve_call(self, fn: FuncInfo, func: ast.expr, types: dict[str, str]) -> FuncInfo | None:
        mod = fn.module
        if isinstance(func, ast.Name):
            return self._by_symbol(mod, func.id)
        if not isinstance(func, ast.Attribute):
            return None
        meth, recv = func.attr, func.value
        # self.method() / cls.method()
        if isinstance(recv, ast.Name) and recv.id in ("self", "cls") and fn.cls:
            return self.p.find_method(mod, fn.cls, meth)
        # super().method()
        if (isinstance(recv, ast.Call) and isinstance(recv.func, ast.Name)
                and recv.func.id == "super" and fn.cls):
            for b in mod.bases.get(fn.cls, []):
                r = self.p.class_of(mod, dotted(b) or "")
                if r and (m := self.p.find_method(r[0], r[1], meth)):
                    return m
            return None
        # self.attr.method()
        if (isinstance(recv, ast.Attribute) and isinstance(recv.value, ast.Name)
                and recv.value.id == "self" and fn.cls):
            ctor = mod.attr_types.get(fn.cls, {}).get(recv.attr)
            if ctor is not None and (d := dotted(ctor)):
                return self._method_on(mod, d, meth)
            return None
        if isinstance(recv, ast.Name):
            m = self.p.resolve_module(mod, recv.id)          # imported module: mod.func()
            if m:
                return self._by_symbol(m, meth)
            if recv.id in types and (r := self._method_on(mod, types[recv.id], meth)):
                return r                                      # typed variable
            return self._method_on(mod, recv.id, meth)        # Class.method()
        d = dotted(recv)                                      # pkg.sub.func()
        if d:
            head, _, rest = d.partition(".")
            m = self.p.resolve_module(mod, head)
            if m:
                m2 = self.p.load(f"{m.name}.{rest}") if rest else m
                if m2:
                    return self._by_symbol(m2, meth)
        return None

    def _method_on(self, mod: Module, class_expr: str, meth: str) -> FuncInfo | None:
        r = self.p.class_of(mod, class_expr)
        return self.p.find_method(r[0], r[1], meth) if r else None

    def _by_symbol(self, mod: Module, name: str) -> FuncInfo | None:
        r = self.p.resolve_symbol(mod, name)
        if not r or not r[1]:
            return None
        m, sym = r
        if sym in m.funcs:
            return m.funcs[sym]
        if sym in m.classes:                       # constructor call
            return self.p.find_method(m, sym, "__init__")
        return None

    def build(self, fn: FuncInfo, depth: int = 0, stack: tuple[str, ...] = ()) -> dict:
        node = {"name": fn.qname, "file": str(fn.module.path), "line": fn.node.lineno,
                "calls": [], "status": None}
        if fn.qname in stack:
            node["status"] = "recursive"
            return node
        if self.max_depth is not None and depth >= self.max_depth:
            node["status"] = "depth-limit"
            return node
        for line, label, target in self.calls(fn):
            if target is not None:
                child = self.build(target, depth + 1, (*stack, fn.qname))
                child["call_line"], child["label"] = line, label
                node["calls"].append(child)
            elif self.external:
                node["calls"].append({"name": label, "call_line": line, "label": label,
                                      "external": True, "calls": [], "status": None})
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
    note = {"recursive": "  ↻ recursive", "depth-limit": "  … depth limit"}.get(node["status"], "")
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
        nodes[ids[key]] = (node["name"] if not node.get("external") else node["label"] + " [ext]")
    me = ids[key]
    for k in node["calls"]:
        child = _walk(k, ids, edges, nodes)
        if (me, child) not in edges:
            edges.append((me, child))
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
        out += [f"    {a} --> {b}" for a, b in edges]
    else:
        out = ["digraph trace {", "    rankdir=LR;", "    node [shape=box, fontname=monospace];"]
        out += [f'    {i} [label="{esc(n)}"];' for i, n in nodes.items()]
        out += [f"    {a} -> {b};" for a, b in edges]
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
    tree = Tracer(proj, a.depth, a.external, a.all_calls).build(fn)
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
