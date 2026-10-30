"""Documentation lookup for functions the tracer can't follow (libraries, stdlib, builtins).

Strategy, in order:
  1. static   - find the module's source in the interpreter / venv search dirs and read the
                docstring with `ast` (re-exports are followed). Nothing is imported or run.
  2. import   - only if the source isn't available (C extensions, builtins): a throw-away
                `python -I` subprocess imports the library and reads `inspect.getdoc`.
                Never touches the traced project's own code.
"""
from __future__ import annotations

import ast
import re
import site
import subprocess
import sys
import sysconfig
import textwrap
from dataclasses import dataclass
from pathlib import Path

import tracer as core


@dataclass
class Doc:
    title: str
    signature: str = ""
    location: str = ""
    text: str = ""
    source: str = ""        # how it was obtained
    found: bool = True


def signature_of(node: ast.AST) -> str:
    try:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            return f"{kw} {node.name}({ast.unparse(node.args)}){ret}"
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            sig = f"class {node.name}" + (f"({bases})" if bases else "")
            init = next((n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and n.name == "__init__"), None)
            return sig + (f"\n    __init__({ast.unparse(init.args)})" if init else "")
    except Exception:
        pass
    return ""


def docstring_of(node: ast.AST) -> str:
    doc = ast.get_docstring(node) or ""
    if not doc and isinstance(node, ast.ClassDef):          # many classes document __init__ instead
        for n in node.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "__init__":
                doc = ast.get_docstring(n) or ""
    return doc


def doc_for_project(fn: core.FuncInfo) -> Doc:
    node = fn.node
    return Doc(fn.qname.split(":", 1)[1], signature_of(node), f"{fn.module.path}:{node.lineno}",
               docstring_of(node) or "(this function has no docstring)", "project source")


class LibraryDocs:
    def __init__(self, project_root: Path):
        self.projects = [core.Project(d) for d in self._search_dirs(project_root)]
        self.cache: dict[str, Doc] = {}

    @staticmethod
    def _search_dirs(root: Path) -> list[Path]:
        cands: list[Path] = []
        for name in (".venv", "venv"):
            cands += sorted(Path(root, name, "lib").glob("python*/site-packages"))
        try:
            cands += [Path(p) for p in site.getsitepackages()] + [Path(site.getusersitepackages())]
        except Exception:
            pass
        paths = sysconfig.get_paths()
        cands += [Path(paths[k]) for k in ("purelib", "platlib", "stdlib") if k in paths]
        seen, out = set(), []
        for c in cands:
            if c.is_dir() and (r := c.resolve()) not in seen:
                seen.add(r)
                out.append(r)
        return out

    def lookup(self, path: str) -> Doc:
        if path not in self.cache:
            self.cache[path] = self._static(path) or self._imported(path)
        return self.cache[path]

    # ---- 1. static
    def _static(self, path: str) -> Doc | None:
        parts = path.split(".")
        if parts[0] == "builtins":
            return None
        try:
            for k in range(len(parts), 0, -1):
                for proj in self.projects:
                    mod = proj.load(".".join(parts[:k]))
                    if mod and (d := self._resolve(proj, mod, parts[k:], path)):
                        return d
        except Exception:
            return None
        return None

    def _resolve(self, proj: core.Project, mod: core.Module, attrs: list[str], path: str, depth: int = 0):
        if depth > 8:
            return None
        if not attrs:
            return Doc(mod.name, "", str(mod.path), ast.get_docstring(mod.tree) or "(module has no docstring)",
                       "parsed from source")
        r = proj.resolve_symbol(mod, attrs[0])
        if not r:
            return None
        dm, name = r
        if name == "":                                       # `from pkg import submodule`
            if len(attrs) == 1:       # a call target is never a module; likely a same-named object
                return None
            return self._resolve(proj, dm, attrs[1:], path, depth + 1)
        if name in dm.classes:
            cls = next(n for n in dm.tree.body if isinstance(n, ast.ClassDef) and n.name == name)
            if len(attrs) == 1:
                node = cls
            else:
                fi = proj.find_method(dm, name, attrs[1])
                node = fi.node if fi else None
        elif name in dm.funcs and len(attrs) == 1:
            node = dm.funcs[name].node
        else:
            node = None
        if node is None:
            return None
        return Doc(path, signature_of(node), f"{dm.path}:{node.lineno}",
                   docstring_of(node) or "(no docstring in the source)", "parsed from source")

    # ---- 2. import in a throw-away interpreter
    _PROBE = r'''
import sys, importlib, inspect
parts = sys.argv[1].split(".")
obj = rest = None
for k in range(len(parts), 0, -1):
    try:
        obj = importlib.import_module(".".join(parts[:k])); rest = parts[k:]; break
    except BaseException:
        continue
if rest is None:
    print("::notfound"); sys.exit(0)
try:
    for a in rest:
        obj = getattr(obj, a)
except BaseException:
    print("::notfound"); sys.exit(0)
try:
    sig = str(inspect.signature(obj))
except BaseException:
    sig = ""
try:
    where = inspect.getsourcefile(obj) or ""
except BaseException:
    where = ""
print("::sig:" + sig); print("::file:" + where); print("::doc"); print(inspect.getdoc(obj) or "")
'''

    def _imported(self, path: str) -> Doc:
        try:
            r = subprocess.run([sys.executable, "-I", "-c", self._PROBE, path],
                               capture_output=True, text=True, timeout=15, cwd="/")
            out = r.stdout.split("\n")
        except (subprocess.TimeoutExpired, OSError):
            out = ["::notfound"]
        if not out or out[0].startswith("::notfound") or len(out) < 4 or not out[0].startswith("::sig:"):
            return Doc(path, text=f"No documentation found for {path}.\n\n"
                                  "It was not found in the installed sources, and importing it failed.",
                       source="not found", found=False)
        sig = out[0][6:]
        where = out[1][7:]
        text = "\n".join(out[3:]).strip() or "(no docstring)"
        name = path.rsplit(".", 1)[-1]
        return Doc(path, f"{name}{sig}" if sig else "", where, text, "imported in a sandbox subprocess")


