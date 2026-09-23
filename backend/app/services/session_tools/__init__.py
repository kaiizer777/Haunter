"""
Session tools package for Haunter live pair-programming agent.
"""

from app.services.session_tools.recon import (
    _validate_file_path,
    glob_files,
    grep_search,
    list_directory,
    read_file_slice,
    tool_glob_files,
    tool_grep_search,
    tool_list_directory,
    tool_read_file_slice,
    validate_file_path,
)
from app.services.session_tools.editor import (
    apply_multi_patch,
    create_file,
    delete_file,
    str_replace,
    tool_apply_multi_patch,
    tool_create_file,
    tool_delete_file,
    tool_str_replace,
)
from app.services.session_tools.symbols import (
    find_references,
    find_symbol,
    get_file_outline,
    tool_find_references,
    tool_find_symbol,
    tool_get_file_outline,
)
from app.services.session_tools.sandbox import (
    _sanitize_command,
    tool_run_terminal_command,
    tool_run_linter,
    tool_run_targeted_tests,
)
from app.services.session_tools.web import (
    _validate_external_url,
    tool_search_web_docs,
    tool_fetch_web_content,
    tool_fetch_package_metadata,
)

__all__ = [
    # recon
    "_validate_file_path",
    "validate_file_path",
    "tool_grep_search",
    "grep_search",
    "tool_glob_files",
    "glob_files",
    "tool_read_file_slice",
    "read_file_slice",
    "tool_list_directory",
    "list_directory",
    # editor
    "tool_str_replace",
    "str_replace",
    "tool_create_file",
    "create_file",
    "tool_delete_file",
    "delete_file",
    "tool_apply_multi_patch",
    "apply_multi_patch",
    # symbols (AST & code intelligence)
    "tool_get_file_outline",
    "get_file_outline",
    "tool_find_symbol",
    "find_symbol",
    "tool_find_references",
    "find_references",
    # sandbox execution (Phase 5)
    "_sanitize_command",
    "tool_run_terminal_command",
    "tool_run_linter",
    "tool_run_targeted_tests",
    # web intelligence (Phase 3)
    "_validate_external_url",
    "tool_search_web_docs",
    "tool_fetch_web_content",
    "tool_fetch_package_metadata",
]
