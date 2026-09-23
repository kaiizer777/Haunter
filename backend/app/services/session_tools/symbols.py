"""
AST & Code Intelligence tools for the Haunter session agent.

Provides structural code understanding without heavy binary dependencies:
- tool_get_file_outline: Extract signatures, classes, docstrings from a file.
- tool_find_symbol: Locate definitions of functions, classes, interfaces, types.
- tool_find_references: Find all call sites and usages of a symbol.

Python (.py): Uses built-in ast module (ast.parse, FunctionDef, ClassDef).
TypeScript/JS (.ts, .tsx, .js, .jsx): Structural regex extractors.
"""

from __future__ import annotations

import ast
import logging
import re
from typing import Any

from app.github_client import (
    GitHubClientError,
    fetch_file_content,
    fetch_git_tree,
)
from app.services.session_tools.recon import _validate_file_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_IDENTIFIER_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

_SOURCE_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx")

_IGNORE_PREFIXES = (
    ".git/",
    ".github/",
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "dist/",
    "build/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".next/",
)

_MAX_REFERENCES = 50


# ---------------------------------------------------------------------------
# Python AST outline extraction
# ---------------------------------------------------------------------------

def _python_outline(source: str) -> str:
    """
    Parse Python source with ast and return a clean indented outline.

    Extracts top-level classes (with their methods) and top-level functions.
    Includes first-line docstrings where present.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"[AST parse error: {exc}]"

    lines: list[str] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            bases = (
                ", ".join(ast.unparse(b) for b in node.bases)
                if node.bases
                else ""
            )
            class_sig = f"class {node.name}({bases}):" if bases else f"class {node.name}:"
            lines.append(class_sig)

            doc = ast.get_docstring(node)
            if doc:
                first_doc = doc.split("\n")[0].strip()
                lines.append(f'  """{first_doc}"""')

            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                    try:
                        args_str = ast.unparse(child.args)
                    except Exception:
                        args_str = "..."
                    returns = ""
                    if child.returns is not None:
                        try:
                            returns = f" -> {ast.unparse(child.returns)}"
                        except Exception:
                            pass
                    lines.append(f"  {prefix} {child.name}({args_str}){returns}:")
                    method_doc = ast.get_docstring(child)
                    if method_doc:
                        first_doc = method_doc.split("\n")[0].strip()
                        lines.append(f'    """{first_doc}"""')

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            try:
                args_str = ast.unparse(node.args)
            except Exception:
                args_str = "..."
            returns = ""
            if node.returns is not None:
                try:
                    returns = f" -> {ast.unparse(node.returns)}"
                except Exception:
                    pass
            lines.append(f"{prefix} {node.name}({args_str}){returns}:")
            doc = ast.get_docstring(node)
            if doc:
                first_doc = doc.split("\n")[0].strip()
                lines.append(f'  """{first_doc}"""')

    return "\n".join(lines) if lines else "(no top-level symbols found)"


# ---------------------------------------------------------------------------
# TypeScript/JS structural regex outline extraction
# ---------------------------------------------------------------------------

# interface Foo { ... }
_TS_INTERFACE_RE = re.compile(
    r"(?:export\s+)?interface\s+(\w+)(?:\s+extends\s+[\w,\s<>]+)?\s*\{([^}]*)\}",
    re.MULTILINE,
)

# type Foo = ...;
_TS_TYPE_RE = re.compile(
    r"(?:export\s+)?type\s+(\w+)(?:<[^>]*>)?\s*=\s*([^;]+);",
    re.MULTILINE,
)

# class Foo / export class Foo / abstract class Foo
_TS_CLASS_RE = re.compile(
    r"(?:export\s+)?(?:abstract\s+)?class\s+(\w+)(?:<[^>]*>)?(?:\s+extends\s+[\w<>,\s]+)?(?:\s+implements\s+[\w,\s<>]+)?\s*\{",
    re.MULTILINE,
)

# method declarations inside a class block
_TS_METHOD_RE = re.compile(
    r"(?:(?:public|private|protected|static|async|override|readonly)\s+)*"
    r"(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)\s*(?::\s*[\w<>\[\]|&\s,]+)?\s*(?:\{|;)",
    re.MULTILINE,
)

# export function foo(...)
_TS_EXPORT_FN_RE = re.compile(
    r"export\s+(?:async\s+)?function\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)\s*(?::\s*[\w<>\[\]|\s,&]+)?",
    re.MULTILINE,
)

# export const foo = (...) =>
_TS_EXPORT_CONST_FN_RE = re.compile(
    r"export\s+const\s+(\w+)\s*(?::\s*[\w<>\[\]|&\s,]+)?\s*=\s*(?:async\s+)?(?:\([^)]*\)|(?:\w+))\s*=>",
    re.MULTILINE,
)

# enum Foo { ... }
_TS_ENUM_RE = re.compile(
    r"(?:export\s+)?(?:const\s+)?enum\s+(\w+)\s*\{([^}]*)\}",
    re.MULTILINE,
)

_TS_SKIP_KEYWORDS = frozenset({"if", "for", "while", "switch", "catch", "return", "new"})


def _ts_outline(source: str) -> str:
    """Extract structural outline from TypeScript/JavaScript source via regex."""
    lines: list[str] = []

    for m in _TS_INTERFACE_RE.finditer(source):
        name = m.group(1)
        body = m.group(2).strip()
        members = [ln.strip().rstrip(",;") for ln in body.splitlines() if ln.strip()]
        lines.append(f"interface {name} {{")
        for member in members[:8]:
            lines.append(f"  {member}")
        if len(members) > 8:
            lines.append(f"  ... ({len(members) - 8} more)")
        lines.append("}")

    for m in _TS_TYPE_RE.finditer(source):
        name = m.group(1)
        val = m.group(2).strip()
        if len(val) > 80:
            val = val[:77] + "..."
        lines.append(f"type {name} = {val};")

    for m in _TS_ENUM_RE.finditer(source):
        name = m.group(1)
        body = m.group(2).strip()
        members = [ln.strip().rstrip(",") for ln in body.splitlines() if ln.strip()]
        lines.append(f"enum {name} {{")
        for member in members[:6]:
            lines.append(f"  {member}")
        if len(members) > 6:
            lines.append(f"  ... ({len(members) - 6} more)")
        lines.append("}")

    for m in _TS_CLASS_RE.finditer(source):
        name = m.group(1)
        class_start = m.end()
        class_body_snippet = source[class_start : class_start + 4000]
        lines.append(f"class {name} {{")
        method_count = 0
        for mm in _TS_METHOD_RE.finditer(class_body_snippet):
            method_name = mm.group(1)
            if method_name in _TS_SKIP_KEYWORDS:
                continue
            params = mm.group(2).strip()
            if len(params) > 60:
                params = params[:57] + "..."
            lines.append(f"  {method_name}({params})")
            method_count += 1
            if method_count >= 10:
                lines.append("  ...")
                break
        lines.append("}")

    for m in _TS_EXPORT_FN_RE.finditer(source):
        name = m.group(1)
        params = m.group(2).strip()
        if len(params) > 60:
            params = params[:57] + "..."
        lines.append(f"export function {name}({params})")

    for m in _TS_EXPORT_CONST_FN_RE.finditer(source):
        name = m.group(1)
        lines.append(f"export const {name} = (...) =>")

    return "\n".join(lines) if lines else "(no top-level symbols found)"


# ---------------------------------------------------------------------------
# Shared file content resolver
# ---------------------------------------------------------------------------

async def _resolve_file_content(
    path: str,
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    staged_patches: dict[str, str],
    gh_token: str | None,
) -> str | None:
    """
    Return full file content from GitHub.

    staged_patches are unified diffs, not full source — we always fetch from
    GitHub for AST/outline parsing purposes.
    """
    try:
        return await fetch_file_content(
            owner=repo_owner,
            repo=repo_name,
            path=path,
            sha=base_sha,
            token=gh_token,
        )
    except GitHubClientError as exc:
        logger.warning("symbols: fetch_file_content error for %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# tool_get_file_outline
# ---------------------------------------------------------------------------

async def tool_get_file_outline(
    path: str,
    repo_owner: str,
    repo_name: str,
    base_sha: str,
    staged_patches: dict[str, str],
    gh_token: str | None = None,
) -> str:
    """
    Return a compact structural outline of a source file.

    Python: AST-based extraction of classes, methods, functions, docstrings.
    TS/JS: Regex-based extraction of interfaces, types, classes, exported functions.
    No function bodies included — saves context window tokens.
    """
    try:
        path = _validate_file_path(path)
    except ValueError as exc:
        return f"Error: {exc}"

    ext = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path else ""
    if ext not in _SOURCE_EXTENSIONS:
        return (
            f"Error: unsupported file extension {ext!r}. "
            f"Supported: {', '.join(_SOURCE_EXTENSIONS)}"
        )

    content = await _resolve_file_content(
        path=path,
        repo_owner=repo_owner,
        repo_name=repo_name,
        base_sha=base_sha,
        staged_patches=staged_patches,
        gh_token=gh_token,
    )
    if content is None:
        return f"File not found: {path!r}"

    outline = _python_outline(content) if ext == ".py" else _ts_outline(content)
    return f"# Outline: {path}\n\n{outline}"


# ---------------------------------------------------------------------------
# Python AST definition search
# ---------------------------------------------------------------------------

def _find_py_recursive(
    node: ast.AST,
    name: str,
    kind: str | None,
    results: list[tuple[int, str]],
) -> None:
    """Recursively walk Python AST collecting definitions matching name/kind."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if child.name == name and (kind is None or kind == "function"):  # type: ignore[attr-defined]
                lineno = getattr(child, "lineno", 0)
                prefix = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                try:
                    args_str = ast.unparse(child.args)  # type: ignore[attr-defined]
                except Exception:
                    args_str = "..."
                returns = ""
                if child.returns is not None:  # type: ignore[union-attr]
                    try:
                        returns = f" -> {ast.unparse(child.returns)}"  # type: ignore[union-attr]
                    except Exception:
                        pass
                results.append((lineno, f"{prefix} {child.name}({args_str}){returns}"))  # type: ignore[attr-defined]
            _find_py_recursive(child, name, kind, results)

        elif isinstance(child, ast.ClassDef):
            if child.name == name and (kind is None or kind == "class"):  # type: ignore[attr-defined]
                lineno = getattr(child, "lineno", 0)
                results.append((lineno, f"class {child.name}"))  # type: ignore[attr-defined]
            # Always recurse into class body for nested defs
            _find_py_recursive(child, name, kind, results)

        else:
            _find_py_recursive(child, name, kind, results)


