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

---

Haunter is an enterprise-grade autonomous CI failure remediation engine, security auditor bot, and cloud agentic pairing studio. 

When a GitHub Actions workflow fails, Haunter wakes autonomously via webhooks, distills failure context, determines the root cause, generates candidate patches, verifies fixes inside an isolated GitHub Actions sandbox mirror, and opens an auditable Pull Request with complete diagnostic rationale — or leaves a structured root-cause diagnostic comment if retries are exhausted.

Beyond reactive CI repair, Haunter features **Auditor Mode** (an autonomous, read-only AI Staff Engineer & Security Reviewer bot), **Cloud Agentic Live Sessions** (an interactive in-browser development environment powered by StackBlitz WebContainer and Monaco Diff Editor), and **Granular Per-Repository Governance** with 1-click policy presets.

> **The Human Merge Gate Invariant:** Haunter operates strictly as an autonomous contributor, never an autonomous administrator. It **never** auto-merges or pushes directly to default branches. Developers remain the final merge authority.

---

## Architecture Overview

```mermaid
flowchart TD
    subgraph GitHub ["GitHub Platform"]
        GHA["GitHub Actions CI Failure / PR Event"]
        PR["Target Repo: Pull Request or Comment"]
        TestMirror["Isolated Sandbox Mirror Repo\n(kaiizer777/haunter-test-{hash})"]
    end

    subgraph Backend ["AWS Lambda (us-east-1, x86_64) — FastAPI + Mangum"]
        Webhook["POST /webhooks/github\nHMAC-SHA256 + Delivery Deduplication"]
        AsyncSelf["Async Self-Invocation\n(AWSHostingAdapter via boto3)"]
        AuditWorkers["Background Dispatch Workers\n(asyncio consumer queue)"]

        subgraph FixPipeline ["Autonomous CI Repair Pipeline"]
            CG["1. Context Gatherer\n(Distills log trace & failing diff)"]
            FG["2. Fix Generator\n(Produces patch + confidence score)"]
            SB["3. Sandbox Verifier\n(GitHubActionsSandboxRunner)"]
            PW["4. PR Writer / Fallback\n(Creates PR or diagnostic comment)"]
        end

        subgraph AuditorBot ["Auditor Subagent & Multi-Perspective Review"]
            Auditor["Multi-Perspective Auditor\n(Security, Architecture, Regression, Perf)"]
            Publisher["GitHub Audit Publisher\n(PR Review Comments & Summaries)"]
        end

        subgraph LiveStudio ["Cloud Agentic Live Sessions Engine"]
            SessOrch["Session Orchestrator & Tool Dispatcher\n(Surgical Edits, Recon, Git, Web Docs)"]
            Subagents["Specialized Subagents\n(RepoNavigator, FeatureArchitect, BugHunter)"]
            SSE["SseQueue Streamer\n(Real-Time SSE Protocol)"]
        end
    end

    subgraph Storage ["Neon Serverless Postgres"]
        DB[(Neon Database\nRuns · Steps · Evals · ModelConfigs · Sessions · Settings)]
    end

    subgraph FrontendApp ["Cloudflare Workers Static Assets"]
        Dashboard["Next.js 16 SPA Dashboard\n(Cross-Origin Cookie Session)"]
        WebContainer["StackBlitz WebContainer API\n(In-Browser Node/Next.js Dev Server)"]
        Monaco["Monaco Diff Editor\n(Visual Patch Inspection)"]
    end

    GHA -->|"workflow_run / pull_request / comment"| Webhook
    Webhook -->|"200 Ack (<1s)"| GHA
    Webhook -.->|"Self-Invoke Async"| AsyncSelf
    Webhook -.->|"Queue Audit Task"| AuditWorkers

    AsyncSelf --> CG --> FG --> SB
    SB <-->|"Git Data API Seeding\n& Check-Runs Polling"| TestMirror
    SB -->|"Pass (Confidence >= 30)"| PW
    SB -.->|"Fail (Retry <= 3)"| FG
    PW -->|"Verified Fix PR"| PR
    PW -.->|"Attempts Exhausted Comment"| PR

    AuditWorkers --> Auditor --> Publisher -->|"Audit Report / Inline Findings"| PR

    Dashboard <-->|"REST API + SSE Stream"| Backend
    Dashboard --- WebContainer
    Dashboard --- Monaco
    Backend <--> DB
```

---

## Core Capabilities & Feature Matrix

