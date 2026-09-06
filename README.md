# Haunter

<div align="center">

**Autonomous CI Failure Diagnosis & Fix Agent**

[![Frontend](https://img.shields.io/badge/Frontend-Next.js%2016%20%7C%20React%2019-black?style=flat-square&logo=nextdotjs)](https://nextjs.org/)
[![Backend](https://img.shields.io/badge/Backend-FastAPI%20%7C%20Python%203.11-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com/)
[![Compute](https://img.shields.io/badge/Compute-AWS%20Lambda%20(x86__64)-FF9900?style=flat-square&logo=amazon-aws)](https://aws.amazon.com/lambda/)
[![Edge Hosting](https://img.shields.io/badge/Edge-Cloudflare%20Workers-F38020?style=flat-square&logo=cloudflare)](https://workers.cloudflare.com/)
[![Database](https://img.shields.io/badge/Database-Neon%20Postgres%20(Async)-00E599?style=flat-square&logo=postgresql)](https://neon.tech/)
[![Sandbox](https://img.shields.io/badge/Sandbox-GitHub%20Actions%20CI-181717?style=flat-square&logo=githubactions)](https://github.com/features/actions)
[![Default LLM](https://img.shields.io/badge/LLM-OpenCode%20Zen%20(Nemotron%203.5)-7C3AED?style=flat-square)](https://opencode.ai/zen/v1)

</div>

---

Haunter wakes autonomously when a connected repository's GitHub Actions workflow fails. It gathers distilled failure context, determines the root cause, generates targeted candidate patches, verifies fixes inside an isolated GitHub Actions sandbox runner, and opens an auditable Pull Request with complete diagnostic rationale — or posts a comprehensive diagnostic comment if retries are exhausted.

> **Human In The Loop:** Haunter is strictly designed to diagnose, verify, and propose fixes. It **never** auto-merges or commits directly to default branches. Developers remain the final merge gate.

---

## Architecture Overview

```mermaid
flowchart TD
    subgraph GitHub ["GitHub Platform"]
        GHA["GitHub Actions CI Failure"]
        PR["Target Repo: Pull Request or Comment"]
        TestMirror["Isolated Sandbox Mirror Repo\n(kaiizer777/haunter-test-{hash})"]
    end

    subgraph Backend ["AWS Lambda (us-east-1, x86_64) — FastAPI + Mangum"]
        Webhook["POST /webhooks/github\nHMAC-SHA256 + Deduplication"]
        AsyncSelf["Async Self-Invocation\n(AWSHostingAdapter)"]
        
        subgraph Pipeline ["Multi-Stage Subagent Pipeline"]
            CG["1. Context Gatherer\n(Distills log trace & failing diff)"]
            FG["2. Fix Generator\n(Produces patch + confidence score)"]
            SB["3. Sandbox Verifier\n(GitHubActionsSandboxRunner)"]
            PW["4. PR Writer / Fallback\n(Creates PR or diagnostic comment)"]
        end
    end

    subgraph Storage ["Neon Postgres"]
        DB[(Neon Serverless DB\nRuns · Steps · Evals · ModelConfigs)]
    end

    subgraph FrontendApp ["Cloudflare Workers"]
        Dashboard["Next.js 16 SPA Dashboard\n(Cross-Origin Cookie Session)"]
    end

    GHA -->|"workflow_run.completed (failure)"| Webhook
    Webhook -->|"200 Ack (<10s)"| GHA
    Webhook -.->|"Self-Invoke Async"| AsyncSelf
    AsyncSelf --> CG
    CG --> FG
    FG --> SB
    SB <-->|"Git Data API Seeding\n& Check-Runs Polling"| TestMirror
    SB -->|"Pass (Confidence >= 30)"| PW
    SB -.->|"Fail (Retry <= 3)"| FG
    PW -->|"Verified Fix PR"| PR
    PW -.->|"Attempts Exhausted Comment"| PR

    CG & FG & SB & PW -->|"Telemetry (Tokens / Latency / Cost)"| DB
    Dashboard <-->|"REST API + Signed Session Cookie"| Backend
    Dashboard -->|"Observability, Traces & Eval Benchmarks"| DB
```

---

## Key Capabilities & Differentiators

- **Zero-Trust Isolated Sandbox:** Untrusted code execution is completely barred from the Lambda orchestrator and backend host. All candidate patches run inside isolated, per-user GitHub Actions mirror repositories (`kaiizer777/haunter-test-{hash}`). *(Note: Retired legacy runners such as AWS CodeBuild, GCP Cloud Build, and Local Docker have been fully superseded by this zero-trust architecture).*
- **Lightweight Git Data API Seeding:** Eliminates heavy `git clone` overhead, container runtimes, and local Git binaries on AWS Lambda. Code snapshots and test harness workflows (`haunter-test-py.yml` / `haunter-test-ts.yml`) are committed directly via GitHub's Git Data API (trees, blobs, and commits).
- **Narrow Context & Ephemeral Subagents:** The core orchestrator maintains state machine metadata (`run_id`, `step`, `confidence`) without log bloat. Ephemeral subagents (Context Gatherer, Fix Generator, PR Writer) receive strictly distilled inputs and operate within hard token budgets.
- **Closed-Loop Retry Feedback:** Failed sandbox verification runs feed sanitized compiler/test error logs back to the Fix Generator to adjust strategies across up to 3 bounded attempts.
- **Live Model & Provider Switcher:** Defaults to OpenCode Zen (`nemotron-3.5-lightning-free` via `https://opencode.ai/zen/v1`), with seamless dynamic runtime switching to OpenAI or Anthropic driven by database configuration (`model_configs`) without redeploying.
- **Integrated Golden Eval Harness & Demo Mode:** Contains a 20-fixture canonical benchmark suite covering import errors, type errors, assertion failures, and missing dependencies. Includes a one-click dashboard **Demo Mode** pinned to `fixture-001` for instant interview or evaluation demonstrations.
- **Full Production Telemetry:** Every subagent execution records exact duration in milliseconds, prompt tokens, completion tokens, and cost estimates directly to Neon Postgres.

---

## Tech Stack

| Layer | Technology | Details |
|---|---|---|
| **Frontend** | Next.js 16, React 19, Tailwind CSS v4, Lucide Icons | Static SPA export (`output: "export"`) deployed on **Cloudflare Workers** via Wrangler static assets (`frontend/wrangler.jsonc`, worker `haunter-ci-agent`). |
| **Backend** | FastAPI, Python 3.11, Mangum | Serverless ASGI orchestrator on **AWS Lambda** Function URL in `us-east-1` (`x86_64`, 512 MB, 900s timeout). |
| **Database** | Neon Serverless Postgres | SQLAlchemy 2.0 Async + `asyncpg` with `NullPool` for pooled connections; unpooled direct connection for Alembic migrations. |
| **Sandbox CI** | GitHub Actions (`GitHubActionsSandboxRunner`) | Automated test mirror provisioning, tarball-based Git Data API tree injection, and REST check-run polling loop (up to 120s). |
| **LLM Inference** | OpenCode Zen API | Default `nemotron-3.5-lightning-free` (`https://opencode.ai/zen/v1`). Dynamic model configuration and provider fallback. |
| **Security & Auth** | GitHub OAuth + Fernet | Cross-origin signed `haunter_session` cookie (`SameSite=None`, `Secure`, `HttpOnly`), Fernet encryption at rest for tokens, HMAC-SHA256 webhook validation. |

---

## Repository Structure

```
Haunter/
├── backend/
│   ├── alembic/                       # Database migrations (asyncpg/Postgres)
│   ├── app/
│   │   ├── auth.py                    # GitHub OAuth, Fernet encryption, signed sessions
│   │   ├── config.py                  # Pydantic Settings & environment validation
│   │   ├── db.py                      # SQLAlchemy async engine & NullPool setup
│   │   ├── github_client.py           # GitHub REST & Git Data API client
│   │   ├── models.py                  # Declarative DB models (Runs, Steps, Evals, etc.)
│   │   ├── orchestrator.py            # Event loop & subagent orchestration state machine
│   │   ├── llm/                       # LLM client & OpenCode Zen adapter
│   │   ├── sandbox/                   # GitHub Actions runner, mirror lifecycle, tarball seeder
│   │   │   ├── github_actions_runner.py
│   │   │   ├── mirror.py
│   │   │   ├── _seed_tarball.py
│   │   │   └── workflow_templates/    # haunter-test-py.yml & haunter-test-ts.yml
│   │   └── subagents/                 # Context Gatherer, Fix Generator, PR Writer
│   ├── tests/                         # Comprehensive pytest suite & eval fixtures
│   ├── lambda_handler.py              # Mangum adapter for AWS Lambda
│   ├── main.py                        # FastAPI application declaration
│   └── rebuild_lambda_zip.py          # Linux x86_64 binary wheel packager for Lambda
├── frontend/                          # Next.js 16 SPA dashboard
│   ├── src/app/                       # App router pages (Runs, Repos, Eval Harness, Settings)
│   ├── src/components/                # Trace timelines, diff viewers, charts
│   ├── next.config.ts                 # Configured with output: "export"
│   └── wrangler.jsonc                 # Cloudflare Workers static asset configuration
├── infra/aws/                         # Terraform definitions (Lambda Function URL, IAM)
├── HAUNTER.md                         # Product specification & architectural design
├── WORK.md                            # Comprehensive engineering implementation log
├── aws.md                             # AWS Lambda deployment & packaging runbook
└── github.md                          # GitHub Actions sandbox runner runbook
```

---

## Local Development & Setup

### 1. Prerequisites
- Python 3.11+
- Node.js 20+ & npm 11+
- Neon Postgres database instance (pooled and unpooled connection URLs)
- GitHub OAuth Application credentials (`GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`)
- GitHub App or Personal Access Token with repo write permissions
- OpenCode Zen API key (`OPENCODE_ZEN_API_KEY`)

### 2. Environment Variables (`backend/.env`)

```env
# Database (Neon Postgres)
DATABASE_URL=postgresql+asyncpg://user:pass@ep-pool.us-east-2.aws.neon.tech/haunter?sslmode=require
DATABASE_URL_UNPOOLED=postgresql+asyncpg://user:pass@ep-direct.us-east-2.aws.neon.tech/haunter?sslmode=require

# Auth & Secrets
GITHUB_CLIENT_ID=your_oauth_client_id
GITHUB_CLIENT_SECRET=your_oauth_client_secret
CALLBACK_URL=http://localhost:8000/auth/github/callback
SESSION_SECRET_KEY=generate_with_openssl_rand_hex_32
TOKEN_ENCRYPTION_KEY=generate_with_fernet_generate_key
FRONTEND_URL=http://localhost:3000

# Webhook & Sandbox
GITHUB_WEBHOOK_SECRET=your_webhook_hmac_secret
SANDBOX_PROVIDER=github_actions
GITHUB_SANDBOX_ORG=kaiizer777
GITHUB_SANDBOX_APP_ID=your_sandbox_app_id
GITHUB_SANDBOX_INSTALLATION_ID=your_sandbox_installation_id
GITHUB_SANDBOX_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n..."

# LLM Inference
OPENCODE_ZEN_API_KEY=your_opencode_zen_key
DEFAULT_PROVIDER=opencode_zen
DEFAULT_MODEL=nemotron-3.5-lightning-free
ADMIN_USER_ID=your_user_uuid
```

### 3. Backend Setup

```bash
cd backend
python -m venv .venv
# Activate virtual environment
source .venv/bin/activate       # Linux/macOS
# .\.venv\Scripts\activate      # Windows

pip install -r requirements.txt

# Run database migrations
alembic upgrade head

# Start development server
uvicorn main:app --reload --port 8000
```

Verify backend health at `http://localhost:8000/health`.

### 4. Frontend Setup

```bash
cd frontend
npm install

# Run local development server
npm run dev
```

Open `http://localhost:3000` to access the dashboard.

### 5. Running Tests

```bash
cd backend
pytest -v
```

---

## 5-Minute Demo Walkthrough (Eval Harness)

The dashboard includes a built-in benchmark harness with a **Demo Mode** designed to showcase Haunter's diagnostic and fix capabilities deterministically:

1. Launch both Backend and Frontend locally, or navigate to the deployed Cloudflare Worker dashboard.
2. Sign in via GitHub OAuth.
3. Open the **Eval Harness** view from the navigation menu.
4. Switch the **Demo mode** toggle to **ON** (persisted in `localStorage`). This pins the test run to `fixture-001` (`test_demo_canonical.py`, containing a canonical import typo).
5. Click **Run Eval Harness** → **Start Benchmark**.
6. Observe the orchestrator invoke the Context Gatherer, identify the missing module, generate the fix, execute verification, and log token usage, cost, and latency in real time.
7. Click the resulting run to inspect the live execution trace and generated patch diff.

---

## Deployment Summary

### Backend: AWS Lambda Function URL
The backend is packaged using native Linux wheels and deployed to AWS Lambda:

1. Package the deployment bundle:
   ```bash
   cd backend
   python rebuild_lambda_zip.py
   ```
2. Apply infrastructure via Terraform:
   ```bash
   cd infra/aws
   terraform init
   terraform plan
   terraform apply
   ```
   *For operational details, SSM parameter references, and troubleshooting, consult [aws.md](file:///C:/Users/bari2/Desktop/Haunter/aws.md).*

### Frontend: Cloudflare Workers Static Assets
The Next.js 16 dashboard compiles to a static SPA export and is served via Cloudflare Workers:

```bash
cd frontend
npm run build
npx wrangler deploy
```
*Worker configuration is specified in [wrangler.jsonc](file:///C:/Users/bari2/Desktop/Haunter/frontend/wrangler.jsonc).*

---

## Primary Documentation Links

- [HAUNTER.md](file:///C:/Users/bari2/Desktop/Haunter/HAUNTER.md): System architecture, subagent contracts, and core requirements.
- [WORK.md](file:///C:/Users/bari2/Desktop/Haunter/WORK.md): Complete chronological record of all implementation phases.
- [aws.md](file:///C:/Users/bari2/Desktop/Haunter/aws.md): AWS Lambda deployment runbook, Terraform configuration, and gotchas.
- [github.md](file:///C:/Users/bari2/Desktop/Haunter/github.md): GitHub Actions sandbox runner implementation and mirror lifecycle.
