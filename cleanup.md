# Haunter — Codebase Cleanup Plan

**Date:** 2026-09-01
**Status:** Drafting (not yet started)
**Goal:** Reduce Haunter to a clean, single-provider, single-hosting system. Remove all dormant GCP and AWS CodeBuild code paths. Drop dead dependencies. Update docs to match the actual deployed state.
**Method:** Five sessions, each one independently shippable. Within each session, sub-phases are the order of execution. Stop and run the session's acceptance test before ticking its box. If it fails, the session is not done.

> **How to use this file.** Same as `fix.md`: outcome → scope → checkboxes → acceptance. One session per sitting. Commit at the end of each session. Don't bundle sessions.
>
> **Line numbers in this plan come from the 2nd agent's pre-Phase-1 audit.** Phase 1 added ~50 lines to `backend/app/config.py` (the `max_attempts` field + new constants) and to `backend/app/sandbox/__init__.py` (the new exception types). So any line number in this plan referencing `config.py`, `__init__.py`, or other files Phase 1 touched may be off by ±50. **Find locations by string search (`grep "sandbox_provider" backend/app/config.py` etc.) instead of trusting the line numbers literally.**

---

## Background — what we know

- **Active sandbox:** `SANDBOX_PROVIDER=github_actions` (`backend/app/config.py:106`). Reason: AWS CodeBuild account-level concurrent-build quota = 0 in `us-east-1`.
- **Active hosting:** AWS Lambda Function URL (`backend/app/config.py:133` and `infra/aws/lambda.tf`). Reason: GCP account not provisioned.
- **Dead code paths to remove:**
  - GCP Cloud Build sandbox runner + buildspec generator + `google-cloud-build` dependency.
  - AWS CodeBuild sandbox runner + `codebuild.tf` + CodeBuild IAM permissions in `lambda.tf`.
  - GCP hosting adapter (Cloud Run `BackgroundTasks` path — never deployed).
  - AWS hosting adapter stays (it's the active one).
- **Critical gotcha:** `backend/app/sandbox/github_actions_runner.py:69` imports `_sanitize_failure_reason` directly from `backend/app/sandbox/verifier.py:67`. If we delete `verifier.py` blindly, the GitHub Actions runner dies. **Move the function first, delete the file second.**
- **Dependency win:** `google-cloud-build==3.*` in `requirements.txt:11` pulls `google-auth`, `google-api-core`, `protobuf`, `grpcio` into `lambda.zip` (~30+ MB of dead code in the bundle). Removing it shrinks deploy time from ~4 min to ~1 min.
- **`boto3` stays.** Used by the AWS Lambda hosting adapter (async self-invocation via `boto3.client("lambda")` in `app/adapters/hosting.py:238`) AND by the GitHub Actions sandbox (App PEM fetched from SSM via `boto3.client("ssm")` in `app/sandbox/github_actions_runner.py:153`).

---

## Session 0 — Baseline + dry-run audit (do this first, ~30 min)

**Outcome.** You have a known-good starting point, a baseline of "what currently passes," and a complete file-by-file deletion plan. Nothing is committed yet.

**Scope.** Read-only work plus one stash/branch.

1. **Pre-flight.** Confirm the working tree is at `bc243c1` (Phases 1-3) plus the agent's uncommitted Phase 4 + Phase 6 work. Run:
   ```bash
   cd C:\Users\bari2\Desktop\Haunter
   git status --short
   git log --oneline -5
   ```
   Expected untracked test files (no need to stop if these are the only untracked items):
   - `backend/tests/test_orchestrator_session.py` (Phase 1)
   - `backend/tests/test_fix_generator_paths.py` (Phase 2)
   - `backend/tests/test_sandbox_mirror.py` (Phase 2)
   - `backend/tests/test_fix_generator_deterministic.py` (Phase 3)
   - `backend/tests/test_seed_user_tree.py` (Phase 4)
   - `backend/tests/demo_canonical/` directory (Phase 6, contains `__init__.py` and `test_demo_canonical.py`)

   Also expected modified files (the 22 pre-existing uncommitted changes from before this session started — see `git status`). If anything other than these is modified or untracked, STOP and figure out what changed before proceeding.

2. **Create the cleanup branch.** Don't work on `main` for this.
   ```bash
   git checkout -b cleanup/dead-code-removal
   ```

3. **Run the full test suite once for a baseline.**
   ```bash
   cd backend
   python -m pytest -q
   ```
   Record the output. This is the "passing baseline" — every subsequent session must end with at least this passing (DB tests will skip; that's expected).

4. **Inventory the deletions.** Use this checklist as you go through each session. The "owner" column is the session that deletes it.

   | # | File | Lines (approx) | Owner | Reason |
   |---|------|----------------|-------|--------|
   | 1 | `backend/app/sandbox/verifier.py` | 465 | Session 1 | GCP Cloud Build — dormant |
   | 2 | `backend/app/sandbox/build_config.py` | 215 | Session 1 | GCP Cloud Build buildspec — dormant |
   | 3 | `backend/app/sandbox/aws_runner.py` | 434 | Session 2 | AWS CodeBuild — quota blocker |
   | 4 | `backend/app/adapters/hosting.py` (`GCPHostingAdapter` class only) | ~80 | Session 3 | GCP Cloud Run — never deployed |
   | 5 | `infra/aws/codebuild.tf` | 224 | Session 2 | Dormant sandbox infra |
   | 6 | `backend/tests/test_sandbox_verifier.py` | 411 | Session 1 | Tests dead code |
   | 7 | `backend/tests/test_hosting.py` (GCP cases) | partial | Session 3 | Tests dead code |
   | 8 | `requirements.txt` (google-cloud-build line) | 1 | Session 1 | Dead dependency |
   | 9 | `app/sandbox/__init__.py` (gcp + aws branches) | ~90 | Sessions 1+2 | Dead dispatch |
   | 10 | `app/config.py` (default flips) | 2 | Session 4 | Defaults should match active provider |
   | 11 | `app/schemas.py` (`gcp` Literal entries) | 2 | Session 4 | Dead literal |
   | 12 | `app/routers/hosting_config.py` (allowlist) | 3 | Session 3 | Allowlist mismatch |
   | 13 | `infra/aws/lambda.tf` (CodeBuild IAM perms) | 10 | Session 2 | Dead permissions |

   Total: ~1,940 lines deleted across 13 files. Plus 1 dependency dropped from `requirements.txt`. Plus 2 doc files updated (Session 5).

5. **Confirm no shared module breaks the import chain.** For each file in row 1, 3, 4 above, run:
   ```bash
   python -c "import app.sandbox.<modname>"
   ```
   in `backend/`. It should currently succeed. After Session 0, do NOT delete any file. Just confirm the import works today.

**Acceptance.**
- [ ] On branch `cleanup/dead-code-removal`.
- [ ] Baseline `pytest -q` output saved.
- [ ] Inventory table above checked against actual file sizes.
- [ ] All 3 "must-stay" modules (`lambda_handler.py`, `app/adapters/hosting.py` AWS class, `app/sandbox/github_actions_runner.py`) verified to import cleanly.

---

## Session 1 — Remove GCP sandbox (verifier.py + build_config.py + google-cloud-build)

**Outcome.** The GCP Cloud Build sandbox is gone. `google-cloud-build` is dropped from `requirements.txt`. The `_sanitize_failure_reason` function has been moved to `app/sandbox/runner.py` so the GitHub Actions sandbox still works. `lambda.zip` is meaningfully smaller (verify with `rebuild_lambda_zip.py` dry-run if you have a build env).

**Why first.** This is the biggest single deletion (~680 lines) and the only one with a cross-module import hazard. Doing it first gives us the cleanest base for Sessions 2-4 and proves the "move then delete" pattern works before we touch the AWS CodeBuild code.

**Scope.** 4 source files modified, 1 file deleted, 1 test file deleted, 1 dependency dropped.

1. **Move `_sanitize_failure_reason` from `verifier.py` to `runner.py`.**
   - **Pre-check (do this first):** `grep -n "from app.sandbox.runner\|import app.sandbox.runner" backend/app/sandbox/verifier.py` — must return zero hits. If `verifier.py` imports from `runner.py`, you have a circular import risk; choose a different home (e.g. a new `app/sandbox/_sanitize.py` module).
   - Read `backend/app/sandbox/verifier.py:67` to see the current implementation.
   - Read `backend/app/sandbox/runner.py` to find the right home (probably near `make_result` or `SandboxInput`/`SandboxResult`).
   - Add the function to `runner.py` with the same signature and behavior. Keep the docstring verbatim.
   - Find every importer of `_sanitize_failure_reason` and update it:
     ```bash
     grep -rn "_sanitize_failure_reason" backend/
     ```
     For each match, change `from app.sandbox.verifier import _sanitize_failure_reason` to `from app.sandbox.runner import _sanitize_failure_reason`. The known one is `backend/app/sandbox/github_actions_runner.py:69`, but check the grep for any others.
   - Run `python -c "from app.sandbox.github_actions_runner import _resolve_user_github_id"` to confirm the import still works.
   - Do NOT add a re-export in `verifier.py`. We delete the file in step 3 — the re-export would be dead code in the brief window it lives.

2. **Delete the GCP dispatch branch in `app/sandbox/__init__.py`.**
   - Remove lines around 189-195 (the `if provider == "gcp"` block) and 142-144 (the GCP registry entry construction).
   - Remove the `"gcp": "app.sandbox.verifier.GCPSandboxRunner"` entry from `SANDBOX_PROVIDERS`.
   - Update the `verify()` docstring to drop GCP references.
   - **Keep** the `gcp` value in `provider` validation for now (Session 4 will narrow the Literal). The point of this session is to delete the GCP sandbox code, not to forbid the string entirely.

3. **Delete `backend/app/sandbox/verifier.py`.**
   - Confirm no other module imports from it: `grep -rn "from app.sandbox.verifier\|app.sandbox.verifier" backend/` — should now only return zero hits.
   - `rm backend/app/sandbox/verifier.py`

4. **Delete `backend/app/sandbox/build_config.py`.**
   - Same: `grep -rn "from app.sandbox.build_config\|app.sandbox.build_config" backend/` → zero hits.
   - `rm backend/app/sandbox/build_config.py`

5. **Delete `backend/tests/test_sandbox_verifier.py`.**
   - The whole file is GCP Cloud Build mocks (`build_cloud_build_config`, `verify_patch`). Dead.
   - `rm backend/tests/test_sandbox_verifier.py`

6. **Drop `google-cloud-build==3.*` from `backend/requirements.txt`.**
   - This is the highest-leverage single-line change in the entire cleanup (~30+ MB out of `lambda.zip`).
   - Keep `boto3` (still used by AWS Lambda + SSM).
   - Keep `google-auth` if anything else uses it — verify with `pip show google-auth` after editing (a fresh venv install shows what's actually transitively required).

7. **Run the full test suite.**
   - `cd backend && python -m pytest -q`
   - Pre-cleanup baseline (from Session 0): 149 passed, 107 skipped. After deleting `test_sandbox_verifier.py`, expect the passed count to drop by however many tests that file contained (estimate ~10-20 based on its 411-line size — the exact number is a soft check). The skipped count should be unchanged (DB tests).
   - **The 0 failures is the real check.** If something that used to pass now fails, you broke a non-obvious import.

8. **Run the collection check.**
   - `pytest --co -q | tail -3` — should report a number slightly less than 256 (we deleted one test file's worth).

9. **Commit.**
   ```bash
   git add backend/app/sandbox/runner.py \
           backend/app/sandbox/github_actions_runner.py \
           backend/app/sandbox/__init__.py \
           backend/requirements.txt
   git rm backend/app/sandbox/verifier.py \
          backend/app/sandbox/build_config.py \
          backend/tests/test_sandbox_verifier.py
   git commit -m "cleanup(session 1): drop GCP Cloud Build sandbox, move _sanitize_failure_reason to runner.py, drop google-cloud-build dep"
   ```

**Acceptance.**
- [ ] `git diff --stat HEAD~1` shows the deletions (verifier.py, build_config.py, test_sandbox_verifier.py) and no accidental changes to Phase 1-6 files.
- [ ] `pytest tests/ -q` shows 0 failures.
- [ ] `grep -rn "verifier\|build_config" backend/app/sandbox/` returns no dead-code references (only the new `runner.py` import if any).
- [ ] `python -c "from app.sandbox.github_actions_runner import _resolve_user_github_id"` succeeds.
- [ ] `python -c "from app.sandbox import verify"` succeeds.
- [ ] If you can build the lambda zip locally: `cd backend && python rebuild_lambda_zip.py` runs without error and the output zip is smaller than before (size diff is a soft check, just confirm no build error).

---

## Session 2 — Remove AWS CodeBuild sandbox (aws_runner.py + codebuild.tf + IAM perms)

**Outcome.** The AWS CodeBuild sandbox runner is gone. `infra/aws/codebuild.tf` is deleted. The CodeBuild IAM permissions in `lambda.tf` are dropped. The `aws` provider is removed from `app/sandbox/__init__.py`'s `SANDBOX_PROVIDERS` and the `aws` dispatch branch.

**Why second.** Same pattern as Session 1 but smaller (~660 lines). After this, `app/sandbox/` has only the GitHub Actions runner + the shared `runner.py` + the dispatch in `__init__.py`. The cleanup feels done at the sandbox layer.

**Scope.** 3 source files modified, 2 files deleted, 1 infra file modified.

1. **Delete the `aws` dispatch branch in `app/sandbox/__init__.py`.**
   - Remove the `aws` entry in `SANDBOX_PROVIDERS` (line ~54).
   - Remove the `if provider == "aws"` block in `verify()` (lines 199-231).
   - Keep the AWS-related config fields in `app/config.py` for now (`aws_codebuild_project_name`, `aws_region`) — Session 4 will clean them up. Doing it now risks scope creep.

2. **Delete `backend/app/sandbox/aws_runner.py`.**
   - `grep -rn "from app.sandbox.aws_runner\|app.sandbox.aws_runner" backend/` → must be zero hits after step 1.
   - `rm backend/app/sandbox/aws_runner.py`

3. **Delete `backend/scratch/unit_test_codebuild_fix.py` and `backend/scratch/unit_test_aws_quota_guard.py`** if they exist.
   - These are throwaway scripts from when CodeBuild was being debugged. Pure dead weight.

4. **Delete `infra/aws/codebuild.tf`.**
   - `grep -rn "aws_codebuild\|haunter_sandbox\|haunter-codebuild" infra/` after the next step should return only the IAM permissions you're about to remove.

5. **Remove the CodeBuild IAM permissions from `infra/aws/lambda.tf`.**
   - Lines ~100-109: the `codebuild:StartBuild` and `codebuild:BatchGetBuilds` statements.
   - Also remove the `AWS_CODEBUILD_PROJECT_NAME` env var on line ~202 if it's still there.
   - Read the file before editing — the exact line numbers may have shifted since the audit.

6. **Run the full test suite.**
   - `cd backend && python -m pytest -q`
   - **0 failures expected.** Skipped count should be similar to Session 1 (we didn't touch any test files in this session).

7. **Run `terraform validate`** if you have it available.
   - `cd infra/aws && terraform validate`
   - Confirms the .tf changes are syntactically valid even if you can't plan/apply without AWS creds.

8. **Commit.**
   ```bash
   git add backend/app/sandbox/__init__.py
   git rm backend/app/sandbox/aws_runner.py
   git rm backend/scratch/unit_test_codebuild_fix.py backend/scratch/unit_test_aws_quota_guard.py
   git rm infra/aws/codebuild.tf
   git add infra/aws/lambda.tf
   git commit -m "cleanup(session 2): drop AWS CodeBuild sandbox runner + codebuild.tf + Lambda CodeBuild IAM"
   ```

**Acceptance.**
- [ ] `git diff --stat HEAD~1` shows the deletions and the IAM-perm removal in `lambda.tf` (small, surgical).
- [ ] `pytest tests/ -q` shows 0 failures.
- [ ] `terraform validate` passes (or document why you can't run it).
- [ ] `grep -rn "codebuild\|AWSSandboxRunner" backend/ infra/` returns only legitimate references (e.g. comments about why we moved away, the new docs in `cleanup.md` itself).

---

## Session 3 — Remove GCP hosting adapter (`GCPHostingAdapter` class only)

**Outcome.** The GCP Cloud Run hosting adapter is gone. The `aws` provider is now the only option. `app/routers/hosting_config.py` allows `"aws"` only.

**Why third.** This is the smallest session (~120 lines + a few small touch-ups). Saves it for last among the "delete a provider" sessions so we can validate the dispatch pattern works with just one provider before we flip the default.

**Scope.** 2 source files modified, 1 test file modified.

1. **Read `backend/app/adapters/hosting.py` in full.** This is the largest of the cleanup files. Understand:
   - `_ALLOWED_PROVIDERS = frozenset({"gcp", "aws"})` (line ~50) — change to `frozenset({"aws"})`.
   - `GCPHostingAdapter` class (lines ~140-157) — delete the class.
   - `get_hosting_adapter()` (line ~266) — currently falls back to `GCPHostingAdapter()`. Change the fallback to `AWSHostingAdapter()`.
   - The dispatch logic (search for `if provider == "gcp"`).

2. **Delete `GCPHostingAdapter` from `app/adapters/hosting.py`.**
   - Remove the class definition.
   - Remove any helper functions that only `GCPHostingAdapter` used (e.g. Cloud Run background-task helpers).
   - Update imports at the top of the file to drop the now-unused imports.

3. **Update `_ALLOWED_PROVIDERS` to `frozenset({"aws"})`.**

4. **Update `get_hosting_adapter()`'s fallback to `AWSHostingAdapter()`.**

5. **Update `backend/app/routers/hosting_config.py:104-106`.**
   - The current `allowed_values = {"gcp", "aws"}` allowlist is in a function that validates incoming config-update requests.
   - Change to `allowed_values = {"aws"}`.

6. **Update `backend/tests/test_hosting.py`.**
   - Remove all GCP test cases (search for `gcp`, `GCP`, `GCPHostingAdapter`).
   - Keep the AWS test cases.
   - If the AWS test coverage was thin, the deletion might leave the file very short — that's fine. Don't add new AWS tests in this session; that's scope creep.

7. **Run the full test suite.**
   - `cd backend && python -m pytest -q`
   - **0 failures expected.** The remaining AWS tests should still pass.

8. **Commit.**
   ```bash
   git add backend/app/adapters/hosting.py \
           backend/app/routers/hosting_config.py \
           backend/tests/test_hosting.py
   git commit -m "cleanup(session 3): drop GCPHostingAdapter, narrow _ALLOWED_PROVIDERS to {aws}"
   ```

**Acceptance.**
- [ ] `git diff --stat HEAD~1` shows small, surgical changes.
- [ ] `pytest tests/ -q` shows 0 failures.
- [ ] `grep -rn "GCPHostingAdapter\|gcp" backend/app/ backend/tests/` returns no hits except legitimate doc comments.
- [ ] `python -c "from app.adapters.hosting import get_hosting_adapter; print(type(get_hosting_adapter('aws')).__name__)"` prints `AWSHostingAdapter`.

---

## Session 4 — Flip defaults + narrow Literals + drop config fields

**Outcome.** `app/config.py` defaults match the active stack: `sandbox_provider: str = "github_actions"`, `hosting_provider: str = "aws"`. The `gcp` and `aws` Literals in `app/schemas.py` are narrowed. Stale `aws_codebuild_project_name` and `gcp_project_id` config fields are dropped (or kept — see step 2).

**Why fourth.** Defaults must be flipped AFTER all the code that referenced the old defaults is gone. Otherwise the suite breaks in confusing ways during Sessions 1-3.

**Scope.** 2 source files modified.

1. **Read `backend/app/config.py:101-145` (the provider-related section).** Verify which fields are still referenced after Sessions 1-3:
   - `sandbox_provider` (line 107) — referenced in `app/sandbox/__init__.py:136` (verify). After Session 2 the only valid value is `github_actions`.
   - `hosting_provider` (line 134) — referenced in `app/adapters/hosting.py` (verify). After Session 3 the only valid value is `aws`.
   - `aws_codebuild_project_name` (line 112) — was for the deleted CodeBuild runner.
   - `aws_region` (line 113) — was for CodeBuild. Check if `AWSHostingAdapter` or any other active code uses it. If not, drop.
   - `gcp_project_id` (line 92) — was for the deleted Cloud Build + Cloud Run. Drop.

2. **Decide on `aws_codebuild_project_name` / `aws_region` / `gcp_project_id` retention.** Three options:
   - **Drop all three.** Cleanest. If you ever flip back to AWS CodeBuild, you'll add the fields back. Low risk.
   - **Keep `aws_region`, drop the other two.** `aws_region` is a generic AWS setting that future code might want; the others were sandbox-specific.
   - **Keep all three as `Optional[str] = None`.** Most defensive. Future-proofs for hypothetical provider re-introduction. Matches the current "all optional" pattern.

   **Recommendation: drop `aws_codebuild_project_name` and `gcp_project_id`. Keep `aws_region` as `Optional[str]`** (it's used by `AWSHostingAdapter` for the Lambda region — verify, then keep).

3. **Update `backend/app/config.py:107`.** Change `sandbox_provider: str = "gcp"` to `sandbox_provider: str = "github_actions"`.

4. **Update `backend/app/config.py:134`.** Change `hosting_provider: str = "gcp"` to `hosting_provider: str = "aws"`.

5. **Update `backend/app/schemas.py:67-68`.**
   - `AllowedHostingProvider = Literal["gcp", "aws"]` → `Literal["aws"]`.
   - `AllowedSandboxProvider = Literal["gcp", "aws", "github_actions"]` → `Literal["github_actions"]`.

6. **Update `backend/main.py:82`.** The `getattr(settings, "sandbox_provider", "gcp")` fallback should match the new default. Change to `"github_actions"`.

7. **Update `backend/.env.example` if it exists.** Make sure the example env file shows the active defaults. (Check with `Test-Path backend/.env.example` in PowerShell; if it doesn't exist, skip this step.)

8. **Run the full test suite.**
   - `cd backend && python -m pytest -q`
   - **0 failures expected.** Some tests may have asserted on the old defaults — fix them to assert on the new defaults.

9. **Commit.**
   ```bash
   git add backend/app/config.py \
           backend/app/schemas.py \
           backend/main.py
   # Conditionally add .env.example if it exists:
   if (Test-Path backend/.env.example) { git add backend/.env.example }
   git commit -m "cleanup(session 4): flip config defaults to active providers, narrow Literals to {aws, github_actions}"
   ```

**Acceptance.**
- [ ] `git diff --stat HEAD~1` shows only the 2-3 small file changes.
- [ ] `pytest tests/ -q` shows 0 failures.
- [ ] `python -c "from app.config import settings; print(settings.sandbox_provider, settings.hosting_provider)"` prints `github_actions aws`.
- [ ] `grep -rn "gcp" backend/app/ backend/tests/ | grep -v "\.md:"` returns no production code references (only doc comments if any).

---

## Session 5 — Doc updates (README.md + HAUNTER.md) + the final lambda.zip smoke

**Outcome.** `README.md` and `HAUNTER.md` reflect the post-cleanup reality. No mention of GCP Cloud Build, no mention of AWS CodeBuild, no mention of `SANDBOX_PROVIDERS["gcp"]` or `["aws"]`. The README's "Known limitations & Caveats" section is updated. HAUNTER.md's "How the sandbox works" section is rewritten. Both files stay at roughly their current size (README ≤ ~140 lines, HAUNTER.md can grow).

**Why fifth.** Docs should be the last to change. Otherwise we'd be documenting behavior we haven't shipped yet, and then the doc-agent would have to revise when the code changes.

**Scope.** 2 doc files modified, optional lambda.zip rebuild.

### 5a. `README.md` (target: ~131 lines, current is 131)

The current README is mostly correct but has stale references. Updates:

- **Line 13 (Sandbox verification feature bullet):** Drop "(AWS CodeBuild & GCP dormant)" — only `github_actions` is real now.
- **Line 24 (Tech Stack table, Sandbox row):** Change `GitHub Actions CI (github_actions_runner) via per-user test mirrors (AWS CodeBuild & GCP dormant)` to just `GitHub Actions CI (github_actions_runner) via per-user test mirrors`.
- **Line 30-38 ("How the sandbox works" section):** Update intro. Current intro says "Haunter previously executed sandboxes via AWS CodeBuild ... migrated to GitHub Actions CI". After cleanup, AWS CodeBuild is gone entirely, not "dormant." Rewrite to:
  > Haunter executes sandboxes via **GitHub Actions CI** (`GitHubActionsSandboxRunner`) using polled test mirrors (documented in `github.md`).
  Then keep the 5-step list as-is (it accurately describes the GitHub Actions flow).
- **Line 42 (Sandbox location caveat):** Keep the caveat as-is. The text "Hosted on the `kaiizer777` personal GitHub namespace rather than the originally-planned `haunter-sandboxes` org, because the GitHub App could not be installed cleanly on the org without granting excessive permissions." is still accurate post-cleanup.
- **Line 43 (Dormant infra caveat):** DELETE this entire caveat. After cleanup there is no dormant infra. The caveat was: *"AWS Lambda orchestrator infra is fully active. The previously deployed AWS CodeBuild sandbox (`infra/aws/codebuild.tf`) and GCP Cloud Build remain dormant fallbacks in `SANDBOX_PROVIDERS`; GitHub Actions is the active runner (`SANDBOX_PROVIDER=github_actions`)."* All three providers listed here are now removed or narrowed.
- **Line 56 (Project Structure, `infra/aws/`):** Change `└── infra/aws/      # Terraform: Lambda Function URL + CodeBuild (dormant)` to `└── infra/aws/      # Terraform: Lambda Function URL`.
- **Line 80 (Deployment bullet):** Keep — this is about Lambda hosting, not sandbox. Still accurate.
- **Add a new section after the "Deployment" section:** "## Cleanup history" with a one-line reference: "See `cleanup.md` for the dead-code removal history (GCP Cloud Build + AWS CodeBuild + GCP hosting adapter, ~1,940 lines across 13 files, plus `google-cloud-build` dependency dropped)."

**Target size:** ~125 lines. Current: 131. Should be slightly smaller after deleting the dormant-infra caveat.

### 5b. `HAUNTER.md` (target: can grow, current is 211)

The current HAUNTER.md is the product/arch spec. After cleanup, expand it. Suggested additions:

- **New section: "Sandbox providers"** — short paragraph explaining that Haunter ONLY supports GitHub Actions CI. The "we used to support GCP and AWS CodeBuild" history belongs in a footnote or `cleanup.md` reference, not the main spec. The active spec should describe the active stack only.
- **New section: "Hosting providers"** — short paragraph explaining that Haunter ONLY supports AWS Lambda (Function URL, async self-invocation). Drop any Cloud Run references.
- **"## Build phases" section:** Add a new "Phase 17 (Cleanup)" entry summarizing what was removed. Reference `cleanup.md` for the full plan.
- **Cross-references to update:** Search the doc for "GCP", "Cloud Build", "CodeBuild", "gcr.io", "google-cloud-build". Update or remove each.

**Target size:** ~230-260 lines. Current: 211. Allow it to grow.

### 5c. (Optional) lambda.zip rebuild

If you can build the lambda zip locally:
```bash
cd backend
python rebuild_lambda_zip.py
ls -lh lambda.zip  # or Get-Item lambda.zip in PowerShell
```

The size should be visibly smaller than the pre-cleanup baseline. Record the before/after size. If the rebuild fails, that's a real bug — investigate, don't ship.

**Acceptance.**
- [ ] `README.md` mentions no "dormant", "CodeBuild", "GCP Cloud Build", or "google-cloud-build". (Use `grep` to verify.)
- [ ] `HAUNTER.md` mentions no "Cloud Build" except possibly in a "history/cleanup" footnote.
- [ ] `README.md` size is roughly 125 lines (current 131).
- [ ] `HAUNTER.md` size is roughly 230-260 lines (current 211).
- [ ] (Optional) lambda.zip is smaller than the pre-cleanup baseline.
- [ ] Commit: `git add README.md HAUNTER.md && git commit -m "docs(cleanup): update README + HAUNTER.md to reflect post-cleanup single-provider reality"`.
- [ ] Final `pytest tests/ -q` shows 0 failures (sanity check that nothing in the doc changes accidentally broke a test that grep'd for old strings).

---

## What I'm NOT doing in this cleanup

- **Touching Phase 1-6 work.** That code is settled. The 5 sessions above delete adjacent code, not Phase code.
- **Adding new tests for AWS hosting.** The `test_hosting.py` AWS coverage may be thin. That's pre-existing. Not in scope.
- **Touching `WORK.md` or `issues.md` or `aws.md`.** `WORK.md` is the 14-phase build plan (all done). `issues.md` is an issue tracker. `aws.md` is the AWS Lambda deployment runbook. All still accurate post-cleanup, no changes needed.
- **Touching frontend.** The cleanup is backend-only.
- **Adding a CI lint that fails if `gcp` or `codebuild` re-appears in the codebase.** Could be a follow-up but not in this cleanup.
- **Phase 5 (custom domain URL stability).** Still parked. Separate decision.

---

## Rollback plan

If any session goes sideways:
- Each session ends with a single commit on `cleanup/dead-code-removal` branch.
- `git revert HEAD` from the cleanup branch safely undoes one session.
- `git reset --hard origin/main` from main (after `git checkout main`) wipes the entire cleanup branch and returns to the pre-cleanup state.
- The audit report from your 2nd agent (the file list above) is the canonical reference for what "pre-cleanup" looks like — if you need to manually re-add a file, you know exactly which one.

---

## Time budget (per session)

| Session | Estimated time | Difficulty | Risk |
|---|---|---|---|
| 0 (audit) | 30 min | low | low (read-only) |
| 1 (GCP sandbox) | 1-2 hr | medium | medium (cross-module import) |
| 2 (CodeBuild) | 1 hr | low | low (parallel to Session 1) |
| 3 (GCP hosting) | 45 min | low | low (well-isolated class) |
| 4 (defaults + Literals) | 30 min | low | medium (config flips can surprise tests) |
| 5 (docs) | 1 hr | low | low (docs only) |

**Total: 5-6 hours across 5 sessions, plus the 30-min audit.** Spread over 2-3 days. Don't try to do this in one sitting — Session 1 is the only one that needs fresh eyes, and you want a clean head for it.

---

## End state — what the cleaned codebase looks like

After all 5 sessions:

- **1 sandbox provider:** `github_actions`. No GCP, no AWS CodeBuild. `app/sandbox/__init__.py`'s `SANDBOX_PROVIDERS` has 1 entry.
- **1 hosting provider:** `aws` (Lambda). No GCP Cloud Run. `app/adapters/hosting.py` has 1 adapter class.
- **1 dependency removed:** `google-cloud-build`. `requirements.txt` is ~10 lines shorter.
- **13 files deleted, ~1,940 lines removed.**
- **`lambda.zip` ~30+ MB smaller.** Deploys ~3 minutes faster.
- **Tests still pass.** Baseline: 149 passed, 107 skipped. Post-cleanup: ~134 passed (15 fewer because `test_sandbox_verifier.py` deleted), 107 skipped. 0 failures.
- **Docs match reality.** No mention of dormant providers. Active stack documented as the only stack.

This is the codebase you'd want to onboard a new contributor into. No "what is this GCP class doing here?" moments. No "why is CodeBuild still configured if we don't use it?" questions. Just the one path that ships.