### 1. Autonomous CI Failure Diagnosis & Repair Loop
- **Zero-Trust Isolated Sandbox:** Untrusted code execution is strictly barred from the Lambda orchestrator and backend host. All candidate patches run inside isolated, ephemeral GitHub Actions mirror repositories (`kaiizer777/haunter-test-{hash}`).
- **Lightweight Git Data API Seeding:** Eliminates heavy `git clone` overhead, container runtimes, and local Git binaries on AWS Lambda. Code snapshots and test harness workflows (`haunter-test-py.yml` / `haunter-test-ts.yml`) are committed directly via GitHub's Git Data API (trees, blobs, and commits).
- **Closed-Loop Retry Feedback:** Failed verification runs feed sanitized compiler/test error logs back to the Fix Generator to iteratively adjust strategies across up to 3 bounded attempts.
- **Flakiness Detection:** Re-runs failed tests against clean baseline SHAs across isolated iterations to detect and flag flaky tests automatically.

### 2. Autonomous Read-Only Auditor Bot ("Auditor Mode")
- **Zero-Mutation Guarantee:** Operates strictly in read-only mode — never creates branches, modifies files, or opens unsolicited PRs.
- **Granular Triggers:** Configurable per repository to activate on:
  - `on_pull_request`: Review incoming PR diffs before merge.
  - `on_ci_failure`: Detailed diagnosis on red builds without touching code.
  - `on_ci_success`: Post-build sanity and regression verification on green builds.
  - `on_manual_mention`: On-demand ad-hoc code audits triggered by `@haunter audit` comments.
- **Multi-Perspective Review Core:** Analyzes code across 4 distinct perspectives:
  1. *Security*: OWASP API Top 10, SSRF, injection vectors, timing leaks, hardcoded credentials.
  2. *Architecture & Concurrency*: Race conditions, connection leaks, state mutation anti-patterns.
  3. *Regressions & Edge Cases*: Off-by-one errors, boundary conditions, unhandled nulls.
  4. *Performance*: N+1 queries, unindexed filters, blocking operations in async loops.
- **Studio-Grade Reports:** Emits executive summaries, radial health scores (0–100), filterable findings by severity (`Blocker`, `Warning`, `Note`), and unified surgical diff previews.

### 3. Cloud Agentic Live Pairing Sessions (`/sessions`)
- **Interactive In-Browser Workspace (`/sessions/workspace`):** A modern pair-programming studio matching Claude/Cursor UX with thought duration accordions, grouped tool execution chips, and responsive split views.
- **StackBlitz WebContainer Integration:** Boots a real Node.js runtime inside the browser via `@webcontainer/api`, mounts repository trees, installs dependencies, and runs dev servers (`npm run dev`) with instant hot-module reloading and responsive viewports (Desktop, Tablet, Mobile).
- **Monaco Diff Editor:** Embedded `@monaco-editor/react` Diff Editor for side-by-side patch inspection and verification before committing.
- **Comprehensive Agent Toolset:**
  - *Surgical Editing:* `str_replace`, `create_file`, `delete_file`, `apply_multi_patch` (atomic multi-file edits).
  - *Codebase Reconnaissance:* `grep_search`, `glob_files`, `read_file_slice`, `symbol_definitions`, `find_references`.
  - *Git Provenance:* `git_log`, `git_blame`, `git_show`, `git_diff` via GitHub API without local git binaries.
  - *Live Web Intelligence:* `live_web_search`, `fetch_web_content`, `inspect_live_docs` for up-to-date documentation.
  - *Autonomous CI Verification:* `verify_in_ci_sandbox` dispatches staged patches directly to GitHub Actions sandbox mirrors with real-time SSE streaming.
  - *In-Studio Slash Commands:* `/security-scan` and `/repo-audit` with 1-click **"Stage Surgical Fix"** buttons.
  - *Specialized Subagents:* `repo_navigator`, `feature_architect`, `bug_hunter`, `sandbox_verifier`, `security_auditor`.
  - *Time Travel & Checkpoints:* `checkpoint_create` and `checkpoint_restore` to snapshot and revert session states safely.
  - *Human-in-the-Loop Clarification:* `ask_user_clarification` pauses execution and presents clickable choice pills in the UI when encountering architectural trade-offs.

