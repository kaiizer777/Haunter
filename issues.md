# Haunter — open issues blocking end-to-end success

Last updated: 2026-09-01. Snapshot of everything still standing between the current
code base and a "Haunter opens a passing PR on a real CI failure" demo.

> Order is by impact: **blockers** at the top, **fragile** in the middle, **nice-to-have** at the bottom.

---

## BLOCKER-1 · DB session dies mid-orchestrator (Neon idle-timeout)

**What breaks it.** `backend/app/orchestrator.py:271` opens one `async with
async_session_maker() as db:` and reuses it for the whole retry loop (up to
`MAX_ATTEMPTS = 10` iterations, each taking 90s LLM call + 2-3 min sandbox
verify). After ~5 min Neon closes the idle connection, the next `db.commit()`
raises `InterfaceError: connection is closed`, the robust catch moves the run
to `error`, and the agent never gets to attempt #2.

**Evidence.** `ccec138d` and `72bf0ba0` runs both failed this way; the most
recent `bf7591c7` only survived 2 attempts because the second sandbox verify
completed before the timeout.

**Fix.** Use a fresh `async with async_session_maker() as db:` per attempt.
Reload `Run`, `Repo`, `prior_attempt` in the new session. The patch I
attempted mid-session was the right direction — aborted because the refactor
is large; needs a clean second pass. ~60-80 lines.

---

## BLOCKER-2 · LLM fix quality on the canonical CI failure

**What breaks it.** With the test mirror now seeded, verification actually
runs pytest. But the LLM-generated fixes don't make the test pass:

| Attempt | Patch | Why it fails |
|---|---|---|
| #1 | `conftest.py` with `sys.path.insert(0, '.../backend')` | Path is one level too deep — Python looks for `backend/backend/...` |
| #2 | `pyproject.toml` with `[tool.setuptools.packages.find]` | Needs `pip install -e .` to take effect; the CI workflow only does `pip install pytest` |
| #3 | rejected by `/dev/null` validator edge case (see FRAGILE-1) |

**The correct conftest.py fix is two lines:**
```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
```
(parent of `tests/` = repo root). The LLM is consistently one level off.

**Fix direction (in priority order):**

1. **Prompt example** — add a 3-line worked example of the canonical
   `conftest.py` pattern to `fix_generator.py:_build_messages` system prompt,
   so the LLM has a template to copy.
2. **Prior-attempt feedback** — the failure_reason is already passed into
   attempt #2, but it's not surfaced prominently in the prompt. Move it to
   the user message (not just strategy_notes) so the LLM can see the prior
   attempt's exact error.
3. **Cheap deterministic fallback** — if the diagnosis names
   `ModuleNotFoundError: No module named 'X'`, have the runner auto-emit a
   `sys.path.insert(0, repo_root)` patch and only invoke the LLM if that's
   insufficient. Bypasses the LLM-quality issue for the most common case.

---

## FRAGILE-1 · `/dev/null` validator edge case

**What breaks it.** `backend/app/subagents/fix_generator.py:173` does an
exact-match skip:
```python
if raw_path == "/dev/null":
    return
```
When the LLM produces a malformed patch where `/dev/null` is glued to
adjacent text (e.g. a `+++` on the next line with no whitespace), the
captured path becomes `/dev/null\n+++`, the equality check fails, and the
patch is rejected.

**Fix.** Normalize before compare:
```python
if raw_path.strip() == "/dev/null":
    return
```
One-line change + redeploy. Currently the only thing standing between a
green run and a spurious `PatchRejected` on attempt #3+.

---

## FRAGILE-2 · Long-running Lambda Function URL instability

**What breaks it.** Every `terraform taint aws_lambda_function.haunter &&
terraform apply` assigns the function a **brand new random URL**. The full
chain that must be updated in lockstep is documented in agent memory; the
short version:

1. Lambda env `CALLBACK_URL` (OAuth redirect_uri)
2. Lambda env `FRONTEND_URL`
3. GitHub webhook URL on the connected repo
4. Cloudflare Pages env `NEXT_PUBLIC_API_URL`
5. GitHub OAuth App registered callback URL
6. Local `frontend/.env.local` and `backend/.env`
7. `infra/aws/terraform.tfvars` (so next deploy keeps it)

**This bit us twice in one day.** The dashboard went 404, the OAuth flow
went to a dead URL, the trailing space in the env var added `%20` to the
callback. Every Cloudflare Pages build is a manual trigger.

