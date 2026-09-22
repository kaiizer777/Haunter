"""
AST and Symbol Call-Graph Analyzer Subagent Helper.

Provides stack trace parsing across Python and TypeScript/JavaScript logs,
and extracts enclosing AST scopes (classes, function signatures, parameters,
return types, imports) for frames inside the target repository.

Guards:
  - Caps extracted frames to at most max_frames (default 5) to prevent GitHub
    API secondary rate limits.
  - Zero external C-bindings: uses Python's standard library `ast` for Python
    sources and a lightweight regex/indentation analyzer for TS/JS/other files,
    maintaining zero-dependency compatibility with AWS Lambda runtimes.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import os
import re
from typing import Any, Optional


@dataclass(frozen=True)
class StackFrame:
    """Represents a single parsed stack frame from CI failure logs."""

    file_path: str
    line_number: int
    symbol_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Stack Trace Regexes
# ---------------------------------------------------------------------------

# Standard Python traceback line:
#   File "/path/to/file.py", line 42, in my_func
#   File "backend/app/main.py", line 15, in <module>
_PY_TRACE_RE = re.compile(
    r'File\s+["\'](?P<path>[^"\']+\.py)["\'],\s+line\s+(?P<line>\d+)(?:,\s+in\s+(?P<symbol>[^\n\r]+))?',
    re.IGNORECASE,
)

# Pytest short traceback style:
#   backend/tests/test_foo.py:28: in test_method
#   app/services/ai.py:45: in generate
_PYTEST_TRACE_RE = re.compile(
    r"^(?:\s*)(?P<path>[\w./\\-]+\.py):(?P<line>\d+):\s+in\s+(?P<symbol>\S+)",
    re.MULTILINE,
)

# Node.js / TypeScript stack trace line:
#   at Function.myMethod (/path/to/repo/src/index.ts:42:15)
#   at async doSomething (src/utils.ts:12:3)
#   at /path/to/repo/src/index.js:99:5
_NODE_TRACE_RE = re.compile(
    r"^\s*at\s+(?:(?P<async>async\s+)?(?P<symbol>[\w.$<>]+)\s+\((?P<path_with_paren>[^)]+):(?P<line1>\d+):(?P<col1>\d+)\)|(?P<path_bare>[^\s:]+):(?P<line2>\d+):(?P<col2>\d+))",
    re.MULTILINE,
)

# Generic fallback for other languages:
#   path/to/file.ext:42:10 or path/to/file.ext:42
_GENERIC_TRACE_RE = re.compile(
    r"(?:^|[\s\"'(])(?P<path>[\w./\\-]+\.(?:py|ts|js|tsx|jsx|go|rs|java|rb)):(?P<line>\d+)(?::\d+)?(?:[\s\"')]|$)",
    re.MULTILINE,
)


def _normalize_path(raw_path: str) -> str:
    """Normalize file path to forward slashes and strip whitespace."""
    return raw_path.replace("\\", "/").strip()


def _match_repo_path(extracted_path: str, repo_paths: list[str]) -> Optional[str]:
    """
    Match an extracted path (which may be absolute or relative) against known repo paths.
    Returns the matching repo relative path or None.
    """
    norm = _normalize_path(extracted_path)

    # Direct match
    if norm in repo_paths:
        return norm

    # Match by suffix (e.g. /home/runner/work/repo/repo/app/main.py -> app/main.py)
    for rp in repo_paths:
        if norm.endswith("/" + rp) or rp.endswith("/" + norm):
            return rp

    return None


def extract_stack_frames(
    logs_text: str,
    repo_paths: Optional[list[str]] = None,
    max_frames: int = 5,
) -> list[StackFrame]:
    """
    Parse failure logs for stack trace frames and match them against known repo files.

    Guards:
      - Excludes frames from third-party/virtualenv/node_modules/standard library.
      - Restricts to at most `max_frames` unique frames (default 5) to protect
        GitHub API rate limits.
      - Preserves ordering from deepest frame to shallowest (or chronologically as seen).
    """
    if not logs_text:
        return []

    found_frames: list[StackFrame] = []
    seen: set[tuple[str, int]] = set()

    def _resolve_repo_file(path_str: str) -> Optional[str]:
        if repo_paths:
            return _match_repo_path(path_str, repo_paths)
        # Fallback if repo_paths not supplied: use normalized relative path
        norm = _normalize_path(path_str)
        if any(ign in norm for ign in ("site-packages", "dist-packages", "node_modules", "hostedtoolcache", "lib/python")):
            return None
        # Strip leading slashes
        return norm.lstrip("/")

    # 1. Check Python tracebacks
    for match in _PY_TRACE_RE.finditer(logs_text):
        raw_path = match.group("path")
        line_str = match.group("line")
        symbol = (match.group("symbol") or "").strip() or None

        # Ignore standard library and external virtualenvs
        if "site-packages" in raw_path or "dist-packages" in raw_path or "lib/python" in raw_path:
            continue

        matched_repo_file = _resolve_repo_file(raw_path)
        if matched_repo_file:
            try:
                line_no = int(line_str)
            except ValueError:
                continue

            key = (matched_repo_file, line_no)
            if key not in seen:
                seen.add(key)
                found_frames.append(StackFrame(file_path=matched_repo_file, line_number=line_no, symbol_name=symbol))
                if len(found_frames) >= max_frames:
                    return found_frames

    # 2. Check Pytest tracebacks
    for match in _PYTEST_TRACE_RE.finditer(logs_text):
        raw_path = match.group("path")
        line_str = match.group("line")
        symbol = (match.group("symbol") or "").strip() or None

        matched_repo_file = _resolve_repo_file(raw_path)
        if matched_repo_file:
            try:
                line_no = int(line_str)
            except ValueError:
                continue

            key = (matched_repo_file, line_no)
            if key not in seen:
                seen.add(key)
                found_frames.append(StackFrame(file_path=matched_repo_file, line_number=line_no, symbol_name=symbol))
                if len(found_frames) >= max_frames:
                    return found_frames

    # 3. Check Node/TypeScript tracebacks
    for match in _NODE_TRACE_RE.finditer(logs_text):
        raw_path = match.group("path_with_paren") or match.group("path_bare") or ""
        line_str = match.group("line1") or match.group("line2") or "0"
        symbol = (match.group("symbol") or "").strip() or None

        if "node_modules" in raw_path:
            continue

        matched_repo_file = _resolve_repo_file(raw_path)
        if matched_repo_file:
            try:
                line_no = int(line_str)
            except ValueError:
                continue

            key = (matched_repo_file, line_no)
            if key not in seen:
                seen.add(key)
                found_frames.append(StackFrame(file_path=matched_repo_file, line_number=line_no, symbol_name=symbol))
                if len(found_frames) >= max_frames:
                    return found_frames

    # 4. Fallback generic trace pattern if no frames found yet
    if not found_frames:
        for match in _GENERIC_TRACE_RE.finditer(logs_text):
            raw_path = match.group("path")
            line_str = match.group("line")

            if "site-packages" in raw_path or "node_modules" in raw_path:
                continue

            matched_repo_file = _resolve_repo_file(raw_path)
            if matched_repo_file:
                try:
                    line_no = int(line_str)
                except ValueError:
                    continue

                key = (matched_repo_file, line_no)
                if key not in seen:
                    seen.add(key)
                    found_frames.append(StackFrame(file_path=matched_repo_file, line_number=line_no, symbol_name=None))
                    if len(found_frames) >= max_frames:
                        return found_frames

    return found_frames[:max_frames]


# ---------------------------------------------------------------------------
# Python AST Extraction
# ---------------------------------------------------------------------------


def _format_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Extract and reconstruct function signature including parameter types and return type."""
    is_async = isinstance(node, ast.AsyncFunctionDef)
    prefix = "async def " if is_async else "def "

    args_strs: list[str] = []

    # Positional-only args (Python 3.8+)
    posonly = getattr(node.args, "posonlyargs", [])
    for arg in posonly:
        arg_str = arg.arg
        if arg.annotation:
            arg_str += f": {ast.unparse(arg.annotation)}"
        args_strs.append(arg_str)
    if posonly:
        args_strs.append("/")

    # Regular positional args with defaults
    total_args = len(node.args.args)
    defaults = node.args.defaults
    num_defaults = len(defaults)
    offset = total_args - num_defaults

    for idx, arg in enumerate(node.args.args):
        arg_str = arg.arg
        if arg.annotation:
            arg_str += f": {ast.unparse(arg.annotation)}"
        if idx >= offset:
            default_node = defaults[idx - offset]
            arg_str += f" = {ast.unparse(default_node)}"
        args_strs.append(arg_str)

    # Vararg (*args)
    if node.args.vararg:
        var_str = f"*{node.args.vararg.arg}"
        if node.args.vararg.annotation:
            var_str += f": {ast.unparse(node.args.vararg.annotation)}"
        args_strs.append(var_str)
    elif node.args.kwonlyargs:
        args_strs.append("*")

    # Keyword-only args
    for kwarg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        kw_str = kwarg.arg
        if kwarg.annotation:
            kw_str += f": {ast.unparse(kwarg.annotation)}"
        if default is not None:
            kw_str += f" = {ast.unparse(default)}"
        args_strs.append(kw_str)

    # Kwarg (**kwargs)
    if node.args.kwarg:
        kwarg_str = f"**{node.args.kwarg.arg}"
        if node.args.kwarg.annotation:
            kwarg_str += f": {ast.unparse(node.args.kwarg.annotation)}"
        args_strs.append(kwarg_str)

    sig = f"{prefix}{node.name}({', '.join(args_strs)})"
    if node.returns:
        sig += f" -> {ast.unparse(node.returns)}:"
    else:
        sig += ":"

    return sig


