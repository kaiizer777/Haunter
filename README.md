# Haunter

<div align="center">

**Autonomous CI Failure Diagnosis, Self-Healing Repair & Security Auditing Engine**

[![Frontend](https://img.shields.io/badge/Frontend-Next.js%2016%20%7C%20React%2019-black?style=flat-square&logo=nextdotjs)](https://nextjs.org/)
[![Backend](https://img.shields.io/badge/Backend-FastAPI%20%7C%20Python%203.11-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com/)
[![Compute](https://img.shields.io/badge/Compute-AWS%20Lambda%20(x86__64)-FF9900?style=flat-square&logo=amazon-aws)](https://aws.amazon.com/lambda/)
[![Edge Hosting](https://img.shields.io/badge/Edge-Cloudflare%20Workers-F38020?style=flat-square&logo=cloudflare)](https://workers.cloudflare.com/)
[![Database](https://img.shields.io/badge/Database-Neon%20Postgres%20(Async)-00E599?style=flat-square&logo=postgresql)](https://neon.tech/)
[![Sandbox](https://img.shields.io/badge/Sandbox-GitHub%20Actions%20CI-181717?style=flat-square&logo=githubactions)](https://github.com/features/actions)
[![Runtime Preview](https://img.shields.io/badge/In--Browser%20Preview-StackBlitz%20WebContainer-1389FD?style=flat-square)](https://webcontainers.io/)
[![Multi--Model](https://img.shields.io/badge/LLM-OpenCode%20Zen%20%7C%20OpenAI%20%7C%20Anthropic-7C3AED?style=flat-square)](https://opencode.ai/zen/v1)
[![License](https://img.shields.io/badge/License-Business%20Source%201.1-yellow?style=flat-square)](LICENSE)

</div>

Haunter diagnoses GitHub Actions failures, verifies candidate fixes in an isolated sandbox, and opens auditable PRs — it never merges on its own.

## What it does

- Wakes on `workflow_run` failure via webhook and distills logs plus failing diff into a root cause.
- Generates a candidate patch with a confidence score, retrying up to 3 times on sandbox feedback.
- Verifies each candidate in an ephemeral GitHub Actions mirror repo before proposing it.
- Opens a fix PR on pass, or posts a diagnosis-only comment on exhaust. Human Merge Gate: Haunter never auto-merges or pushes to default branches.

## How it works

1. **Context Gatherer** — extracts failing logs, trace, and implicated diff.
2. **Fix Generator** — produces a patch plus confidence score (default model: `nemotron-3.5-lightning-free` via OpenCode Zen).
3. **Sandbox Verifier** — seeds a mirror repo via GitHub Git Data API and polls check-runs for a pass/fail signal.
4. **PR Writer** — opens a verified-fix PR, or a structured diagnostic comment when attempts are exhausted.

Text flow: `failure → context → fix → sandbox → PR (or diagnosis comment)`

## Features

- **Auditor Mode** — read-only security, architecture, regression, and perf reviews; never creates branches or PRs unprompted.
- **Live Sessions** — in-browser pairing studio with WebContainer Node runtime and Monaco diff editor, checkpoints, and one-click CI verify.
- **Per-repo governance** — presets (`autonomous`, `conservative`, `standard`, `audit_only`, `live_studio_only`, `custom`) with confidence thresholds and spend caps.
- **Multi-model engine** — OpenCode Zen default (`nemotron-3.5-lightning-free`), switchable to OpenAI / Anthropic at runtime.
- **Eval harness** — 20 golden fixtures for regression benchmarking; demo mode pinned to `fixture-001`.
- **Full audit trail** — tokens, latency, cost, confidence, and per-run traces stored in Neon Postgres and surfaced on the dashboard.

## Tech stack

| Layer | Tech |
| --- | --- |
| Backend | FastAPI + Mangum on AWS Lambda |
| Database | Neon Postgres (SQLAlchemy async + `asyncpg`, `NullPool`) |
| Frontend | Next.js 16 SPA on Cloudflare Workers |
| Sandbox | GitHub Actions ephemeral mirror verifier |
| Studio | WebContainer in-browser Node + Monaco diff editor |
| LLM | OpenCode Zen (default `nemotron-3.5-lightning-free`), OpenAI, Anthropic |
| Auth | GitHub OAuth, HMAC-SHA256 webhooks, Fernet-encrypted session cookie |

## Quickstart

Prereqs: Python 3.11+, Node.js 20+, a Neon Postgres instance, GitHub OAuth credentials, and an `OPENCODE_ZEN_API_KEY`.

Configure `backend/.env` (`DATABASE_URL`, `GITHUB_*`, `OPENCODE_ZEN_API_KEY`). See `HAUNTER.md` and `redeploy.md` for the full variable list.

```bash
# Backend (port 7555)
cd backend
python -m venv .venv && .\.venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head
python -m uvicorn main:app --reload --port 7555

# Frontend (port 3011)
cd frontend
npm install
npm run dev
```

Health: backend `http://127.0.0.1:7555/health`, dashboard `http://localhost:3011`.

## Docs

[HAUNTER.md](HAUNTER.md) · [WORK.md](WORK.md) · [redeploy.md](redeploy.md) · [github.md](github.md) · [future02.md](future02.md)

## License

Licensed under Business Source License 1.1 — source-available, not OSI open source. See [LICENSE](LICENSE) for terms and GPL conversion.
