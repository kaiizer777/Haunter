# Haunter — `fix.md`: Phased Remediation Plan

**Date:** 2026-09-01
**Source:** `issues.md` (12 open issues) + `AUDIT_REPORT.md` cross-references
**Goal:** Take Haunter from "demo occasionally crashes the orchestrator" to "Haunter opens a passing PR on a real CI failure, repeatably."
**Method:** Six phases, ordered by impact. Each phase is **independently shippable** — no phase depends on unstarted later work. Within a phase, checkboxes are the order of execution.

> **How to read this doc.** Each phase has:
> 1. A one-line outcome (what is true after the phase ships).
> 2. A scope bullet (files touched, lines estimated).
> 3. Concrete checkboxes with the actual edit.
> 4. An **Acceptance** section with the smoke test that proves the phase works.
>
> Stop and run the acceptance test before ticking the phase box. If it fails, the phase is not done.

---

## Phase 1 — Stop the orchestrator from dying mid-run (BLOCKER-1 + NICE-1 + O-06 + O-07 + O-09)

**Outcome.** The orchestrator no longer fails on Neon idle-timeout, no longer burns 10 sandbox attempts, has a single hard wall-clock timeout, and row-locks the Run record.

**Why first.** Every other phase produces a working pipeline only if the pipeline stays alive long enough to run it. BLOCKER-1 alone is responsible for the two consecutive `ccec138d` / `72bf0ba0` failed runs. Fixing it unblocks BLOCKER-2 verification too.

**Scope.** `backend/app/orchestrator.py` (~80 lines), `backend/app/config.py` (~10 lines), `backend/app/subagents/fix_generator.py` (~5 lines). No schema change, no infra change.

- [ ] **Add `MAX_ATTEMPTS` to `app/config.py`.** Single source of truth. Pydantic field with default `3` (down from 10) and `env="HAUNTER_MAX_ATTEMPTS"`. Remove the local `MAX_ATTEMPTS = 10` in `orchestrator.py:373` and the duplicate in `fix_generator.py:43`; both modules import from `config`. (Addresses NICE-1 + O-07.)
- [ ] **Fast-fail on repeated `failure_reason`.** In the orchestrator retry loop, before invoking `generate_fix`, compare the new diagnosis's `failure_reason` substring to the previous attempt's. If the trailing 200 chars of `failure_reason` match exactly, raise `AttemptCapExceeded` early and post the diagnosis comment. Avoids the 3-iteration retry-on-deterministic-LLM-mistake pattern.
- [ ] **Fresh DB session per attempt.** Replace the single `async with async_session_maker() as db:` at `orchestrator.py:271` with a per-iteration session. The context-gathering call stays on the outer session (single short call, no timeout risk); the retry loop opens its own `async with async_session_maker() as attempt_db:` per attempt. Reload `Run`, `Repo`, `prior_attempt` inside the loop. (Addresses BLOCKER-1.)
- [ ] **Wrap the whole orchestrator body in `asyncio.wait_for(..., timeout=800)`.** Catch `asyncio.TimeoutError`, transition the run to `error`, persist `failure_reason="orchestrator wall-clock timeout"`. (Addresses O-06.) 800s leaves 100s of headroom under Lambda's 900s limit.
- [ ] **Row-lock the Run record.** Change `select(Run).where(Run.id == run_id)` to `.with_for_update()`. Mirrors the existing Repo lock at `orchestrator.py:285`. (Addresses O-09.)
- [ ] **Add a unit test.** `backend/tests/test_orchestrator_session.py`: monkeypatch the per-attempt `db` so it raises `InterfaceError("connection is closed")` on attempt #2's first commit; assert attempt #3 opens a new session and the run ends in `pr_opened` or `fallback_commented`, not `error`.

**Acceptance.**
- [ ] `pytest backend/tests/test_orchestrator_session.py -q` is green.
- [ ] Re-run the Phase 0 baseline smoke 3 times. After 5 minutes, the run is still in `fix_generation` or beyond (not `error: InterfaceError`).
- [ ] A run with a deterministic LLM failure (e.g. force the LLM to return `patch=""` twice) reaches `fallback_commented` on attempt #2 or #3, not #10.
- [ ] `grep -n MAX_ATTEMPTS backend/app/` shows exactly one definition (in `config.py`).

---

## Phase 2 — Patch-validator hardening (FRAGILE-1 + NICE-4 + R-02)

**Outcome.** The validator stops rejecting valid patches because of a stray `\n`, the mirror parser stops eating malformed `/dev/null` markers, and the empty `RunStep` noise on `PatchRejected` is fixed.

**Why second.** This is a one-line change that takes us from "spurious rejection on attempt #3+" to "validator only rejects truly broken patches." Cheap and unblocks the verification loop end-to-end.

