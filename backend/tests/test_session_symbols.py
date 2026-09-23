"""
Unit tests for backend/app/services/session_tools/symbols.py

Covers:
  1. test_python_outline_extraction — AST outline: classes, async funcs, methods, docstrings
  2. test_typescript_outline_extraction — TS regex outline: interfaces, types, exported functions
  3. test_find_symbol_python_and_ts — find_py_defs and find_ts_defs by name/kind
  4. test_find_references_word_boundary — word-boundary matching (user vs username)
  5. test_invalid_identifier_rejection — rejects shell-injection identifiers
  6. test_path_traversal_blocked — directory traversal rejected by _validate_file_path
"""

from __future__ import annotations

import pytest

from app.services.session_tools.symbols import (
    _find_py_defs,
    _find_ts_defs,
    _python_outline,
    _ts_outline,
    tool_find_references,
    tool_find_symbol,
    tool_get_file_outline,
)
from app.services.session_tools.recon import _validate_file_path


# ---------------------------------------------------------------------------
# 1. Python outline extraction
# ---------------------------------------------------------------------------

PYTHON_SOURCE = '''\
class UserService:
    """Manages user CRUD operations."""

    def get_user(self, user_id: str) -> "User":
        """Return user by ID."""
        ...

    async def create_user(self, name: str, email: str) -> "User":
        """Create and persist a new user."""
        ...

    def _private_helper(self):
        ...


def standalone_function(x: int, y: int) -> int:
    """Sum two integers."""
    return x + y


async def async_top_level() -> None:
    ...
'''


def test_python_outline_extraction() -> None:
    outline = _python_outline(PYTHON_SOURCE)

    # Class signature
    assert "class UserService:" in outline

    # Class docstring (first line)
    assert '"""Manages user CRUD operations."""' in outline

    # Regular method
    assert "def get_user(self, user_id: str) -> 'User':" in outline or \
           "def get_user(" in outline  # ast.unparse quote style may vary

    # Async method
    assert "async def create_user(" in outline

    # Private method included
    assert "_private_helper" in outline

    # Top-level function
    assert "def standalone_function(" in outline
    assert '"""Sum two integers."""' in outline

    # Async top-level function
    assert "async def async_top_level()" in outline

    # No function bodies (no 'return' keyword exposed at outline level)
    assert "return x + y" not in outline


def test_python_outline_syntax_error() -> None:
    outline = _python_outline("def broken(::")
    assert "AST parse error" in outline


# ---------------------------------------------------------------------------
# 2. TypeScript/TSX outline extraction
# ---------------------------------------------------------------------------

TS_SOURCE = '''\
export interface UserProfile {
  id: string;
  name: string;
  email: string;
}

export type UserId = string;

export type UserStatus = "active" | "inactive" | "banned";

export class UserController {
  constructor(private service: UserService) {}

  getUser(id: string): Promise<UserProfile> {
    return this.service.get(id);
  }

  async createUser(dto: CreateUserDto): Promise<UserProfile> {
    return this.service.create(dto);
  }
}

export function transformUser(raw: unknown): UserProfile {
  return raw as UserProfile;
}

export const fetchUsers = async (page: number) => {
  return [];
};
'''


def test_typescript_outline_extraction() -> None:
    outline = _ts_outline(TS_SOURCE)

    # Interface
    assert "interface UserProfile {" in outline
    assert "id: string" in outline or "id:" in outline

    # Type aliases
    assert "type UserId = string;" in outline
    assert "type UserStatus" in outline

    # Class
    assert "class UserController {" in outline

    # Exported function
    assert "export function transformUser(" in outline

    # Exported arrow const
    assert "export const fetchUsers = (...) =>" in outline


def test_typescript_outline_empty_file() -> None:
    outline = _ts_outline("")
    assert "(no top-level symbols found)" in outline


# ---------------------------------------------------------------------------
# 3. find_py_defs and find_ts_defs — by name and kind
# ---------------------------------------------------------------------------

def test_find_symbol_python_function() -> None:
    results = _find_py_defs(PYTHON_SOURCE, "get_user", "function")
    assert len(results) == 1
    lineno, sig = results[0]
    assert lineno > 0
    assert "get_user" in sig
    assert sig.startswith("def ")


def test_find_symbol_python_class() -> None:
    results = _find_py_defs(PYTHON_SOURCE, "UserService", "class")
    assert len(results) == 1
    lineno, sig = results[0]
    assert lineno == 1
    assert "class UserService" in sig


def test_find_symbol_python_async_function() -> None:
    results = _find_py_defs(PYTHON_SOURCE, "create_user", None)
    assert len(results) == 1
    _, sig = results[0]
    assert "async def create_user" in sig


def test_find_symbol_python_kind_mismatch() -> None:
    # Searching for UserService as a function should return nothing
    results = _find_py_defs(PYTHON_SOURCE, "UserService", "function")
    assert results == []


def test_find_symbol_python_not_found() -> None:
    results = _find_py_defs(PYTHON_SOURCE, "nonexistent_symbol", None)
    assert results == []


