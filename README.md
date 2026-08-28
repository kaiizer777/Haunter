# Haunter — Autonomous CI Failure Fix Agent

On `workflow_run` failure, Haunter wakes via webhook, diagnoses root cause, generates a fix, verifies it in an isolated sandbox, and opens a PR — or posts a diagnosis comment on exhaust. Human stays merge gate. Every step is traced.

```
GitHub Actions failure → Webhook → Orchestrator → Context Gatherer → Fix Generator → Sandbox Verifier (retry ≤3) → PR Writer / Fallback comment
```

## Key Features

- **Multi-repo webhook** `POST /webhooks/github` — HMAC `X-Hub-Signature-256` (constant-time), payload validation, dedupe on `X-GitHub-Delivery`, 10s async ack via `BackgroundTasks`.
- **Orchestrator/subagent isolation** — orchestrator holds only `{run_id, repo_id, step, confidence}`, never raw logs. Subagents are narrow, ephemeral, return distilled summaries.
- **Sandbox verification** — `Cloud Build` / `CodeBuild` via `SandboxRunner` abstraction: fresh clone, language detect (`python:3.12`/`node:22`) or repo Dockerfile, `git apply` patch, `install → test`, pass/fail with truncated failure reason.
- **Retry with alternate strategy** — failure reason fed back to Fix Generator, capped at 3 attempts (DB-enforced).
- **Observability** — `run_steps` timeline (tokens/latency/cost per step), failure classification, per-repo stats; all queries tenant-scoped.
- **Eval harness** — 20 golden cases (import/type/assertion/deps) on public test repos, per-subagent scores + regression diff.
- **Dashboard** — Cloudflare-hosted repos, runs feed, expandable trace, eval scores, confidence-vs-outcome chart, live model/provider switcher. GitHub OAuth gated.

## Tech Stack

| Layer | Choice |
|---|---|
| Orchestrator | FastAPI + `Mangum` (Lambda) / Cloud Run, SQLAlchemy 2.0 async + `asyncpg`, Alembic |
| Sandbox | Cloud Build or CodeBuild (`SandboxRunner` interface, `boto3` / `google-cloud-build`) |
| DB | Neon Postgres — pooled URL + `NullPool` (app), direct URL (migrations) |
| Auth | GitHub OAuth (`authlib` + `itsdangerous` signed `httpOnly` cookie), Fernet-encrypted tokens |
| Frontend | Next.js 16 (App Router), TypeScript 5, Tailwind 4, Cloudflare Pages |
| LLM | `LLMClient.complete()` → OpenCode Zen `https://opencode.ai/zen/v1` (`nemotron-3.5-lightning-free`), DB-driven `model_configs`, swappable without redeploy |

## Project Structure

```
haunter/
├── backend/        # FastAPI + orchestrator + subagents + LLM client + sandbox
│   ├── app/orchestrator.py
│   ├── app/llm/ + app/subagents/ + app/sandbox/
│   ├── alembic/ + app/models.py
│   └── lambda_handler.py
├── frontend/       # Next.js dashboard
├── infra/aws/      # Terraform: Lambda Function URL + CodeBuild
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

- **AWS Lambda** (default): ` infra/aws/lambda.tf` — 512MB/900s, Function URL `auth_type=NONE` (HMAC-secured), always-free `1M req + 400k GB-s/mo` → `$0` at ~10 users. CodeBuild sandbox `100 min/mo` free.
- **GCP Cloud Run**: `min-instances=0`, `*.run.app` URL, Cloud Build `2500 min/day` free. Retained as restore target.

Switch via `HOSTING_PROVIDER=gcp|aws` + `SANDBOX_PROVIDER=gcp|aws` (env or DB override, admin-only, allowlisted).

## Docs

- `HAUNTER.md` — product/arch spec
- `WORK.md` — 14-phase build plan (all DONE)
- `backend/tests/` — `pytest-asyncio` + `respx`