**Scope.** `backend/app/subagents/fix_generator.py` (~5 lines), `backend/app/sandbox/mirror.py` (~10 lines), `backend/tests/test_sandbox_mirror.py` (~30 lines, new), `backend/app/orchestrator.py` (~5 lines).

- [ ] **Strip `/dev/null` check.** Change `if raw_path == "/dev/null":` to `if raw_path.strip() == "/dev/null":` at `fix_generator.py:173`. (Addresses FRAGILE-1.)
- [ ] **Same strip in `mirror.py:328`.** Add `.strip()` to the deletion-marker check so a `+++ /dev/null\r\n` (CRLF patch) is treated identically. (Addresses NICE-4.)
- [ ] **Regression test for `fix_generator._check_path`.** `backend/tests/test_fix_generator_paths.py`: add cases for `"/dev/null\n"`, `"/dev/null\r\n"`, `" /dev/null"`, `"/dev/null \n"` — all must be accepted. Plus the existing reject cases (absolute, `..`, blocked prefix) still reject.
- [ ] **Regression test for `mirror.py` re-parse.** `backend/tests/test_sandbox_mirror.py::test_malformed_dev_null` — feed a patch where `+++ /dev/null` has a trailing tab and the next `+++` line is glued; assert the parser produces a deletion marker, not a file named `"/dev/null\t+++"`.
- [ ] **Stop inserting empty `RunStep` on `PatchRejected`.** At `orchestrator.py:418-427`, only insert a `RunStep` if `tokens_used > 0` or `latency_ms > 0`. Otherwise log at `WARNING` and skip. (Addresses R-02.)

**Acceptance.**
- [ ] `pytest backend/tests/test_fix_generator_paths.py backend/tests/test_sandbox_mirror.py -q` is green.
- [ ] Hand-crafted patch with `+++ /dev/null\n` in the body now passes the validator (was previously rejected with `PatchRejected`).
- [ ] Dashboard trace for a `PatchRejected` run no longer shows a 0-token 0-ms step entry.

---

## Phase 3 — LLM fix quality on the canonical failure (BLOCKER-2 + FG-05)

**Outcome.** When the diagnosis says `ModuleNotFoundError: No module named 'X'`, the LLM emits the right `conftest.py` patch on attempt #1 ~95% of the time, and prior-attempt failures are visible in the user message (not buried in `strategy_notes`).

**Why third.** Phase 1 keeps the pipeline alive; this phase makes the pipeline produce a correct fix. Without it, the agent loops through 3 identical-wrong patches.

**Scope.** `backend/app/subagents/fix_generator.py` (`_build_messages`, ~25 lines), `backend/app/subagents/fix_generator.py` (deterministic fallback, ~40 lines, optional). One new constant, one new branch.

- [ ] **Add a 3-line worked example to the system prompt.** Inside `_build_messages` at `fix_generator.py:223`, append a concrete `conftest.py` example to the existing "FILE-LEVEL FIXES ARE ENCOURAGED" block:

      Example: when the diagnosis is `ModuleNotFoundError: No module named 'app'`
      and the tests/ directory exists, the canonical fix is a top-level
      `conftest.py` with:
          import sys, os
          sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

- [ ] **Move `prior_section` out of the user message's tail and into a dedicated user turn.** Currently `prior_attempt.failure_reason` is in the same user message as the diagnosis. Restructure so prior failures appear as a second user turn (`{"role": "user", "content": "## Prior Attempt #N ... ## Failure Reason: ..."}`) right after the diagnosis. The LLM sees the prior failure as a discrete conversation event, not appendix text. (Addresses BLOCKER-2 #2.)
- [ ] **Redact `prior_attempt.patch_text` before embedding.** Call `_redact_secrets(prior_attempt.patch_text)` at the point of formatting into the prompt. (Addresses FG-05.)
- [ ] **Deterministic `ModuleNotFoundError` fallback.** New helper `_module_not_found_path_fix(diagnosis_summary: str) -> Optional[str]`:
  - Returns `None` if the diagnosis does not name a `ModuleNotFoundError`.
  - If it does, regex out the module name; if it's a relative import (e.g. `from app.X import Y` and the module is `app`), emit a `conftest.py` patch with the `sys.path.insert` snippet above. Confidence = 95.
  - In `generate_fix`, if the helper returns a patch, skip the LLM call and use the deterministic patch (still log a `RunStep` of type `deterministic_fix`). Bypasses LLM quality for the most common case.
- [ ] **Add a unit test.** `backend/tests/test_fix_generator_deterministic.py`: feed a diagnosis containing `ModuleNotFoundError: No module named 'app'`, assert `_module_not_found_path_fix` returns a non-empty `conftest.py` patch with the right `sys.path` line. Feed a diagnosis without the error, assert it returns `None`.

