"""
Tamper / security validation for SandboxInput (Phase 13).
Run with: python scripts/test_sandbox_input.py
"""
import sys
import uuid
from pydantic import ValidationError

sys.path.insert(0, ".")
from app.sandbox.runner import SandboxInput  # noqa: E402

VALID_PATCH = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-x\n+y"
RID = uuid.uuid4()

results: list[tuple[str, bool]] = []


def expect_ok(label: str, **kwargs):
    try:
        SandboxInput(**kwargs, run_id=RID)
        results.append((label, True))
        print(f"PASS  {label}")
    except Exception as e:
        results.append((label, False))
        print(f"FAIL  {label}: {e}")


def expect_reject(label: str, **kwargs):
    try:
        SandboxInput(**kwargs, run_id=RID)
        results.append((label, False))
        print(f"FAIL  {label}: should have been rejected but was not")
    except (ValidationError, ValueError) as e:
        results.append((label, True))
        print(f"PASS  {label}")


# ---- Valid inputs ----
expect_ok("valid standard", patch=VALID_PATCH, repo_ref="owner/repo")
expect_ok("valid with sha", patch=VALID_PATCH, repo_ref="owner/repo@abc123def456")
expect_ok("valid dots in name", patch=VALID_PATCH, repo_ref="my.org/my.repo")

# ---- path traversal ----
expect_reject("traversal ../", patch="x", repo_ref="../etc/passwd")
expect_reject("traversal .git/", patch="x", repo_ref="owner/.git/config")
expect_reject("traversal .github/", patch="x", repo_ref="owner/.github/workflows")
expect_reject("double slash", patch="x", repo_ref="owner//repo")

# ---- oversized ----
expect_reject("oversized patch", patch="x" * (512 * 1024 + 1), repo_ref="owner/repo")
expect_reject("oversized repo_ref", patch="x", repo_ref="a" * 201)

# ---- shell metacharacters ----
expect_reject("shell $ in ref", patch="x", repo_ref="owner/repo$(id)")
expect_reject("semicolon in ref", patch="x", repo_ref="owner;malicious")
expect_reject("backtick in ref", patch="x", repo_ref="owner/`id`")
expect_reject("pipe in ref", patch="x", repo_ref="owner/repo|cmd")
expect_reject("space in ref", patch="x", repo_ref="owner/repo name")

# ---- empty patch ----
expect_reject("empty patch", patch="", repo_ref="owner/repo")
expect_reject("blank patch", patch="   ", repo_ref="owner/repo")

# ---- Summary ----
passed = sum(1 for _, ok in results if ok)
total = len(results)
print(f"\n{passed}/{total} checks passed")
if passed < total:
    sys.exit(1)