def extract_python_ast_context(source_code: str, line_number: int) -> Optional[dict[str, Any]]:
    """
    Analyze Python source code using built-in `ast` and extract:
      - Enclosing function/method name and reconstructed signature
      - Enclosing class name (if inside a class)
      - Module-level import declarations
      - Enclosing code snippet (start line, end line, snippet string)
    """
    if not source_code:
        return None

    try:
        tree = ast.parse(source_code)
    except Exception:
        return None

    lines = source_code.splitlines()

    # Collect module-level imports
    imports: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(ast.unparse(node))

    # Find enclosing function / class containing line_number
    enclosing_func: Optional[ast.FunctionDef | ast.AsyncFunctionDef] = None
    enclosing_class: Optional[ast.ClassDef] = None

    class ScopeVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.current_classes: list[ast.ClassDef] = []
            self.matching_func: Optional[ast.FunctionDef | ast.AsyncFunctionDef] = None
            self.matching_class: Optional[ast.ClassDef] = None

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.current_classes.append(node)
            self.generic_visit(node)
            self.current_classes.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._check_func(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._check_func(node)

        def _check_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            start = getattr(node, "lineno", 0)
            end = getattr(node, "end_lineno", start)
            if start <= line_number <= end:
                # Innermost match
                self.matching_func = node
                if self.current_classes:
                    self.matching_class = self.current_classes[-1]
            self.generic_visit(node)

    visitor = ScopeVisitor()
    visitor.visit(tree)
    enclosing_func = visitor.matching_func
    enclosing_class = visitor.matching_class

    if not enclosing_func:
        # Check if line is at least inside a class body
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                start = getattr(node, "lineno", 0)
                end = getattr(node, "end_lineno", start)
                if start <= line_number <= end:
                    enclosing_class = node
                    break

    signature = _format_function_signature(enclosing_func) if enclosing_func else None

    # Determine snippet boundaries
    if enclosing_func:
        start_line = getattr(enclosing_func, "lineno", 1)
        end_line = getattr(enclosing_func, "end_lineno", len(lines))
    elif enclosing_class:
        start_line = getattr(enclosing_class, "lineno", 1)
        end_line = getattr(enclosing_class, "end_lineno", len(lines))
    else:
        # Context window around the target line
        start_line = max(1, line_number - 10)
        end_line = min(len(lines), line_number + 10)

    # Bound snippet to max 60 lines to keep prompt compact (acceptance criteria: <= 25,000 tokens)
    if end_line - start_line > 60:
        end_line = min(len(lines), start_line + 60)
        snippet_lines = lines[start_line - 1 : end_line]
        snippet = "\n".join(snippet_lines) + "\n... (truncated)"
    else:
        snippet_lines = lines[start_line - 1 : end_line]
        snippet = "\n".join(snippet_lines)

    return {
        "enclosing_class": enclosing_class.name if enclosing_class else None,
        "enclosing_symbol": enclosing_func.name if enclosing_func else None,
        "signature": signature,
        "imports": imports[:10],  # top 10 imports
        "start_line": start_line,
        "end_line": end_line,
        "snippet": snippet,
        "language": "python",
    }


# ---------------------------------------------------------------------------
# Generic / TS / JS Fallback Extraction
# ---------------------------------------------------------------------------


def extract_generic_symbol_context(
    source_code: str,
    line_number: int,
    symbol_name: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """
    Extract enclosing function/scope block from non-Python or unparseable source files.
    Identifies signatures via regex patterns for TS/JS/Go/etc.
    """
    if not source_code:
        return None

    lines = source_code.splitlines()
    total_lines = len(lines)
    if line_number < 1 or line_number > total_lines:
        line_number = max(1, min(line_number, total_lines))

    # Scan upwards from line_number to detect enclosing function/class definition
    _FUNC_SIG_RE = re.compile(
        r"^\s*(?:(?:export\s+)?(?:async\s+)?function\s+([a-zA-Z0-9_$]+)|(?:const|let|var)\s+([a-zA-Z0-9_$]+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>|(?:public|private|protected|static|async|\s)*([a-zA-Z0-9_$]+)\s*\([^)]*\)\s*(?::\s*[^{]+)?\{)",
    )

    detected_symbol = symbol_name
    signature: Optional[str] = None
    start_line = max(1, line_number - 15)

    for i in range(line_number - 1, max(-1, line_number - 40), -1):
        line_str = lines[i]
        match = _FUNC_SIG_RE.match(line_str)
        if match:
            sig_name = match.group(1) or match.group(2) or match.group(3)
            if not detected_symbol or (sig_name and sig_name in (detected_symbol,)):
                detected_symbol = sig_name or detected_symbol
                signature = line_str.strip()
                start_line = i + 1
                break

    end_line = min(total_lines, start_line + 45)
    snippet_lines = lines[start_line - 1 : end_line]
    snippet = "\n".join(snippet_lines)
    if end_line < total_lines:
        snippet += "\n... (truncated)"

    return {
        "enclosing_class": None,
        "enclosing_symbol": detected_symbol,
        "signature": signature,
        "imports": [],
        "start_line": start_line,
        "end_line": end_line,
        "snippet": snippet,
        "language": "generic",
    }


# ---------------------------------------------------------------------------
# Formatting Helper
# ---------------------------------------------------------------------------


def format_ast_context(frame: StackFrame, context: dict[str, Any]) -> str:
    """
    Format extracted symbol context into clean, compact markdown for injection
    into distilled_context.
    """
    parts: list[str] = []
    symbol_display = frame.symbol_name or context.get("enclosing_symbol") or "unknown"
    header = f"### `{frame.file_path}` (Line {frame.line_number}, in `{symbol_display}`)"
    parts.append(header)

    class_name = context.get("enclosing_class")
    if class_name:
        parts.append(f"**Enclosing Class:** `{class_name}`")

    sig = context.get("signature")
    if sig:
        parts.append(f"**Signature:** `{sig}`")

    imports = context.get("imports")
    if imports:
        import_block = "\n".join(imports)
        parts.append(f"**Module Imports:**\n```python\n{import_block}\n```")

    snippet = context.get("snippet")
    lang = context.get("language", "python")
    if lang == "generic":
        ext = os.path.splitext(frame.file_path)[1].lstrip(".")
        lang = ext if ext in ("ts", "js", "tsx", "jsx", "go", "rs", "java") else "text"

    if snippet:
        parts.append(f"**Enclosing Scope (Lines {context.get('start_line')}-{context.get('end_line')}):**\n```{lang}\n{snippet}\n```")

    return "\n".join(parts)