**Acceptance.**
- [ ] `pytest backend/tests/test_fix_generator_deterministic.py -q` is green.
- [ ] Run the smoke 5 times against the canonical test-mirror failure. At least 4 of 5 runs reach `pr_opened` or `fallback_commented` on attempt #1, not attempt #3.
- [ ] Inspect one trace: the prior attempt's `failure_reason` appears as a separate user-message turn, not buried in `strategy_notes`.

---

## Phase 4 — Sandbox seeding robustness (FRAGILE-3 + NICE-3 + T-02)

**Outcome.** Test-mirror seeding is covered by tests, the file cap is configurable, and the test-mirror seeder handles App-token / PAT fallback, blob-not-found, and empty trees.

**Why fourth.** By this point we have a working pipeline. Now we make the GitHub Actions sandbox path (the active `SANDBOX_PROVIDER=github_actions`) survive on real-world repos — large trees, private files, flaky auth.

**Scope.** `backend/app/sandbox/github_actions_runner.py` (~10 lines), `backend/app/config.py` (~5 lines), `backend/tests/test_seed_user_tree.py` (~80 lines, new).

- [ ] **Settings-driven `max_files`.** Add `seed_max_files: int = 50` to `app/config.py` with `env="HAUNTER_SEED_MAX_FILES"`. Replace the hardcoded `max_files=50` at `github_actions_runner.py:924` with `max_files=settings.seed_max_files`. Document the trade-off in the field description: "values >200 may exceed GitHub Actions runner time; values <20 may under-seed large repos." (Addresses NICE-3.)
- [ ] **Unit test for `_seed_test_mirror_with_user_tree`.** New file `backend/tests/test_seed_user_tree.py` using `respx` (or `pytest-httpx`) to mock the GitHub API. Cover:
  - **Success path:** 200 on `/git/commits/{sha}`, 200 on `/git/trees/{tree_sha}?recursive=1`, 200 on blob creation, 200 on tree creation, 200 on commit creation, 200 on ref update. Assert `True` return.
  - **PAT fallback:** first 403 on `/git/commits/{sha}` with App token, second 200 with PAT. Assert `True` return.
  - **Blob not found:** 422 on blob creation; assert `False` return (best-effort) and a `WARNING` log.
  - **Empty tree:** 200 with `tree=[]`; assert `True` return and a single README-only commit.

**Acceptance.**
- [ ] `pytest backend/tests/test_seed_user_tree.py -q` is green and runs without network.
- [ ] Setting `HAUNTER_SEED_MAX_FILES=10` in `backend/.env` and restarting causes the runner to log `max_files=10` at the seed call.

---

## Phase 5 — Deploy URL stability (FRAGILE-2)

**Outcome.** `terraform apply` no longer rotates the public Lambda URL. The 7-place manual update checklist shrinks to 1 (terraform.tfvars), and only matters when the *custom domain* changes.

**Why fifth.** Phases 1-4 make the system correct. This phase makes it stay correct across deploys — without it, every other fix can be wiped out by a single `terraform apply` that the user runs the next morning.

**Scope.** `infra/aws/terraform.tfvars`, `infra/aws/lambda.tf` (new `aws_apigatewayv2_domain_name` + `aws_apigatewayv2_api_mapping` ~40 lines), Cloudflare DNS CNAME (1 record), OAuth App / webhook / dashboard / local envs (1-line edits after the deploy).

