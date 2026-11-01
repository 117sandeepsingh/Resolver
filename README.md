# Resolver

Resolver prints the **chain of functions called from a given Python function**. It works statically: it parses your code with `ast` and never imports or runs it.

You can use it three ways:

- as a plain **tree** printed to the terminal
- as a **graph** exported to JSON, Mermaid, Graphviz DOT or PNG
- as a colourful **interactive terminal explorer** (curses) with syntax-highlighted source, jump-into-callee, docstring lookup and optional following into library code

## Contents

1. [Requirements](#requirements)
2. [Files](#files)
3. [Launching: every syntax](#launching-every-syntax)
4. [Options reference](#options-reference)
5. [Output formats](#output-formats)
6. [Interactive explorer](#interactive-explorer)
7. [Following into libraries](#following-into-libraries)
8. [Reading the output: markers](#reading-the-output-markers)
9. [Errors and exit codes](#errors-and-exit-codes)
10. [How resolution works and its limits](#how-resolution-works-and-its-limits)

---

## Requirements

- Python 3.10+. Standard library only, so there is nothing to `pip install`.
- A real terminal for interactive mode (`curses`; Linux/macOS, or Windows with `windows-curses`).
- Optional, only for PNG output (`-o file.png`):
  - Chrome or Chromium on `PATH` (`google-chrome`, `chromium` or `chromium-browser`)
  - `vendor/mermaid.min.js` (already included, mermaid 11.4.1)

## Files

| File | Purpose |
|------|---------|
| `resolver.py` | Core resolver and the main CLI (tree / json / mermaid / dot / png, `-i` launches the explorer) |
| `resolver_tui.py` | Interactive curses explorer; can also be launched directly |
| `resolver_docs.py` | Docstring and signature lookup for library functions and library-source discovery |
| `vendor/mermaid.min.js` | Used for PNG rendering |
| `out/` | Example outputs (git-ignored) |

---

## Launching: every syntax

`FUNCTION` is either `name` (a top-level function) or `Class.method`.
A bare method name works too, if it is unique in the file.

### Print a call tree (default)

```bash
python3 resolver.py FILE FUNCTION
python3 resolver.py FILE:FUNCTION            # colon form, one argument
python3 resolver.py path/to/app.py main
python3 resolver.py path/to/app.py Service.handle     # a method
python3 resolver.py path/to/app.py:Service.handle     # a method, colon form
```

Example:

```text
$ python3 resolver.py resolver.py:project_root
resolver:project_root  (/home/sandeep/Resolver/resolver.py:604)
```

### Limit depth, show more or fewer calls

```bash
python3 resolver.py app.py main --depth 3        # stop after 3 levels
python3 resolver.py app.py main --external       # also list unresolved/library calls as leaves
python3 resolver.py app.py main --all-calls      # every call site, not one per distinct callee
python3 resolver.py app.py main --root /path/to/project   # set the import root explicitly
```

### Choose an output format

```bash
python3 resolver.py app.py main --format tree      # default
python3 resolver.py app.py main --format json
python3 resolver.py app.py main --json             # alias for --format json
python3 resolver.py app.py main --format mermaid
python3 resolver.py app.py main --format dot
```

### Write to a file (the extension picks the format)

```bash
python3 resolver.py app.py main -o chain.mmd       # Mermaid text
python3 resolver.py app.py main -o chain.mermaid   # Mermaid text
python3 resolver.py app.py main -o chain.dot       # Graphviz DOT (also .gv)
python3 resolver.py app.py main -o chain.json      # JSON
python3 resolver.py app.py main -o chain.png       # rendered PNG (needs Chrome/Chromium)
python3 resolver.py app.py main -o chain.txt       # any other extension: keeps --format (tree by default)
```

`--output` / `-o` always overrides `--format` for the extensions above.

### Follow calls into installed libraries

```bash
python3 resolver.py app.py main --libs                       # follow into site-packages / stdlib
python3 resolver.py app.py main --libs --lib-depth 4         # follow deeper (default 2)
python3 resolver.py app.py main --libs --lib-path ~/miniconda3/envs/ml   # extra env to search
python3 resolver.py app.py main --libs --lib-path /env/a --lib-path /env/b   # repeatable
```

### Interactive explorer

```bash
python3 resolver.py app.py main -i                    # via the main CLI
python3 resolver.py app.py:main -i
python3 resolver_tui.py app.py main                   # directly
python3 resolver_tui.py app.py:main
python3 resolver_tui.py app.py main --external        # start with library calls visible
python3 resolver_tui.py app.py main --libs            # start with "follow into libraries" on
python3 resolver_tui.py app.py main --libs --lib-path /path/to/venv --root /path/to/project
```

### Help

```bash
python3 resolver.py -h
python3 resolver_tui.py -h
```

### Make it a command (optional)

```bash
chmod +x resolver.py
ln -s "$PWD/resolver.py" ~/.local/bin/resolver
resolver app.py:main -i
```

Keep the `resolver*.py` files and `vendor/` together; the symlink resolves back to them.

---

## Options reference

### `resolver.py`

| Option | Meaning |
|--------|---------|
| `FILE` | Python file containing the starting function. Also accepts `FILE:FUNCTION`. |
| `FUNCTION` | `name` or `Class.method`. Optional when you use `FILE:FUNCTION`. |
| `--root DIR` | Import root of the project. Default: the first directory above the file's package (the walk goes up while `__init__.py` exists). |
| `--depth N` | Maximum call depth. Default: unlimited (recursion is detected and stops). |
| `--external` | Also list unresolved or external calls (stdlib, third-party, dynamic) as leaf nodes. |
| `--all-calls` | List every call site instead of one entry per distinct callee. |
| `--format tree\|json\|mermaid\|dot` | Output format. Default `tree`. |
| `--json` | Alias for `--format json`. |
| `-o`, `--output PATH` | Write to a file instead of stdout. The extension chooses the format (see above). |
| `--libs` | Follow calls into installed libraries (site-packages, stdlib), marked `⬡`. |
| `--lib-path DIR` | Extra directory, or venv/conda root, to search for libraries. Repeatable. |
| `--lib-depth N` | With `--libs`: how many levels to follow inside library code. Default 2. |
| `-i`, `--interactive` | Open the interactive explorer. |

### `resolver_tui.py`

| Option | Meaning |
|--------|---------|
| `FILE`, `FUNCTION` | As above; `FILE:FUNCTION` is accepted. |
| `--root DIR` | Import root. |
| `--external` | Start with calls into libraries visible (toggle with `x`). |
| `--libs` | Start with "follow into library source" on (toggle with `L`). |
| `--lib-path DIR` | Extra library search location. Repeatable. |

Note that the TUI always explores the **full** chain lazily. It expands nodes as you open them, so `--depth` does not apply.

---

## Output formats

### Tree (default)

```text
main  (app.py:12)
├── load_config  :14
│   └── parse_yaml  :3
├── Service.handle  :20  (as handler)
│   ├── Service.validate  :31
│   └── Service.handle  :44  ↻ recursive
└── requests.get  [external]  :22        # only with --external
```

Each child line shows the callee and the line number of the call site in its caller.

### JSON

A nested structure. Each node has `name`, `file`, `line`, `calls` (children), `status`, plus `call_line`, `label`, `override`, `external` and `lib` where relevant.

### Mermaid and DOT

One vertex per function and one edge per distinct caller→callee pair. Subclass-override edges are dashed (`-.->` in Mermaid, `style=dashed` in DOT). Library functions are prefixed `⬡`. External leaves are suffixed `[ext]`.

```bash
python3 resolver.py app.py main -o chain.mmd     # paste into https://mermaid.live
python3 resolver.py app.py main -o chain.dot && dot -Tsvg chain.dot -o chain.svg
```

### PNG

`-o chain.png` renders the Mermaid graph with headless Chrome and the vendored `mermaid.min.js`. If Chrome or the JS file is missing, the tool exits with a clear error.

---

## Interactive explorer

Launch it with `-i` or `resolver_tui.py` (see [Launching](#interactive-explorer)). It needs a real terminal; piping or redirecting prints `error: interactive mode needs a terminal`.

### Layout

```text
┌ breadcrumbs: main › Service.handle › Service.validate ─────────────────┐   ← row 0 (clickable)
│ Tree pane (call chain)     │ Code pane (syntax-highlighted source)      │
│  ▾ main                    │  12  def main():                           │
│    ▸ load_config           │  13      cfg = load_config()  ← cursor     │
│    ▾ Service.handle        │  ...                                       │
├─────────────────────────────┴────────────────────────────────────────────┤
│ status line: file:line · call count · what Enter / d will do             │
└──────────────────────────────────────────────────────────────────────────┘
```

- **Tree pane** (left): the call tree, expanded lazily as you open nodes.
- **Code pane** (right): source of the selected function with Python syntax colours. The selected line shows which calls it makes.
- **Breadcrumb bar** (top): the path from the root to the current function. Click any ancestor to jump to it.
- **Status line** (bottom): context for the current selection. It says whether a node is a library call, a recursion or an override, and what Enter or `d` will do.

Only one pane has focus at a time. Keys behave differently per pane.

### Keys: Tree pane

| Key | Action |
|-----|--------|
| `↑` `↓` or `k` `j` | Move selection |
| `→` or `l` | Open the node; if already open, step into its first call; if it has no calls, focus the code pane |
| `←` or `h` | Close the node; if already closed, go to its caller |
| `Enter` or `Tab` | Focus the code pane (for a library/external call: show its docs) |
| `Space` | Expand / collapse |
| `*` | Expand everything below the selected node |
| `C` | Collapse everything and return to the root |
| `PgUp` `PgDn` | Move a page |
| `g` / `Home`, `G` / `End` | Jump to top / bottom |
| `Esc` | Clear search highlights and the message line |

### Keys: Code pane

| Key | Action |
|-----|--------|
| `↑` `↓` or `k` `j` | Move the line cursor |
| `PgUp` / `PgDn` or `Space` | Page up / down |
| `Ctrl-D`, `Ctrl-U` | Half page down / up |
| `g` / `Home`, `G` / `End` | First / last line |
| `Enter` | **Jump into** the function called on the cursor line (if several, a chooser appears) |
| `u` or `Backspace` | Go up to the caller |
| `→` or `l` | Scroll sideways (8 columns) |
| `←` or `h` | Scroll back; at the left edge it returns to the tree |
| `Tab` or `Esc` | Return to the tree pane |

### Keys: everywhere

| Key | Action |
|-----|--------|
| `d` or `K` | Docs: the docstring for the function on the cursor line or the selected node. Works for project functions, libraries, stdlib and builtins. If a line has several calls, you pick one |
| `/` | Search by function name |
| `n` / `N` | Next / previous search match |
| `[` / `]` | History back / forward through your jumps |
| `x` | Show / hide calls into libraries (as unexpandable leaves) |
| `L` | Toggle following **into** library source (marked `⬡`, purple) |
| `e` | Open the current function in `$VISUAL`/`$EDITOR` (default `vi`) at the cursor line |
| `?` | Help (scroll with `↑↓`; any other key closes) |
| `q` or `Ctrl-C` | Quit |

### Chooser popups

When a line makes several calls, or a doc lookup is ambiguous, a numbered chooser opens:

- `↑` `↓` / `j` `k` / `Tab`: move
- `1`–`9`: pick by number
- `Enter`: confirm
- `Esc` or `q`: cancel

### Docs pager

Opened by `d`, `K`, or `Enter` on a library node:

- `↑` `↓` / `j` `k`: scroll a line
- `PgDn` / `Space` / `Ctrl-F`, `PgUp` / `b` / `Ctrl-B`: scroll a page
- `Esc`, `q`, `d`, `K` or `Enter`: close

### Mouse

| Action | Effect |
|--------|--------|
| Click a tree row | Select it |
| Click the `▸`/`▾` glyph | Expand / collapse |
| Double-click a tree row | Open the code pane (or show docs for library calls) |
| Click a code line | Move the cursor there and focus the code pane |
| Double-click a code line | Jump into the call on that line |
| Mouse wheel | Scroll the pane under the pointer |
| Click a breadcrumb | Jump to that ancestor |

### Typical workflow

1. `python3 resolver.py app.py:main -i`
2. `→` / `Space` to open nodes, or `*` to expand everything.
3. `Enter` on a node to read its code.
4. Move to a call line and press `Enter` to dive into the callee.
5. `d` on any call to read its docstring.
6. `[` to go back, `u` to go up to the caller, `/` to find a function by name.
7. `e` to edit, `q` to quit.

---

## Following into libraries

By default only code **inside your project root** is traced. Third-party and stdlib calls are hidden (or shown as leaves with `--external` / `x`).

With `--libs` (or `L` in the explorer) Resolver also parses the source of installed packages and follows calls into them, marking those nodes `⬡`.

Library search order:

1. Project virtualenvs: `<root>/.venv`, `<root>/venv`
2. The running interpreter's site-packages
3. The standard library
4. Directories passed with `--lib-path` (a plain directory, or a venv/conda root; named conda envs under `envs/*` are found automatically)
5. Conda fallbacks

Notes:

- `--lib-depth N` controls how many levels deep to go inside library code (default 2).
- Search, expand-all and `walk_all` never wander inside libraries, so `*` stays bounded.
- **Docs lookup** (`d`) first parses the library source statically and reads the docstring with `ast`. Nothing is imported. Only if no source exists (C extensions, builtins) does it import the library in a throw-away `python -I` subprocess and read `inspect.getdoc`. It never imports your project.
- Opening library source with `e` warns you: edits would change your installed package.

---

## Reading the output: markers

| Marker | Meaning |
|--------|---------|
| `:N` | The call site is at line N of the caller |
| `(as X)` | The callee was reached through the alias or name `X` |
| `↻ recursive` | This function is already on the current chain; expansion stops |
| `… depth limit` | `--depth` reached |
| `… library depth limit` | `--lib-depth` reached |
| `◇ override` | A subclass override of the statically resolved method that might be the one actually running |
| `⬡` / `⬡ library: …` | The function lives in a library (site-packages, stdlib); shows its origin |
| `[external]` | A call that couldn't be resolved into any source (leaf; shown with `--external`) |
| dashed edge | Override edge in Mermaid / DOT output |

---

## Errors and exit codes

| Exit | Meaning |
|------|---------|
| `0` | Success |
| `1` | Function not found or ambiguous. The message lists every available name in that file, such as `Class.method`. |
| `2` | Bad input: file missing, file not under the root, file can't be parsed, or interactive mode without a terminal |

Typical messages:

```text
error: no such file: app.py
error: 'nope' not found (or ambiguous) in app.py
available: Service.handle, Service.validate, load_config, main
error: PNG rendering needs Chrome/Chromium on PATH and vendor/mermaid.min.js
error: interactive mode needs a terminal
```

If a method name is ambiguous across classes, qualify it as `Class.method`.

---

## How resolution works and its limits

Resolver resolves calls statically and is deliberately conservative:

- It resolves direct calls to functions, `Class.method`, imported names and aliases, `self.method()`, `super()`, and `self.attr.method()` when the attribute's type can be inferred (for example `self.x = Cls()` or an annotation).
- It tracks the **concrete class** carried down the chain, so `self` inside a callee reflects the actual subclass.
- Method calls on a merely *declared* type also show subclass overrides, marked `◇`.
- It follows imports into your project, including relative imports and `from x import *`.
- It skips `site-packages`, `.venv`, `venv`, `.tox`, `.git`, `__pycache__`, `node_modules` and `.eggs` when scanning your project.

It cannot see:

- calls through dynamically computed names (`getattr`, `eval`, dispatch dicts)
- calls on values whose type isn't statically known (shown as `[external]` leaves with `--external`; docs unavailable)
- code generated at runtime, monkey-patching, or decorators that swap out the function

Treat the result as "what the code visibly calls", not a runtime trace.