def _find_py_defs(source: str, name: str, kind: str | None) -> list[tuple[int, str]]:
    """Parse Python source and return (lineno, signature) pairs for `name`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    results: list[tuple[int, str]] = []
    _find_py_recursive(tree, name, kind, results)
    return results


# ---------------------------------------------------------------------------
# TypeScript/JS definition search
# ---------------------------------------------------------------------------

def _find_ts_defs(source: str, name: str, kind: str | None) -> list[tuple[int, str]]:
    """
    Search TS/JS source for declarations of `name` matching optional `kind`.

    Returns (lineno, line_content) pairs.
    """
    escaped = re.escape(name)
    source_lines = source.splitlines()

    def _lineno(pos: int) -> int:
        return source.count("\n", 0, pos) + 1

    def _line_at(pos: int) -> str:
        ln = _lineno(pos)
        raw = source_lines[ln - 1].strip() if ln <= len(source_lines) else ""
        return raw[:120] + "..." if len(raw) > 120 else raw

    patterns: list[tuple[str, re.Pattern[str]]] = []

    if kind is None or kind == "function":
        patterns.append((
            "function",
            re.compile(
                rf"(?:export\s+)?(?:async\s+)?function\s+{escaped}\s*(?:<[^>]*>)?\s*\(",
                re.MULTILINE,
            ),
        ))
        patterns.append((
            "function",
            re.compile(
                rf"export\s+const\s+{escaped}\s*(?::\s*[\w<>\[\]|&\s,]+)?\s*=\s*(?:async\s+)?(?:\([^)]*\)|\w+)\s*=>",
                re.MULTILINE,
            ),
        ))

    if kind is None or kind == "class":
        patterns.append((
            "class",
            re.compile(
                rf"(?:export\s+)?(?:abstract\s+)?class\s+{escaped}\b",
                re.MULTILINE,
            ),
        ))

    if kind is None or kind == "interface":
        patterns.append((
            "interface",
            re.compile(
                rf"(?:export\s+)?interface\s+{escaped}\b",
                re.MULTILINE,
            ),
        ))

    if kind is None or kind == "type":
        patterns.append((
            "type",
            re.compile(
                rf"(?:export\s+)?type\s+{escaped}\b",
                re.MULTILINE,
            ),
        ))

    results: list[tuple[int, str]] = []
    seen_lines: set[int] = set()
    for _kind_label, pattern in patterns:
        for m in pattern.finditer(source):
            ln = _lineno(m.start())
            if ln not in seen_lines:
                seen_lines.add(ln)
                results.append((ln, _line_at(m.start())))

    return results


# ---------------------------------------------------------------------------
# tool_find_symbol
# ---------------------------------------------------------------------------

async def tool_find_symbol(
    name: str,
    kind: str | None = None,
    repo_owner: str = "",
    repo_name: str = "",
    base_sha: str = "",
    staged_patches: dict[str, str] | None = None,
    gh_token: str | None = None,
) -> str:
    """
    Locate definitions of functions, classes, interfaces, or types across the repo.

    Scans .py, .ts, .tsx, .js, .jsx source files.
    Filters by kind if specified: "function", "class", "interface", "type".
    Returns: path:line: signature for each match.
    """
    if staged_patches is None:
        staged_patches = {}

    if not _IDENTIFIER_RE.fullmatch(name):
        return (
            f"Error: invalid symbol identifier {name!r}. "
            "Must match ^[a-zA-Z_][a-zA-Z0-9_]*$"
        )

    valid_kinds = {"function", "class", "interface", "type"}
    if kind is not None and kind not in valid_kinds:
        return f"Error: invalid kind {kind!r}. Must be one of: {', '.join(sorted(valid_kinds))}"

    try:
        tree_data = await fetch_git_tree(
            owner=repo_owner, repo=repo_name, tree_sha=base_sha, recursive=True, token=gh_token
        )
    except GitHubClientError as exc:
        return f"Error fetching repository tree: {exc}"

    items: list[dict[str, Any]] = tree_data.get("tree", [])
    candidate_files: list[str] = [
        item["path"]
        for item in items
        if item.get("type") == "blob"
        and item.get("path", "")
        and not any(
            item["path"].startswith(ign) or f"/{ign}" in f"/{item['path']}"
            for ign in _IGNORE_PREFIXES
        )
        and any(item["path"].endswith(ext) for ext in _SOURCE_EXTENSIONS)
    ]

    matches: list[str] = []
    for file_path in candidate_files:
        content = await fetch_file_content(
            owner=repo_owner, repo=repo_name, path=file_path, sha=base_sha, token=gh_token
        )
        if not content:
            continue

        ext = ("." + file_path.rsplit(".", 1)[-1].lower()) if "." in file_path else ""
        if ext == ".py":
            # interface/type kinds don't exist in Python
            if kind in ("interface", "type"):
                continue
            hits = _find_py_defs(content, name, kind)
        else:
            hits = _find_ts_defs(content, name, kind)

        for lineno, sig in hits:
            matches.append(f"{file_path}:{lineno}: {sig}")

    if not matches:
        kind_label = f" (kind={kind!r})" if kind else ""
        return f"No definitions found for symbol {name!r}{kind_label}"

    return f"Found {len(matches)} definition(s) for {name!r}:\n" + "\n".join(matches)


# ---------------------------------------------------------------------------
# tool_find_references
# ---------------------------------------------------------------------------

async def tool_find_references(
    symbol: str,
    path: str | None = None,
    repo_owner: str = "",
    repo_name: str = "",
    base_sha: str = "",
    staged_patches: dict[str, str] | None = None,
    gh_token: str | None = None,
) -> str:
    """
    Find all call sites and usages of a symbol across the repository.

    Uses word-boundary regex r"\\b<symbol>\\b" so "user" matches "user.id"
    but NOT "username". Results capped at 50 to prevent context bloat.

    Returns: file_path:line_number: code_line
    """
    if staged_patches is None:
        staged_patches = {}

    if not _IDENTIFIER_RE.fullmatch(symbol):
        return (
            f"Error: invalid symbol identifier {symbol!r}. "
            "Must match ^[a-zA-Z_][a-zA-Z0-9_]*$"
        )

    path_filter: str | None = None
    if path:
        try:
            path_filter = _validate_file_path(path)
        except ValueError as exc:
            return f"Error: {exc}"

    word_re = re.compile(r"\b" + re.escape(symbol) + r"\b")

    try:
        tree_data = await fetch_git_tree(
            owner=repo_owner, repo=repo_name, tree_sha=base_sha, recursive=True, token=gh_token
        )
    except GitHubClientError as exc:
        return f"Error fetching repository tree: {exc}"

    items: list[dict[str, Any]] = tree_data.get("tree", [])
    candidate_files: list[str] = []
    for item in items:
        if item.get("type") != "blob":
            continue
        p: str = item.get("path", "")
        if not p:
            continue
        if path_filter and not p.startswith(path_filter):
            continue
        if any(p.startswith(ign) or f"/{ign}" in f"/{p}" for ign in _IGNORE_PREFIXES):
            continue
        if not any(p.endswith(ext) for ext in _SOURCE_EXTENSIONS):
            continue
        candidate_files.append(p)

    matches: list[str] = []
    for file_path in candidate_files:
        if len(matches) >= _MAX_REFERENCES:
            break
        content = await fetch_file_content(
            owner=repo_owner, repo=repo_name, path=file_path, sha=base_sha, token=gh_token
        )
        if not content:
            continue
        for line_num, line in enumerate(content.splitlines(), start=1):
            if word_re.search(line):
                stripped = line.strip()
                if len(stripped) > 200:
                    stripped = stripped[:197] + "..."
                matches.append(f"{file_path}:{line_num}: {stripped}")
                if len(matches) >= _MAX_REFERENCES:
                    break

    if not matches:
        return f"No references found for symbol {symbol!r}"

    capped = len(matches) >= _MAX_REFERENCES
    suffix = "+ (capped at 50)" if capped else ""
    return (
        f"Found {len(matches)}{suffix} reference(s) for {symbol!r}:\n"
        + "\n".join(matches)
    )


# Module-level aliases for consistent __init__.py export
get_file_outline = tool_get_file_outline
find_symbol = tool_find_symbol
find_references = tool_find_references