**Fix direction.** Pin a custom domain to the Lambda Function URL. A single
`aws_apigatewayv2_domain_name` (or a Cloudflare `CNAME` to a stable alias)
makes the URL stable across deploys. Eliminates 4 of the 7 update points
permanently. ~2 hours of Terraform work.

---

## FRAGILE-3 · Test mirror seeding has no test coverage

**What breaks it.** `_seed_test_mirror_with_user_tree` in
`backend/app/sandbox/github_actions_runner.py:539` is ~150 lines of new
Git-Data-API code that runs on every verification call. It uses cross-repo
blob references (user's blob SHA → test mirror's tree), which is
documented to work but I've never seen a unit test for. If GitHub changes
the API behavior or the App loses a permission, this fails silently (best-
effort `logger.warning` on failure → fall back to old empty-mirror behavior
→ "Run tests: failure" with no useful error).

**Fix.** Add a unit test that hits a recorded HTTP interaction (respx or
pytest-httpx) covering: success path, 403+PAT-fallback, blob-not-found,
empty tree. ~80 lines of test.

---

## NICE-1 · `MAX_ATTEMPTS = 10` is a sandbox-burner

**What.** `orchestrator.py:373` allows 10 attempts. Each attempt is a
separate GitHub Actions workflow run on the test mirror. 10 attempts ×
~120s poll timeout = 20 minutes worst case. Combined with BLOCKER-1
(session close after ~5 min), the 3rd attempt onwards is the
probabilistic-failure regime.

**Fix.** Lower to 3, or add a fast-fail for "failure_reason matches prior
attempt's failure_reason" (means we're in a retry loop on a deterministic
LLM mistake; give up and post diagnosis comment).

---

## NICE-2 · Test case selection biases against the agent

**What.** The test case in `tests/test_analytics.py` is an intentionally
tricky "healing demo" with a wrong assertion (`avg_latency_ms == 150.0`
when actual is 175.0 for inputs (150, 200)). Even if BLOCKER-2 is solved,
the test is still not fixable by changing source code alone — the LLM has
to figure out the test itself is wrong, which requires the failure_reason
to include the assertion error (only visible after the import is fixed,
in attempt #2+).

**Fix direction.** For the demo: use a real, single-bug CI failure (e.g.
a typo in an import statement). For the long term: the dashboard should
have a "demo mode" toggle that picks a known-fixable test case.

---

## NICE-3 · Runner seeding has no `max_files` knob exposed

**What.** `_seed_test_mirror_with_user_tree` hardcodes `max_files = 50`
caller-side. For tiny repos this is fine. For large repos (1000+ files
in the failing commit) the runner would silently truncate, the
verification would test against a partial repo, the LLM fix might pass
locally but fail on the real CI.

**Fix.** Pull the cap from a `settings` field (env-driven) and document
the trade-off. 5-line change.

---

## NICE-4 · Patch re-parsing in `mirror.py` has no test for the `/dev/null` case

**What.** `backend/app/sandbox/mirror.py` re-parses patches to reconstruct
files for the test mirror. If a patch has a malformed `/dev/null` line
that gets stored as the *content* of a file (not as a header), the
parser will treat the next `+++` as a new hunk header. We saw this
manifest as the LLM-generated patch being mis-applied on attempt #1 of
the smoke test.

**Fix.** Add a regression test in `tests/test_sandbox_mirror.py` covering
the malformed `/dev/null` input. ~30 lines.

---

## NICE-5 · Secrets leaked in this session's terminal output

**What.** `OPENCODE_ZEN_API_KEY` and `GITHUB_TOKEN` PAT were both dumped
to terminal output (during env-var inspection) at various points. They
should be rotated.

**Fix.** Rotate both. OpenCode Zen: their dashboard. GitHub PAT:
github.com/settings/tokens.

---

## What "end to end working" actually requires

In priority order, the minimum path to "Haunter opens a passing PR on a
real CI failure":

1. **BLOCKER-1** — fresh DB session per attempt
2. **FRAGILE-1** — `/dev/null` validator whitespace strip
3. **BLOCKER-2** — prompt example for the canonical `conftest.py` pattern
4. **NICE-1** — lower `MAX_ATTEMPTS` to 3 (or add fast-fail on identical
   failure_reason)
5. **NICE-2** — switch the demo test case to a known-fixable one
6. **FRAGILE-2** — pin a custom domain (eliminates a class of
   repeat-deploy bugs forever)

1+2 are an hour of work. 3+4+5 are an afternoon. 6 is half a day.