### 4. Granular Per-Repository Governance Dashboard (`/settings`)
- **1-Click Policy Presets:**
  - `autonomous`: Full autonomous CI repair + code review + PR creation.
  - `conservative`: High-assurance mode requiring 90%+ confidence thresholds.
  - `standard`: Balanced dual-engine repair and code review.
  - `audit_only`: Read-only security auditor bot with zero automated code changes.
  - `live_studio_only`: Dedicated to in-browser WebContainer live pairing.
  - `custom`: Granular toggle-by-toggle control.
- **Operational Boundaries:** Allowed branch regex enforcement, draft PR exclusions, maximum spend caps in cents, and custom confidence thresholds.

### 5. Multi-Model LLM Engine & Dynamic Provider Switcher
- **Pluggable Model Architecture:** Defaults to OpenCode Zen (`nemotron-3.5-lightning-free`), with dynamic runtime switching across OpenAI (`gpt-4o`, `gpt-4o-mini`), Anthropic (`claude-3-5-sonnet`), and free discovery models.
- **Enterprise Prompt Hygiene:** Strict role alternation enforcement (`system` → `user` → `assistant`), empty-response retries, and automated secret scrubbing before tokens hit LLM providers.

### 6. 20-Fixture Golden Eval Benchmark Harness (`/eval`)
- Canonical benchmark covering syntax errors, missing dependencies, import typos, type mismatches, and logic regressions.
- **One-Click Demo Mode:** Pinned to `fixture-001` for deterministic interview and evaluation demonstrations.

---

## Tech Stack

| Layer | Technology | Details |
|---|---|---|
| **Frontend** | Next.js 16.3.3, React 19.2.8, Tailwind CSS v4 | Static SPA export (`output: "export"`) deployed on **Cloudflare Workers** Static Assets (`frontend/wrangler.jsonc`, worker `haunter-ci-agent`). |
| **Browser Runtime** | StackBlitz WebContainer API (`@webcontainer/api`) | Browser-based Node.js virtual container with cross-origin isolation (COOP: `same-origin`, COEP: `require-corp`). |
| **Code Editor** | Monaco Editor (`@monaco-editor/react`) | Interactive side-by-side unified diff inspection. |
| **Backend** | FastAPI, Python 3.11, Mangum | Serverless ASGI orchestrator on **AWS Lambda** Function URL in `us-east-1` (`x86_64`, 512 MB, 900s timeout). |
| **Database** | Neon Serverless Postgres | SQLAlchemy 2.0 Async + `asyncpg` with `NullPool` for pooled connections; unpooled direct connection for Alembic migrations. |
| **Sandbox CI** | GitHub Actions (`GitHubActionsSandboxRunner`) | Ephemeral test mirror provisioning, tarball Git Data API tree injection, and REST check-run polling loop (up to 120s). |
| **LLM Inference** | Multi-Model Architecture | OpenCode Zen (`https://opencode.ai/zen/v1`), OpenAI, Anthropic, with dynamic database-driven model switcher (`model_configs`). |
| **Security & Auth** | GitHub OAuth + Fernet | Cross-origin signed `haunter_session` cookie (`SameSite=None`, `Secure`, `HttpOnly`), Fernet token encryption at rest, HMAC-SHA256 webhook validation. |

---

## Repository Structure

