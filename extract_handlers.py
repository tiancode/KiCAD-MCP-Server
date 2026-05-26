"""
One-shot helper: extract _handle_* methods from python/kicad_interface.py
into a target handlers/<module>.py, leaving trampolines behind.

Usage:
  python3 extract_handlers.py <module_name> <handler1> [<handler2> ...]

Example:
  python3 extract_handlers.py schematic_component \\
      _handle_add_schematic_component _handle_delete_schematic_component ...

This is intentionally simple — no AST manipulation, just literal block
extraction by line numbers found via regex.  It rewrites `self.` to
`iface.` inside the moved bodies and removes `self,` from the signature.
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
KICAD_PY = REPO / "python" / "kicad_interface.py"
HANDLERS_DIR = REPO / "python" / "handlers"

# Handler module headers — bare bones, the script appends individual
# handler defs below.  We deliberately keep imports minimal; each
# handler body imports lazily via existing `from commands.foo import …`
# patterns the original file already used.
MODULE_PREAMBLE = '''"""
{title} handlers, extracted from kicad_interface.py.

See python/handlers/__init__.py for the calling convention.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

import sexpdata

from commands.schematic import SchematicManager
from commands.wire_manager import WireManager

if TYPE_CHECKING:
    from kicad_interface import KiCADInterface

logger = logging.getLogger(__name__)


'''


def find_handler_block(lines, name):
    """Return (start_idx, end_idx_exclusive, indent) for a _handle_<name> def
    in `lines`.  start_idx points at the `def` line; end_idx is the first line
    after the method that is dedented to method level."""
    pat = re.compile(r"^(\s*)def " + re.escape(name) + r"\(")
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        indent = m.group(1)
        # Walk forward to first line that's at indent level (or less) and not blank
        j = i + 1
        method_indent_len = len(indent)
        while j < len(lines):
            ln = lines[j]
            if ln.strip() == "":
                j += 1
                continue
            stripped_indent = len(ln) - len(ln.lstrip())
            if stripped_indent <= method_indent_len:
                break
            j += 1
        return i, j, indent
    raise KeyError(name)


def transform_body(body_lines, indent):
    """Strip the original `def _handle_X(self, params): ...` indent down to
    module level, convert self→iface in the body, rewrite signature."""
    # body_lines includes the `def` line itself
    first = body_lines[0]
    m = re.match(r"^\s*def (\w+)\(\s*self\s*,\s*([^)]*)\)\s*->\s*(.+):\s*$", first)
    if not m:
        # Some defs may span multiple lines (rare here); fall back
        m = re.match(r"^\s*def (\w+)\(", first)
        if not m:
            raise ValueError(f"Could not parse def line: {first!r}")
    original_name = m.group(1)
    assert original_name.startswith("_handle_"), original_name
    public_name = "handle_" + original_name[len("_handle_") :]

    # Rebuild signature
    new_def = (
        f"def {public_name}(iface: \"KiCADInterface\", params: Dict[str, Any]) -> Dict[str, Any]:\n"
    )

    # Body without the def line.  The original method body is indented at
    # `indent + 4` spaces (4 for the class body, 4 for the method body).
    # We want a free-function body at 4 spaces, so strip just `indent` chars.
    body = body_lines[1:]
    strip_n = len(indent)
    new_body = []
    for ln in body:
        if ln.strip() == "":
            new_body.append("\n")
            continue
        # Strip the method-level indent
        if ln.startswith(" " * strip_n):
            new_ln = ln[strip_n:]
        else:
            # Less-indented line in the middle of a body — shouldn't happen
            # for well-formed Python but tolerate it
            new_ln = ln.lstrip()
        # self. → iface.
        new_ln = re.sub(r"\bself\.", "iface.", new_ln)
        new_body.append(new_ln)

    return public_name, original_name, [new_def] + new_body


def make_trampoline(original_name, public_name, module_alias, module_name, indent):
    """Build a trampoline that replaces the original handler block."""
    return (
        f"{indent}def {original_name}(self, params: Dict[str, Any]) -> Dict[str, Any]:\n"
        f"{indent}    from handlers import {module_name} as {module_alias}\n"
        f"{indent}\n"
        f"{indent}    return {module_alias}.{public_name}(self, params)\n"
    )


def main():
    module_name = sys.argv[1]
    handler_names = sys.argv[2:]
    if not handler_names:
        sys.exit("usage: extract_handlers.py <module> <handler1> ...")

    text = KICAD_PY.read_text()
    lines = text.splitlines(keepends=True)

    title = module_name.replace("_", " ").title()
    module_path = HANDLERS_DIR / f"{module_name}.py"
    if module_path.exists():
        out_lines = module_path.read_text().splitlines(keepends=True)
    else:
        out_lines = list(MODULE_PREAMBLE.format(title=title).splitlines(keepends=True))

    # Process from the bottom of the file upward so earlier line numbers
    # don't shift as we delete blocks.
    located = []
    for hname in handler_names:
        start, end, indent = find_handler_block(lines, hname)
        located.append((start, end, indent, hname))
    located.sort(key=lambda t: t[0], reverse=True)

    module_alias = "_" + "".join(p[:1] for p in module_name.split("_")) or "_h"
    if len(module_alias) < 2:
        module_alias = "_mod"

    for start, end, indent, hname in located:
        body_lines = lines[start:end]
        public_name, original_name, new_def = transform_body(body_lines, indent)
        # Append to module file
        out_lines.extend(["\n"] + new_def)
        # Replace original with trampoline
        trampoline = make_trampoline(original_name, public_name, module_alias, module_name, indent)
        lines[start:end] = [trampoline]

    # Write outputs
    KICAD_PY.write_text("".join(lines))
    module_path.write_text("".join(out_lines))

    print(
        f"Wrote {module_path.name}: +{len(handler_names)} handlers; "
        f"kicad_interface.py now {len(lines)} lines"
    )


if __name__ == "__main__":
    main()
