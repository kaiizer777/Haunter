# Haunter — Autonomous CI Failure Fix Agent

On `workflow_run` failure, Haunter wakes via webhook, diagnoses root cause, generates a fix, verifies it in an isolated sandbox, and opens a PR — or posts a diagnosis comment on exhaust. Human stays merge gate. Every step is traced.

```
GitHub Actions failure → Webhook → Orchestrator → Context Gatherer → Fix Generator → Sandbox Verifier (retry ≤3) → PR Writer / Fallback comment
```

## Key Features

- **Multi-repo webhook** `POST /webhooks/github` — HMAC `X-Hub-Signature-256` (constant-time), payload validation, dedupe on `X-GitHub-Delivery`, 10s async ack via `AWSHostingAdapter` (async self-invoke on Lambda).
- **Orchestrator/subagent isolation** — orchestrator holds only `{run_id, repo_id, step, confidence}`, never raw logs. Subagents are narrow, ephemeral, return distilled summaries.
- **Sandbox verification** — GitHub Actions CI via per-user test mirrors (`kaiizer777/haunter-test-{hash}`). Migrated from AWS CodeBuild (account quota blocker) to polled GitHub Actions: pushes test templates (`haunter-test-py.yml` / `haunter-test-ts.yml`) and patch commits, polls Actions check-runs API up to 2m, returns pass/fail with sanitized logs.
- **Retry with alternate strategy** — failure reason fed back to Fix Generator, capped at 3 attempts (DB-enforced).
- **Observability** — `run_steps` timeline (tokens/latency/cost per step), failure classification, per-repo stats; all queries tenant-scoped.
- **Eval harness** — 20 golden cases (import/type/assertion/deps) on public test repos, per-subagent scores + regression diff.
- **Dashboard** — Cloudflare Pages-hosted repos, runs feed, expandable trace, eval scores, confidence-vs-outcome chart, live model/provider switcher. GitHub OAuth gated.

## Tech Stack

| Layer | Choice |
|---|---|
| Orchestrator | FastAPI + `Mangum` on AWS Lambda (Function URL), SQLAlchemy 2.0 async + `asyncpg`, Alembic |
| Sandbox | GitHub Actions CI (`github_actions_runner`) via per-user test mirrors (AWS CodeBuild & GCP dormant) |
| DB | Neon Postgres — pooled URL + `NullPool` (app), direct URL (migrations) |
| Auth | GitHub OAuth (`authlib` + `itsdangerous` signed `httpOnly` cookie), Fernet-encrypted tokens |
| Frontend | Next.js 16 (App Router), TypeScript 5, Tailwind 4, Cloudflare Pages |
| LLM | `LLMClient.complete()` → OpenCode Zen `https://opencode.ai/zen/v1` (`nemotron-3.5-lightning-free`), DB-driven `model_configs`, swappable without redeploy |

## How the sandbox works

Haunter previously executed sandboxes via AWS CodeBuild (`AWSSandboxRunner`). When the AWS account ran into `AccountLimitExceededException` (concurrent-build quota = 0 in `us-east-1`), the sandbox was migrated to **GitHub Actions CI** (`GitHubActionsSandboxRunner`) using polled test mirrors (documented in `github.md`):

1. **Mirror lifecycle (`mirror.py`)**: On the first webhook for a user, Haunter creates a private, isolated test mirror under `kaiizer777` (e.g., `kaiizer777/haunter-test-{8-char-hash}`). The repository is cached and reused across future runs.
2. **Template deployment**: Auto-detects runtime (`py` or `ts`) and pushes the appropriate workflow template (`haunter-test-py.yml` with pytest/ruff/mypy or `haunter-test-ts.yml` with npm test/tsc/eslint) into `.github/workflows/` using a PAT fallback (bypassing GitHub App `workflows:write` permission constraints).
3. **Commit per attempt**: For each fix attempt, the patch is applied via the Git Data API as a single commit on branch `haunter-attempt-{N}`, triggering GitHub Actions without requiring local git or Docker.
4. **Polling & verification**: The runner polls the Actions check-runs API every 10s (up to 2 minutes). If tests pass and confidence is ≥ 30, Haunter opens a PR on the real repo; if tests fail, sanitized logs feed the next retry; non-retryable errors (e.g. auth/quota) immediately trigger the fallback diagnosis comment.
5. **Health monitoring**: `GET /health/sandbox` exposes active provider, org, and App ID.

## Known limitations & Caveats

