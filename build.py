#!/usr/bin/env python3
"""Concatenate parts/*.py into dist/honcho_memory.py (single-file OWUI filter)."""
import ast
import pathlib
import re

root = pathlib.Path(__file__).parent
files = sorted((root / "parts").glob("[0-9][0-9]_*.py"))
imports, bodies, header = [], [], ""
for f in files:
    src = f.read_text()
    if f.name.startswith("00_"):
        header = src.rstrip() + "\n"
        continue
    lines = src.splitlines()
    keep = []
    for ln in lines:
        if re.match(r"^(import |from \S+ import )", ln):
            if ln not in imports: imports.append(ln)
        else:
            keep.append(ln)
    bodies.append(f"# ---- {f.name} ----\n" + "\n".join(keep).strip("\n") + "\n")
out = header + "\n" + "\n".join(imports) + "\n\n\n" + "\n\n".join(bodies)
ast.parse(out)
(root / "dist").mkdir(exist_ok=True)
(root / "dist" / "honcho_memory.py").write_text(out)
print(f"built dist/honcho_memory.py ({len(out.splitlines())} lines)")