```
Haunter/
├── backend/
│   ├── alembic/                       # Database migrations (asyncpg/Postgres)
│   │   └── versions/                  # Schema revisions (Audit state, repo settings, checkpoints)
│   ├── app/
│   │   ├── adapters/                  # Hosting adapters (AWS Lambda self-invocation)
│   │   ├── auth.py                    # GitHub OAuth, Fernet encryption, signed session cookies
│   │   ├── config.py                  # Pydantic Settings & environment validation
│   │   ├── db.py                      # SQLAlchemy async engine & NullPool setup
│   │   ├── github/                    # GitHub audit publisher & auditor integrations
│   │   ├── github_client.py           # GitHub REST & Git Data API client
│   │   ├── llm/                       # LLM client & multi-model adapters (Zen, OpenAI, Anthropic)
│   │   ├── models.py                  # SQLAlchemy declarative models (Runs, Sessions, Settings)
│   │   ├── orchestrator.py            # Event loop & subagent orchestration state machine
│   │   ├── routers/                   # API routers (sessions, settings, traces, model_config, eval)
│   │   ├── sandbox/                   # GitHub Actions runner, mirror lifecycle, tarball seeder
│   │   │   ├── github_actions_runner.py
│   │   │   ├── mirror.py
│   │   │   └── workflow_templates/    # haunter-test-py.yml & haunter-test-ts.yml
│   │   ├── services/                  # Business services
│   │   │   ├── audit_pipeline.py      # Background audit queue & dispatch workers
│   │   │   ├── repo_settings.py       # Governance presets & policy validation
│   │   │   ├── session_orchestrator.py # Live pairing session agent loop & tool dispatcher
│   │   │   ├── session_streamer.py    # Server-Sent Events (SSE) queue
│   │   │   └── session_tools/         # Surgical editor, git, sandbox, recon, web, audit tools
│   │   ├── subagents/                 # Subagents: Context Gatherer, Fix Generator, PR Writer, Auditor
│   │   └── webhooks.py                # GitHub webhook ingestion with HMAC-SHA256 & dedup
│   ├── tests/                         # Comprehensive pytest test suite (100% passing)
│   ├── audit_dispatcher_handler.py    # Dedicated Lambda handler for async audit dispatch
│   ├── lambda_handler.py              # Mangum adapter for AWS Lambda Function URL
│   ├── main.py                        # FastAPI application declaration & startup lifespan
│   └── rebuild_lambda_zip.py          # Linux x86_64 binary wheel packager for Lambda
├── frontend/                          # Next.js 16 SPA dashboard
│   ├── public/                        # Static assets & _headers (COOP/COEP isolation rules)
│   ├── src/app/                       # App router pages
│   │   ├── sessions/                  # Live pairing sessions dashboard & workspace
│   │   ├── settings/                  # Repository governance & policy surface
│   │   ├── runs/                      # CI failure run explorer & execution traces
│   │   ├── repos/                     # Repository manager
│   │   ├── eval/                      # 20-fixture golden benchmark harness
│   │   └── config/                    # Live model & provider switcher
│   ├── src/components/
│   │   ├── workspace/                 # WebPreviewPanel, AuditReportCard, Monaco Diff Editor
│   │   ├── settings/                  # RepoSettingsCard with 1-click governance presets
│   │   └── trace/                     # Run trace timelines, cost breakdown, token counters
│   ├── src/hooks/                     # Custom React hooks (useWebContainer, useSessionStream)
│   ├── next.config.ts                 # Configured with output: "export" & dev COOP/COEP headers
│   └── wrangler.jsonc                 # Cloudflare Workers static asset configuration
├── infra/aws/                         # Terraform definitions (Lambda Function URL, IAM, SSM)
├── HAUNTER.md                         # Exhaustive technical specification & architecture bible
├── WORK.md                            # Comprehensive engineering implementation log
├── future02.md                        # Auditor Mode & Per-Repo Governance specification
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
- OpenCode Zen API key (`OPENCODE_ZEN_API_KEY`) or OpenAI / Anthropic keys

### 2. Environment Variables (`backend/.env`)

```env
# Database (Neon Postgres)
DATABASE_URL=postgresql+asyncpg://user:pass@ep-pool.us-east-2.aws.neon.tech/haunter?sslmode=require
DATABASE_URL_UNPOOLED=postgresql+asyncpg://user:pass@ep-direct.us-east-2.aws.neon.tech/haunter?sslmode=require

# Auth & Secrets
GITHUB_CLIENT_ID=your_oauth_client_id
GITHUB_CLIENT_SECRET=your_oauth_client_secret
CALLBACK_URL=http://localhost:7555/auth/github/callback
SESSION_SECRET_KEY=generate_with_openssl_rand_hex_32
TOKEN_ENCRYPTION_KEY=generate_with_fernet_generate_key
FRONTEND_URL=http://localhost:3011

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
# Windows:
.\.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt

# Install development dependencies (linters, formatters, pytest-timeout).
# Required for local `pytest -v` so the `timeout = 120` setting in pytest.ini
# is actually enforced; CI installs these automatically.
pip install -r requirements-dev.txt  # for tests, linters, formatters

# Run database migrations
alembic upgrade head

# Start development server on port 7555
python -m uvicorn main:app --reload --port 7555
```

Verify backend health at [http://127.0.0.1:7555/health](http://127.0.0.1:7555/health).

### 4. Frontend Setup

```bash
cd frontend
npm install

# Start Next.js Turbopack dev server on port 3011
npm run dev
```

Open [http://localhost:3011](http://localhost:3011) to access the dashboard.

### 5. Running Tests

```bash
# Backend pytest suite
# Prereq: from `backend/`, `pip install -r requirements.txt -r requirements-dev.txt`
# (requirements-dev.txt ships pytest-timeout==2.4.0, which enforces pytest.ini's
# `timeout = 120` and prevents a single hung test from stalling the whole run.)
cd backend
pytest -v