- **Sandbox location**: Hosted on the `kaiizer777` personal GitHub namespace rather than the originally-planned `haunter-sandboxes` org, because the GitHub App could not be installed cleanly on the org without granting excessive permissions.
- **Dormant infra**: AWS Lambda orchestrator infra is fully active. The previously deployed AWS CodeBuild sandbox (`infra/aws/codebuild.tf`) and GCP Cloud Build remain dormant fallbacks in `SANDBOX_PROVIDERS`; GitHub Actions is the active runner (`SANDBOX_PROVIDER=github_actions`).
- **Secrets**: Requires Lambda env vars for GitHub App ID, Installation ID, and AWS SSM Parameter Store for the GitHub App private key (`/haunter/GITHUB_SANDBOX_APP_PRIVATE_KEY`). 

## Project Structure

```
haunter/
├── backend/        # FastAPI + orchestrator + subagents + LLM client + sandbox
│   ├── app/orchestrator.py
│   ├── app/llm/ + app/subagents/ + app/sandbox/
│   ├── alembic/ + app/models.py
│   └── lambda_handler.py
├── frontend/       # Next.js dashboard
├── infra/aws/      # Terraform: Lambda Function URL + CodeBuild (dormant)
└── HAUNTER.md / WORK.md
```

## Quick Start

**Backend** `http://localhost:8000/health`
```bash
cd backend
python -m venv .venv && .\.venv\Scripts\activate  # Windows: .\.venv\Scripts\activate
pip install -r requirements.txt
# .env: DATABASE_URL, DATABASE_URL_UNPOOLED, GITHUB_CLIENT_ID/SECRET, SESSION_SECRET_KEY, FRONTEND_URL, OPENCODE_ZEN_API_KEY, GITHUB_WEBHOOK_SECRET
uvicorn main:app --reload
```

**Frontend** `http://localhost:3000`
```bash
cd frontend
npm install
npm run dev  # NEXT_PUBLIC_API_URL -> backend URL
```

## Deployment

- **AWS Lambda**: `infra/aws/lambda.tf` — 512MB/900s ARM64, Function URL `auth_type=NONE` (HMAC-secured), always-free `1M req + 400k GB-s/mo` → `$0` at ~10 users.
- **Cloudflare Pages**: Next.js 16 frontend dashboard deployed on Cloudflare Pages connected to Lambda Function URL.

## Demoing Haunter in 5 minutes

The dashboard's eval-harness page has a **Demo mode** toggle that pins the
eval to a known-fixable canonical failure and the default model. This is the
path to show "Haunter opens a passing PR" in front of a user or interviewer.

The canonical demo test case lives at
`backend/tests/demo_canonical/test_demo_canonical.py`. It has a deliberate
one-character typo in an import (`from app.servies.billing import charge` —
missing `c` in `services`). The orchestrator's deterministic
`ModuleNotFoundError` fast-path (Phase 3) catches it and applies a
`conftest.py` fix. The PR then passes.

1. `cd backend && pip install -r requirements.txt`
2. Set the required env vars in `backend/.env`:
   - `DATABASE_URL`, `DATABASE_URL_UNPOOLED` (Neon)
   - `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`
   - `SESSION_SECRET_KEY`, `TOKEN_ENCRYPTION_KEY`
   - `FRONTEND_URL`, `CALLBACK_URL`
   - `OPENCODE_ZEN_API_KEY`
   - `GITHUB_WEBHOOK_SECRET`
   - `ADMIN_USER_ID` (your user UUID, to unlock the eval page)
   - `SANDBOX_PROVIDER=github_actions`
   - `GITHUB_SANDBOX_APP_ID`, `GITHUB_SANDBOX_INSTALLATION_ID`
   - `GITHUB_SANDBOX_APP_PRIVATE_KEY` (PEM, or via SSM)
3. `uvicorn main:app --reload --port 8000`
4. Open the dashboard at `http://localhost:3000` and sign in.
5. Navigate to the **Eval Harness** page.
6. Toggle **Demo mode** on (top-right, next to "Run Eval Harness"). The
   toggle persists across page reloads via `localStorage`.
7. Click **Run Eval Harness** → **Start Benchmark**.
8. The runner pins to `fixture-001` and the default model. A live LLM call
   runs against the canonical import-error fixture.
9. Open the resulting `EvalResult` row in the dashboard to see scores; the
   matching `Run` trace shows the pipeline state.

For the end-to-end "open a passing PR" demo, the test mirror in the
`haunter-sandboxes` org must be configured (see `github.md`). The eval
harness path above exercises the LLM and the deterministic fast-path; the
full PR round-trip requires the deployed Lambda + GitHub App + test-mirror
org, and is out of scope for local CI verification.

## Docs

- `HAUNTER.md` — product/arch spec
- `WORK.md` — 14-phase build plan (all DONE)
- `github.md` — GitHub Actions Sandbox migration runbook & architecture
- `aws.md` — AWS Lambda deployment runbook
- `backend/tests/` — `pytest-asyncio` + `respx`
