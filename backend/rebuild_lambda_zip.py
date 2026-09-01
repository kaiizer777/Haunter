"""
Rebuild the Lambda deployment zip at the repo root from the current backend/ tree.

Steps:
  1. Create a staging dir.
  2. pip install the RUNTIME dependencies from requirements.txt into the
     staging dir using manylinux2014_x86_64 wheels so the bundle works on
     Lambda's Linux runtime. (Test-only deps — respx, freezegun,
     pytest-asyncio — are excluded.)
  3. Copy the app/ tree (and any other runtime code) on top.
  4. Zip the staging dir into lambda.zip with deflate compression.
  5. Clean up the staging dir.

This is the same shape as the standard AWS Lambda Python packaging guide.
The previous bare ``zip -r backend/`` did not include deps and would
crash with ``ModuleNotFoundError: No module named 'mangum'`` on the
first request.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
REPO_ROOT = BACKEND_DIR.parent
TARGET = REPO_ROOT / "lambda.zip"
REQUIREMENTS = BACKEND_DIR / "requirements.txt"

# Test-only deps — must NOT be in the production bundle.
EXCLUDE_REQUIREMENTS = {
    "respx",
    "freezegun",
    "pytest-asyncio",
    "pytest",
    "pytest-anyio",
}

# Excluded paths inside the staging dir (relative to staging root).
EXCLUDE_DIR_NAMES = {".venv", "__pycache__", "tests", ".pytest_cache", "node_modules", "scripts", "build", "bin", "lib", "include", "share"}
EXCLUDE_FILE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_FILE_NAMES = {"rebuild_lambda_zip.py"}


def read_runtime_requirements() -> list[str]:
    """Return the list of requirements WITHOUT test-only packages.

    Strips inline ``# ...`` comments (PEP 508 doesn't allow them on
    requirement lines) before passing each line to pip.
    """
    runtime: list[str] = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        # Drop inline comments (not allowed in requirement specifiers).
        line_no_comment = line.split("#", 1)[0].strip()
        if not line_no_comment:
            continue
        # Extract package name (before any version specifier or extras)
        pkg = re.split(r"[\[<>=!~; ]", line_no_comment, maxsplit=1)[0].strip().lower()
        if pkg in EXCLUDE_REQUIREMENTS:
            continue
        runtime.append(line_no_comment)
    return runtime


def main() -> int:
    if not REQUIREMENTS.is_file():
        print(f"requirements.txt not found at {REQUIREMENTS}", file=sys.stderr)
        return 1

    runtime_reqs = read_runtime_requirements()
    print(f"Installing {len(runtime_reqs)} runtime packages (excluding {sorted(EXCLUDE_REQUIREMENTS)}):")
    for r in runtime_reqs:
        print(f"  - {r}")

    with tempfile.TemporaryDirectory(prefix="haunter-lambda-build-") as staging:
        staging_path = Path(staging)

        # 1. pip install with manylinux wheels so the bundle runs on
        #    Lambda's Linux x86_64 Python 3.11 runtime.
        cmd = [
            sys.executable, "-m", "pip", "install",
            "--target", str(staging_path),
            "--platform", "manylinux2014_x86_64",
            "--only-binary=:all:",
            "--python-version", "3.11",
            "--upgrade",
            "--no-cache-dir",
            *runtime_reqs,
        ]
        print("Running:", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("pip install failed:", file=sys.stderr)
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return result.returncode

        # 2. Copy app code on top of the deps.
        for src in BACKEND_DIR.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(BACKEND_DIR)
            if should_exclude(rel):
                continue
            dst = staging_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        # 3. Verify mangum is present (the one the function imports on every request).
        if not (staging_path / "mangum").exists():
            print("ERROR: mangum not in staging dir after pip install", file=sys.stderr)
            return 2

        # 4. Zip the staging dir.
        tmp_target = TARGET.with_suffix(".zip.tmp")
        if tmp_target.exists():
            tmp_target.unlink()
        count = 0
        with zipfile.ZipFile(tmp_target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in sorted(staging_path.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(staging_path)
                if should_exclude(rel):
                    continue
                zf.write(path, rel.as_posix())
                count += 1
        os.replace(tmp_target, TARGET)

    size_mb = TARGET.stat().st_size / (1024 * 1024)
    print(f"Wrote {TARGET} ({count} files, {size_mb:.2f} MB)")
    return 0


def should_exclude(rel_path: Path) -> bool:
    parts = set(rel_path.parts)
    if parts & EXCLUDE_DIR_NAMES:
        return True
    if rel_path.name in EXCLUDE_FILE_NAMES:
        return True
    if rel_path.suffix in EXCLUDE_FILE_SUFFIXES:
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
