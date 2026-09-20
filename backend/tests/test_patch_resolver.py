import pytest
from app.sandbox.mirror import (
    resolve_file_path,
    apply_unified_diff,
    _split_patch_by_file,
)


def test_resolve_file_path_exact():
    paths = ["backend/app/core/analytics.py", "tests/test_analytics.py", "pytest.ini"]
    assert resolve_file_path("backend/app/core/analytics.py", paths) == "backend/app/core/analytics.py"
    assert resolve_file_path("pytest.ini", paths) == "pytest.ini"


def test_resolve_file_path_basename():
    paths = ["backend/app/core/analytics.py", "tests/test_analytics.py", "pytest.ini"]
    # LLM outputs 'analytics.py' -> resolves to full path
    assert resolve_file_path("analytics.py", paths) == "backend/app/core/analytics.py"
    # With a/ or b/ prefix
    assert resolve_file_path("a/analytics.py", paths) == "backend/app/core/analytics.py"
    assert resolve_file_path("b/analytics.py", paths) == "backend/app/core/analytics.py"


def test_resolve_file_path_suffix():
    paths = ["backend/app/core/analytics.py", "frontend/src/core/analytics.ts"]
    assert resolve_file_path("core/analytics.py", paths) == "backend/app/core/analytics.py"
    assert resolve_file_path("core/analytics.ts", paths) == "frontend/src/core/analytics.ts"


def test_resolve_file_path_ambiguous():
    # Two files with the exact same basename
    paths = ["app/utils.py", "tests/utils.py"]
    # Ambiguous basename should not randomly guess; returns target unchanged
    assert resolve_file_path("utils.py", paths) == "utils.py"
    # Qualified suffix resolves correctly
    assert resolve_file_path("tests/utils.py", paths) == "tests/utils.py"


def test_resolve_file_path_not_found():
    paths = ["app/main.py"]
    assert resolve_file_path("unknown.py", paths) == "unknown.py"


def test_apply_unified_diff_single_hunk():
    base = (
        "def add(a, b):\n"
        "    return a - b\n"
        "\n"
        "def sub(a, b):\n"
        "    return a - b\n"
    )
    patch = (
        "--- a/math.py\n"
        "+++ b/math.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b\n"
    )
    result = apply_unified_diff(base, patch)
    expected = (
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def sub(a, b):\n"
        "    return a - b\n"
    )
    assert result == expected


def test_apply_unified_diff_multiple_hunks_with_offset():
    base = "\n".join(f"line_{i}" for i in range(1, 21)) + "\n"
    patch = (
        "--- a/test.txt\n"
        "+++ b/test.txt\n"
        "@@ -3,2 +3,3 @@\n"
        " line_3\n"
        "-line_4\n"
        "+inserted_4a\n"
        "+inserted_4b\n"
        "@@ -10,2 +11,1 @@\n"
        " line_10\n"
        "-line_11\n"
    )
    result = apply_unified_diff(base, patch)
    assert "inserted_4a" in result
    assert "inserted_4b" in result
    assert "line_11" not in result
    assert "line_20" in result


def test_apply_unified_diff_new_file():
    patch = (
        "--- /dev/null\n"
        "+++ b/new_module.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+def hello():\n"
        "+    return 'world'\n"
    )
    result = apply_unified_diff("", patch)
    assert result == "def hello():\n    return 'world'"


def test_split_patch_by_file():
    patch = (
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-x\n"
        "+y\n"
        "--- a/to_delete.py\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        "-bad\n"
        "--- /dev/null\n"
        "+++ b/added.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+new\n"
    )
    files, deleted = _split_patch_by_file(patch)
    assert "foo.py" in files
    assert "added.py" in files
    assert "to_delete.py" in deleted
    assert "-x" in files["foo.py"]
    assert "+y" in files["foo.py"]


def test_apply_unified_diff_tier3_missing_blank_lines():
    base = (
        "def process():\n"
        "    data.append(x)\n"
        "    if len(data) > 100:\n"
        "        data.pop()\n"
        "\n"
        "    total = sum(data)\n"
        "    return total\n"
    )
    # LLM omits the blank line between pop() and total
    patch = (
        "--- a/process.py\n"
        "+++ b/process.py\n"
        "@@ -1,6 +1,6 @@\n"
        " def process():\n"
        "     data.append(x)\n"
        "     if len(data) > 100:\n"
        "-        data.pop()\n"
        "+        data.pop(0)\n"
        "     total = sum(data)\n"
    )
    result = apply_unified_diff(base, patch)
    assert "data.pop(0)" in result
    assert "total = sum(data)" in result
    assert "\n\n    total = sum(data)" in result  # Preserved original blank line