# Frontend vitest suite (32 suites, 276 tests)
cd frontend
npm test
```

---

## 5-Minute Demo Walkthrough (Eval Harness)

The dashboard includes a built-in benchmark harness with a **Demo Mode** designed to showcase Haunter's diagnostic and fix capabilities deterministically:

1. Launch both Backend and Frontend locally, or navigate to the deployed Cloudflare Worker dashboard.
2. Sign in via GitHub OAuth.
3. Open the **Eval Harness** view from the navigation menu (`/eval`).
4. Switch the **Demo mode** toggle to **ON** (persisted in `localStorage`). This pins the test run to `fixture-001` (`test_demo_canonical.py`, containing a canonical import typo).
5. Click **Run Eval Harness** → **Start Benchmark**.
6. Observe the orchestrator invoke the Context Gatherer, identify the missing module, generate the fix, execute verification inside the sandbox runner, and log token usage, cost, and latency in real time.
7. Click the resulting run to inspect the live execution trace and generated patch diff.

---

## Deployment Summary

### Backend: AWS Lambda Function URL
The backend is packaged using native Linux x86_64 wheels and deployed to AWS Lambda:

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
The Next.js 16 dashboard compiles to a static SPA export and is served via Cloudflare Workers Static Assets:

```bash
cd frontend
npm run build
npx wrangler deploy
```
*Worker configuration is specified in [wrangler.jsonc](file:///C:/Users/bari2/Desktop/Haunter/frontend/wrangler.jsonc).*

---

## Primary Documentation Links

- [HAUNTER.md](file:///C:/Users/bari2/Desktop/Haunter/HAUNTER.md): Comprehensive architectural specification, subagent contracts, and system invariants.
- [WORK.md](file:///C:/Users/bari2/Desktop/Haunter/WORK.md): Complete chronological record of all implementation phases.
- [future02.md](file:///C:/Users/bari2/Desktop/Haunter/future02.md): Specification for Auditor Mode, per-repo settings, and slash commands.
- [aws.md](file:///C:/Users/bari2/Desktop/Haunter/aws.md): AWS Lambda deployment runbook, Terraform configuration, and gotchas.
- [github.md](file:///C:/Users/bari2/Desktop/Haunter/github.md): GitHub Actions sandbox runner implementation and mirror lifecycle.

---

## License

Haunter is licensed under the **Business Source License 1.1** (`BUSL-1.1`) — a source-available license, not an OSI-approved open source license.

Copyright © 2026 MD Sufiyan Bari.

### What this means in practice

**You may:**
- Read, study, and audit the entire codebase — it is public and transparent by design.
- Fork it, modify it, and run it for yourself, including in production.
- Use it internally within your own organization or for evaluation and research.

**You may not, without purchasing a commercial license from the Licensor:**
- Host, operate, or resell Haunter as a service.
- Offer it as a managed, white-labelled, or rebranded product.
- Make it available to third parties as part of any product, platform, or
  service — whether or not Haunter is the primary component, and whether or not
  the portion containing it is separately identified, priced, enabled, or
  marketed. Bundling, embedding, or reselling it as part of a larger offering is
  covered even if you never operate it on the purchaser's behalf.

Publishing a modified copy as a public source fork is permitted redistribution. The restriction attaches to offering Haunter, or a derivative of it, as a product, platform, or service to third parties. The Competitive Offering definition in the `LICENSE` file is the controlling text; it also reaches substantially similar offerings and the offering of any individual Haunter feature or capability.

### Conversion

For each version, the rights change on **2030-09-27**, or on the fourth anniversary of that version's first publicly available distribution, whichever comes first, under the **GNU General Public License v3.0 or later**.

This Change Date governs the versions licensed under the current `LICENSE` file. A later version carries a different Change Date only if the Licensor specifies one for it; if these terms are reused unchanged, the existing date continues to apply.

After conversion, use is governed by the GPL's own terms. The GPL's source-sharing obligations are triggered by distributing or conveying the software, not by private internal use — modifications kept inside your own organization carry no publication requirement.

Until the Change Date, commercial licensing is available directly from the Licensor.

### Why this license

The source is published in full so it can be read, verified, and trusted. The commercial terms exist so that the work cannot simply be taken and resold as someone else's product.

Full license text: [LICENSE](LICENSE)

