"""Tests for build.py: verifies dist/honcho_memory.py is a valid single-file OWUI filter."""

import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "honcho_memory.py"


@pytest.fixture(scope="module")
def built() -> str:
    """Run build.py in place (idempotent) and return the built file's source."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "build.py")],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 0, f"build.py failed:\n{result.stderr}"
    return DIST.read_text()


def test_dist_parses(built: str) -> None:
    """The concatenated output is syntactically valid Python."""
    ast.parse(built)


def test_frontmatter_docstring(built: str) -> None:
    """First statement is a module docstring containing OWUI frontmatter fields."""
    tree = ast.parse(built)
    assert tree.body, "dist file is empty"
    first = tree.body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
    assert isinstance(first.value.value, str), "first statement is not a docstring"
    doc = first.value.value
    assert "title:" in doc, "frontmatter missing 'title:'"
    assert "version:" in doc, "frontmatter missing 'version:'"


def test_defines_filter_class(built: str) -> None:
    """The built module defines class Filter (OWUI filter entry point)."""
    tree = ast.parse(built)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    assert "Filter" in names


def test_imports_hoisted_and_deduped(built: str) -> None:
    """Top-level imports are collected at the top of the module and deduped."""
    tree = ast.parse(built)
    # imports must appear before any other top-level non-docstring statement
    seen_non_import = False
    imports = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert not seen_non_import, "import found after other statements: not hoisted"
            imports.append(ast.unparse(node))
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        else:
            seen_non_import = True
    assert imports, "expected top-level imports in the built file"
    rendered = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert len(rendered) == len(set(rendered)), "duplicate imports not deduped"


def test_no_part_to_part_imports(built: str) -> None:
    """No leftover `from parts` / `import parts` references in the built file."""
    tree = ast.parse(built)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] == "parts" for a in node.names), (
                f"bad import: {ast.unparse(node)}"
            )
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "parts", (
                f"bad import: {ast.unparse(node)}"
            )
    assert "from parts" not in built and "import parts" not in built


def test_build_in_temp_copy() -> None:
    """build.py also works in a copied tree (no absolute-path assumptions)."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        copy = Path(td) / "proj"
        shutil.copytree(
            ROOT,
            copy,
            ignore=shutil.ignore_patterns(".venv", "dist", "__pycache__", ".git"),
        )
        result = subprocess.run(
            [sys.executable, str(copy / "build.py")],
            capture_output=True,
            text=True,
            check=False,
            cwd=copy,
        )
        assert result.returncode == 0, f"build.py failed in temp copy:\n{result.stderr}"
        src = (copy / "dist" / "honcho_memory.py").read_text()
        ast.parse(src)
        assert "title:" in src and "class Filter" in src