- [ ] **Provision a custom domain for the Lambda Function URL.** Pick a stable subdomain (e.g. `api.haunter.yourdomain.com`). In `infra/aws/lambda.tf`:
  - Add `aws_apigatewayv2_domain_name` with ACM cert ARN.
  - Add `aws_apigatewayv2_api_mapping` mapping the custom domain to the existing Lambda Function URL.
  - Keep the original Function URL (don't disable it) so deploys that reference it don't break before the mapping attaches.
- [ ] **Add `api_base_url` to `terraform.tfvars`.** Set it to `https://api.haunter.yourdomain.com`. Reference it in `lambda_function.environment.variables.CALLBACK_URL` and `FRONTEND_URL`.
- [ ] **Add the CNAME in Cloudflare.** Point `api.haunter.yourdomain.com` → the API Gateway domain name. Proxy off (DNS-only) so ACM validation works.
- [ ] **Update the OAuth App callback URL** to `https://api.haunter.yourdomain.com/auth/callback` (GitHub Developer settings).
- [ ] **Update the GitHub webhook** on the connected test-mirror repo: Payload URL → `https://api.haunter.yourdomain.com/webhook`.
- [ ] **Update Cloudflare Pages env** `NEXT_PUBLIC_API_URL` → `https://api.haunter.yourdomain.com`.
- [ ] **Update local dev envs** `frontend/.env.local` and `backend/.env` to the new base URL.
- [ ] **Document the deploy pre-flight checklist** in a new section of `AGENTS.md` ("Deploy pre-flight"): the one thing that still needs touching per deploy is `terraform.tfvars` if the custom domain itself moves; everything else is pinned.
- [ ] **Verify with a deliberate `terraform taint aws_lambda_function.haunter && terraform apply`.** Confirm the custom domain URL does not change. Confirm a webhook → orchestrator → PR round-trip works end-to-end after the taint.

**Acceptance.**
- [ ] `terraform apply` produces a plan that does **not** include `aws_lambda_function_url.haunter` destruction or URL change.
- [ ] `curl https://api.haunter.yourdomain.com/health` returns `{"status":"ok"}`.
- [ ] After a taint+apply, the OAuth flow completes; the webhook fires; the dashboard's "Latest Run" updates.
- [ ] `grep -rn gjdbtzw5h\|a5fc7vxb backend/ frontend/ infra/ 2>/dev/null` returns nothing (no leftover hardcoded old URLs).

---

## Phase 6 — Demo & long-tail polish (NICE-2 + NICE-1 demo follow-up + NICE-3 dashboard surface)

**Outcome.** The dashboard has a "demo mode" toggle that picks a known-fixable test case. Running Haunter in demo mode against the canonical test-mirror failure opens a passing PR on the first 5 consecutive attempts.

**Why last.** Once everything above is in place, this is the visible "it works" moment. It's also the moment we'd want to put in front of an interviewer or a user. Not blocking correctness; blocking demo-ability.

**Scope.** `backend/app/routers/eval.py` (~20 lines), `frontend/src/components/eval/*` (~50 lines), `tests/test_analytics.py` (test fixture replacement, optional), `README.md` (one new section).

- [ ] **Add a "demo mode" flag to the eval runner.** New field `demo_mode: bool = False` on the eval request. When true, the runner forces the test case to a known-fixable seed (e.g. an `ImportError` caused by a typo in `app/services/billing.py`) and pins the model to `nemotron-3.5-lightning-free`.
- [ ] **Surface the toggle in the dashboard.** New switch in the eval-harness page header. Tooltip: "Pins the eval to a known-fixable canonical failure. Use for demos and CI."
- [ ] **Replace `test_analytics.py` with a fixable case** (or add a new file `test_demo_canonical.py` alongside it). The replacement: a single import that is broken by a one-character typo. The agent's fix is to correct the typo. This becomes the default test case in demo mode. (Addresses NICE-2.)
- [ ] **Document the demo path in `README.md`.** One new section: "Demoing Haunter in 5 minutes" — lists the exact commands and the expected PR URL pattern. No hand-waving about "run this and see."
- [ ] **Final smoke:** 5 consecutive demo runs from a clean state. Expectation: 5/5 reach `pr_opened` with a passing CI on the test-mirror.

**Acceptance.**
- [ ] Dashboard "Demo mode" toggle is visible and persists across page reloads.
- [ ] 5/5 demo runs reach `pr_opened`. (If even one fails, the phase is not done — do not paper over.)
- [ ] `README.md` has the new section; a new user can follow it without asking questions.

---

## Out of scope (explicitly not in this plan)

These are in `AUDIT_REPORT.md` and are real, but they don't block the "open a passing PR" demo. Park them for a follow-up plan:

- **S-04** — Subagent credential scope (HIGH, but currently mitigated by GitHub App shared identity; document and close).
- **CG-05** — Consolidate secret regexes (MEDIUM; doesn't affect demo).
- **I-04** — Lambda Python 3.12 + arm64 (MEDIUM; current Python 3.11 works).
- **I-05** — AWS budget alarm (MEDIUM; $0.005/min is acceptable for current volume).
- **G-05** — Lambda `.env` packaging (MEDIUM; mitigated by `rebuild_lambda_zip.py` and post-deploy audit; should be done but not demo-blocking).
- **O-05, CG-04, CG-06, FG-06, S-05, S-09** — LOW items, batch into a Phase 7 "polish" plan once Phase 6 ships.

---

## How to use this file

1. Tick the **pre-flight** box. Rotate the secrets. Capture the baseline. Stop and re-read if anything looks off.
2. Work Phase 1. Run its acceptance. Tick the phase box at the top. Commit.
3. Work Phase 2. Run its acceptance. Commit.
4. Same for Phases 3-6. One commit per phase. Don't bundle.
5. After Phase 6 lands, the canonical user-visible claim is: "Haunter opens a passing PR on a real CI failure, repeatably, in demo mode." Anything less is incomplete; do not claim it.

If a phase is blocked, leave its box unticked, write a one-line `BLOCKED:` note under the relevant checkbox describing the blocker, and ask for direction. **Do not silently scope-cut a phase to make it green.**