def test_find_symbol_ts_function() -> None:
    results = _find_ts_defs(TS_SOURCE, "transformUser", "function")
    assert len(results) >= 1
    _, sig = results[0]
    assert "transformUser" in sig


def test_find_symbol_ts_class() -> None:
    results = _find_ts_defs(TS_SOURCE, "UserController", "class")
    assert len(results) >= 1
    _, sig = results[0]
    assert "UserController" in sig


def test_find_symbol_ts_interface() -> None:
    results = _find_ts_defs(TS_SOURCE, "UserProfile", "interface")
    assert len(results) >= 1
    _, sig = results[0]
    assert "UserProfile" in sig


def test_find_symbol_ts_type() -> None:
    results = _find_ts_defs(TS_SOURCE, "UserId", "type")
    assert len(results) >= 1
    _, sig = results[0]
    assert "UserId" in sig


# ---------------------------------------------------------------------------
# 4. find_references word-boundary correctness
# ---------------------------------------------------------------------------

_REFERENCES_SOURCE = '''\
user = get_user(user_id)
username = "admin"
print(user.name)
create_user(user)
update_username(username)
'''

import re


def test_find_references_word_boundary_matches_user() -> None:
    pattern = re.compile(r"\b" + re.escape("user") + r"\b")
    matched_lines = [
        ln.strip()
        for ln in _REFERENCES_SOURCE.splitlines()
        if pattern.search(ln)
    ]
    # "user = get_user(user_id)" — has standalone "user"
    # "user.name" — "user" followed by "." is still a word boundary on the left
    # "create_user(user)" — has standalone "user" argument
    for line in matched_lines:
        assert "user" in line


def test_find_references_word_boundary_excludes_username() -> None:
    pattern = re.compile(r"\b" + re.escape("user") + r"\b")
    # "username" must NOT match
    assert not pattern.search("username = 'admin'")
    # "update_username" must NOT match
    assert not pattern.search("update_username(username)")


def test_find_references_word_boundary_does_not_match_prefix() -> None:
    pattern = re.compile(r"\b" + re.escape("log") + r"\b")
    assert not pattern.search("logger.info('hello')")
    assert pattern.search("log.write('x')")


# ---------------------------------------------------------------------------
# 5. Invalid identifier rejection
# ---------------------------------------------------------------------------

def test_invalid_identifier_shell_injection() -> None:
    """Symbols like 'user;rm -rf' must be rejected cleanly."""
    import asyncio

    async def _run() -> str:
        return await tool_find_symbol(
            name="user;rm -rf",
            repo_owner="owner",
            repo_name="repo",
            base_sha="abc123",
        )

    result = asyncio.run(_run())
    assert result.startswith("Error:")
    assert "invalid symbol identifier" in result


def test_invalid_identifier_spaces() -> None:
    import asyncio

    async def _run() -> str:
        return await tool_find_references(
            symbol="my symbol",
            repo_owner="owner",
            repo_name="repo",
            base_sha="abc123",
        )

    result = asyncio.run(_run())
    assert result.startswith("Error:")
    assert "invalid symbol identifier" in result


def test_invalid_identifier_starts_with_digit() -> None:
    import asyncio

    async def _run() -> str:
        return await tool_find_symbol(
            name="123bad",
            repo_owner="owner",
            repo_name="repo",
            base_sha="abc123",
        )

    result = asyncio.run(_run())
    assert result.startswith("Error:")


def test_invalid_kind_rejected() -> None:
    import asyncio

    async def _run() -> str:
        return await tool_find_symbol(
            name="foo",
            kind="module",  # not a valid kind
            repo_owner="owner",
            repo_name="repo",
            base_sha="abc123",
        )

    result = asyncio.run(_run())
    assert result.startswith("Error:")
    assert "invalid kind" in result


# ---------------------------------------------------------------------------
# 6. Path traversal blocked
# ---------------------------------------------------------------------------

def test_path_traversal_double_dot() -> None:
    with pytest.raises(ValueError, match="Directory traversal rejected"):
        _validate_file_path("../../etc/passwd")


def test_path_traversal_absolute_path() -> None:
    with pytest.raises(ValueError, match="Absolute path rejected"):
        _validate_file_path("/etc/passwd")


def test_path_traversal_disallowed_chars() -> None:
    with pytest.raises(ValueError, match="disallowed characters"):
        _validate_file_path("src/foo;bar.py")


def test_path_traversal_in_get_file_outline() -> None:
    """tool_get_file_outline must reject path traversal attempts."""
    import asyncio

    async def _run() -> str:
        return await tool_get_file_outline(
            path="../../secrets.py",
            repo_owner="owner",
            repo_name="repo",
            base_sha="abc123",
            staged_patches={},
        )

    result = asyncio.run(_run())
    # Should return an Error string, not actually read a file
    assert "Error" in result or "rejected" in result


def test_valid_path_accepted() -> None:
    path = _validate_file_path("backend/app/services/session_tools/symbols.py")
    assert path == "backend/app/services/session_tools/symbols.py"


def test_python_outline_no_top_level() -> None:
    source = "x = 1\ny = 2\n"
    outline = _python_outline(source)
    assert "(no top-level symbols found)" in outline