# ---- rendering: docstring -> styled, wrapped lines -------------------------------------------------

SECTION = {"args", "arguments", "parameters", "returns", "return", "raises", "yields", "example",
           "examples", "note", "notes", "attributes", "see also", "warns", "warning", "references"}
Seg = tuple[str, str]


def format_doc(doc: Doc, width: int) -> list[list[Seg]]:
    out: list[list[Seg]] = []
    width = max(20, width)

    def add(segs: list[Seg]) -> None:
        out.append(segs)

    if doc.title:
        add([(doc.title, "fn")])
    for sl in doc.signature.split("\n") if doc.signature else []:
        for w in textwrap.wrap(sl, width, subsequent_indent="        ", break_long_words=True) or [""]:
            add([(w, "call")])
    if doc.location:
        add([(doc.location, "dim")])
    add([(f"source: {doc.source}", "dim")])
    add([("─" * min(width, 60), "gutter")])
    add([])
    lines = doc.text.split("\n")
    for i, line in enumerate(lines):
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped and re.fullmatch(r"[-=~^]{3,}", nxt):                       # numpy/rst heading
            style = "kw"
        elif re.fullmatch(r"[-=~^]{3,}", stripped):
            style = "gutter"
        elif stripped.endswith(":") and stripped[:-1].lower() in SECTION:       # google-style heading
            style = "kw"
        elif stripped.startswith((">>>", "...")):
            style = "str"                                                       # doctest
        else:
            style = "text"
        segs_for = None
        m = re.match(r"(\s*)(:(?:param|type|returns?|rtype|raises?|var|ivar)\b[^:]*:)(.*)", line)
        if m and style == "text":
            segs_for = [(m.group(1), "text"), (m.group(2), "marker"), (m.group(3), "text")]
        if not stripped:
            add([])
            continue
        pad = " " * indent
        wrapped = textwrap.wrap(line, width, subsequent_indent=pad + ("  " if segs_for else ""),
                                break_long_words=True, replace_whitespace=False, drop_whitespace=False) or [""]
        if segs_for and len(wrapped) == 1:
            add(segs_for)
        else:
            for w in wrapped:
                add([(w, style)])
    return out
