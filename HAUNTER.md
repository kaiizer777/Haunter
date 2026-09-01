# HAUNTER — Autonomous CI Failure Diagnosis & Fix Agent

**Status:** MVP scoping locked. Ready for build.
**Owner:** MD Sufiyan Bari
**Goal:** Production-grade, orchestrator/subagent AI system that diagnoses CI failures, fixes them in an isolated sandbox, verifies the fix, and opens a PR — with full observability and eval built in from day one.

---

## 1. What Haunter Does

When a connected repo's CI (GitHub Actions) fails:

1. Webhook fires → Haunter's orchestrator wakes up.
2. Orchestrator delegates to subagents to gather context, diagnose, fix, and verify — without ever holding raw logs/diffs in its own context.
3. If a fix passes verification in an isolated sandbox → Haunter opens a PR with the fix + explanation.
4. If it can't produce a passing fix after retries → Haunter posts a diagnosis-only comment on the commit/PR instead. It never pushes to main and never merges anything itself.
5. Every run (pass, fail, retry, confidence score, token cost, latency) is logged to Postgres and visible on a dashboard.

Haunter is intentionally **not** "autonomous merge to prod." The human stays the merge gate. That boundary is a deliberate design choice, not a limitation — it's the difference between a toy and something a real eng team could actually trust.

---

## 2. Why This Project (Context, Not Fluff)

- The category (AI fixes CI failures) already has commercial players (GitHub Copilot Autofix, Sentry AI, etc.) — this is treated as validation of real demand, not a reason to avoid it. Differentiation comes from the orchestrator/subagent architecture, the mandatory sandbox verification loop, and the eval harness — three things most portfolio/demo projects skip entirely.

---

## 3. MVP Feature Scope (Locked)

### 3.1 Core Pipeline
1. Webhook listener — **multi-repo**, connect several GitHub repos to one Haunter instance.
2. Context-Gatherer subagent → distilled root-cause summary.
3. Fix-Generator subagent → patch + **confidence score**.
4. Sandbox-Verifier (GitHub Actions) → pass/fail in per-user test mirror.
5. **Retry logic** — alternate fix strategy on failure, capped at 2–3 attempts.
6. PR-Writer subagent → opens PR with fix + explanation on pass.
7. Fallback — diagnosis-only comment if all attempts exhausted.
8. Run history in Neon: repo, attempt count, confidence per attempt, pass/fail, diagnosis, timestamps.

### 3.2 Observability (first-class, not an afterthought)
- Structured trace per run: every subagent call logged with input tokens, output tokens, latency, estimated cost.
- Full run timeline visible in dashboard: orchestrator decision → subagent 1 → subagent 2 → ... step by step, not just final result.
- Failure classification: tag *why* a run failed — wrong diagnosis / wrong fix / tests still failing / sandbox error — not just a generic "failed" status.

### 3.3 Eval Harness
- **Golden test set:** 15–20 curated real CI failures (import errors, type errors, failed assertions, dependency issues) with known-correct fixes, run through Haunter to measure accuracy %.
- **Per-subagent eval**, not just end-to-end: does Context Gatherer find the actual root cause? Does Fix Generator's confidence score correlate with real pass rate?
- **Regression tracking:** re-run the golden set after any prompt/strategy change, compare score before/after — prevents silent regressions, and is a strong standalone resume line.

### 3.4 Dashboard (Frontend, Cloudflare Pages)
- List of connected repos (multi-repo).
- Feed/table of all past runs across repos — status, confidence, attempt count, link to resulting PR or comment.
- Per-run expandable trace view (timeline + token/cost breakdown).
- Eval score display (e.g. "73% fix success rate on golden set").
- Confidence-vs-actual-outcome chart (sanity check: does high confidence actually predict success?).
- Model/provider switcher (per §7).
- Auth via GitHub OAuth (FastAPI + authlib, httpOnly session cookie) — dashboard is private, not public. Frontend is pure client.

---

## 4. Explicit Non-Goals (v1)

- No auto-push/auto-merge to `main` — PR-only, human is always the merge gate.
- No custom domain.
- No support for arbitrarily large monorepos in v1 — scope sandbox testing to reasonably-sized repos/test suites to keep sandbox runs fast.
- No fine-tuning — relies entirely on prompting + the orchestrator/subagent architecture + eval harness for quality, not custom model training.

---

## 5. Architecture as of Aug 2026

### 5.1 High-Level Flow

```
GitHub Actions failure
      │
      ▼
GitHub Webhook (workflow_run, conclusion=failure)
      │
      ▼
FastAPI Orchestrator (AWS Lambda) ── holds only decisions/state, no raw logs
      │
      ├──► Subagent: Context Gatherer  ──► returns distilled root-cause summary
      │
      ├──► Subagent: Fix Generator     ──► returns diff + confidence score
      │
      ├──► GitHub Actions: Sandbox Verifier ──► clones test mirror, applies patch,
      │                                         runs workflow, returns pass/fail via polling
      │
      ├──(if fail, attempts < N)──► retry Fix Generator with alternate strategy
      │
      ├──(if pass)──► Subagent: PR Writer ──► opens PR with fix + explanation
      │
      └──(if all attempts exhausted)──► Fallback: diagnosis-only comment on commit

All steps logged to Neon (Postgres) → surfaced on dashboard
```

### 5.2 Orchestrator / Subagent Pattern (Core Design Principle)

The **main agent (orchestrator)** stays alive for the whole run and holds only compact state: repo id, run id, current step, decisions made, confidence scores. It never sees raw CI logs, full diffs, or verbose test output directly.

**Subagents** are spun up per task, given a narrow prompt and only the input they need, and die after returning a compact result:

| Subagent | Input | Output (what orchestrator receives) |
|---|---|---|
| Context Gatherer | raw logs, diff, commit history | distilled root-cause summary (few hundred tokens) |
| Fix Generator | root-cause summary (+ prior failed attempt on retry) | patch/diff + confidence score (0–100) |
| Sandbox Verifier | patch + repo ref | pass/fail + short failure reason (not full test output) |
| PR Writer | verified fix + summary | PR title + description text |

This keeps the orchestrator's context window clean and cheap across a run with multiple retries — a deliberate token/cost optimization, and a legitimate architecture decision to explain in interviews.

**Concurrency rule (final):**
- **Read-only / analysis subagents run concurrently** when there's no data dependency between them — e.g. log analysis + diff analysis + commit history can all be dispatched together (capped at ~3 concurrent, matching the realistic number of independent context sources), since they're side-effect-free and can't conflict. Orchestrator merges their summaries once all return.
- **Mutating / sequential subagents (Fix Generator → Sandbox Verifier → PR Writer) always run strictly sequentially**, one at a time — each depends on the previous step's output (fix depends on diagnosis, verification depends on the fix, PR depends on a verified pass), so there's no real parallelism to gain and no data-race risk to introduce.
- No general concurrent-subagent pool beyond this — the orchestrator plans the run into small/mid-sized steps and delegates one (or a small read-only batch) at a time, rather than fanning out broadly. Keeps runs traceable in the dashboard and debuggable.

### 5.3 GitHub Integration Details

- **Trigger:** `workflow_run` webhook event, filtered to `action: completed` and `conclusion: failure`.
- Payload gives: workflow run id, head branch, head sha, html_url — enough to fetch full logs and diff via REST API afterward.
- **Response requirement:** GitHub expects a 2xx response within **10 seconds** of webhook delivery — Haunter's webhook endpoint must return immediately and process the actual diagnosis/fix pipeline **asynchronously** (background task/queue), not inline in the request handler.
- GitHub may redeliver the same event more than once (network retries) — webhook handler should be **idempotent** (dedupe on delivery id or workflow run id before starting a new pipeline run).
- Verify `X-Hub-Signature-256` HMAC on every incoming webhook using a stored webhook secret — non-negotiable for a public-facing endpoint.
- Use a public repo for the target/demo repo so GitHub Actions minutes are unlimited and free.

### 5.4 Sandbox Execution (GitHub Actions CI)

- **Sandbox Architecture:** Haunter uses **GitHub Actions CI** (`GitHubActionsSandboxRunner`) as its dedicated sandbox runner, executing test workflows in isolated test mirrors. This provides native testing environments, eliminates cloud container quota bottlenecks, and runs within GitHub Actions free-tier minutes.
- **The Test Mirror Pattern:** To prevent modifying the user's real repository, the webhook initiates or fetches an isolated, private per-user test mirror under `kaiizer777` (named `haunter-test-{8-char-hash}`, hashed deterministically from `user_github_id` + salt via `mirror.py`).
- **Why not `haunter-sandboxes` org?** The original plan envisioned a dedicated GitHub organization for sandboxes. However, GitHub App installation permissions on the org required broader scopes than desirable. Sticking to the `kaiizer777` namespace provided tighter control and simpler App governance.
- **Language Templates & Auto-Detection:** Detected from repository file paths (`detect_language()` in `mirror.py`):
  - Python (`haunter-test-py.yml`): Python 3.11, dependency install, best-effort `ruff` / `mypy`, `pytest -q`.
  - TypeScript (`haunter-test-ts.yml`): Node 20, `npm ci`, `tsc --noEmit`, best-effort `eslint`, `npm test`.
- **Workflow Push via PAT Fallback:** Because the GitHub App installation token lacks the sensitive `workflows:write` scope, the runner uses a PAT fallback to push the chosen template into `.github/workflows/` of the test mirror.
- **Patch Application via Git Data API:** The generated patch is applied directly to branch `haunter-attempt-{N}` via GitHub's Git Data API (blobs → trees → commits → refs) without requiring local git binaries or containerized docker daemons.
- **Unidirectional Polling:** To keep network surface area secure and avoid exposing inbound webhook endpoints from test mirrors to Lambda, the runner polls the Actions check-runs and workflow-runs APIs every 10s up to a 120s timeout (`github_sandbox_poll_timeout_seconds`).
- **Sanitized Failure Logs & Fast-Fail:** On build failure, log tails are sanitized of credentials and truncated via `_sanitize_failure_reason`. Deterministic errors (403, permissions, quota limits) are prefixed with `[non-retryable]`, allowing the orchestrator to fast-fail directly to the diagnosis comment fallback without burning attempts.

### 5.5 Sandbox Internals & Provider Registry

- **Provider Registry:** Defined in `backend/app/sandbox/__init__.py`. The `SANDBOX_PROVIDERS` registry maps the active provider (`"github_actions"`) to `GitHubActionsSandboxRunner`. Runner dependencies (`pyjwt`, `httpx`, `boto3`) are lazy-loaded to optimize cold start times.
- **Active Runner:** `SANDBOX_PROVIDER=github_actions` is the sole production setting. Haunter does not retain dormant runner fallbacks.
- **SSM-Backed Authentication:** GitHub App private key PEM is loaded lazily from AWS SSM Parameter Store (`/haunter/GITHUB_SANDBOX_APP_PRIVATE_KEY`) into memory cache (`_PEM_CACHE`). Short-lived GitHub App installation tokens are minted via PyJWT and cached (`_TOKEN_CACHE`) with a 5-minute pre-expiry margin (55 min TTL).
- **Health Check Endpoint:** `GET /health/sandbox` exposes active provider status, configured org/namespace, and App ID.

### 5.6 Retry Logic

- On sandbox verification failure, Fix Generator is invoked again with the prior attempt's failure reason added to its context, prompting an **alternate strategy**, not a repeat of the same fix.
- Capped at **2–3 total attempts** per run to bound cost and runtime (temporarily relaxed in orchestrator during testing).
- Each attempt (pass/fail, confidence, strategy used) is logged individually — visible in the dashboard's per-run timeline.

---

## 6. Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Agent backend / orchestrator | **FastAPI + Mangum** on **AWS Lambda** | Scales to zero, Lambda Function URL (no API Gateway needed — $0), ARM64 / 512MB / 900s timeout, 1M req + 400k GB-s/mo always-free |
| Sandbox execution | **GitHub Actions** (`github_actions_runner`) | Per-user test mirrors on `kaiizer777`. |
| Database | **Neon (Postgres)** | Use **pooled connection string** (`-pooler` host) for app queries via SQLAlchemy with `NullPool` (don't double-pool — Neon's PgBouncer already handles it); use the **direct/unpooled** connection string only for schema migrations |
| Auth | **GitHub OAuth via FastAPI (authlib + itsdangerous)** | FastAPI owns OAuth flow (`/auth/login`, `/auth/callback`, `/auth/logout`, `/auth/me`), `users` table in Neon via Alembic, signed httpOnly SameSite=Lax cookie (14d, `itsdangerous` TimestampSigner), `get_current_user` dependency; separate OAuth App for login (`read:user`) vs GitHub App for repo/webhooks (Phase 3) |
| Repo integration | **GitHub REST API + Webhooks + Actions** | Use a **public repo** → unlimited free Actions minutes; webhooks are free/unlimited; REST API gives 5,000 req/hour authenticated |
| Secrets & Config | **AWS SSM / Lambda Environment Variables** | `DATABASE_URL`, `DATABASE_URL_UNPOOLED`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `SESSION_SECRET_KEY`, `FRONTEND_URL`, `OPENCODE_ZEN_API_KEY`, `GITHUB_WEBHOOK_SECRET`, `TOKEN_ENCRYPTION_KEY` — mounted as env vars at deploy, never committed |
| CI/CD for Haunter itself | **GitHub Actions** | Unlimited free minutes on public repos |
| Frontend / dashboard | **Cloudflare Pages** (Next.js 16 App Router) | Free tier, no domain required, calls Lambda Function URL directly via `NEXT_PUBLIC_API_URL` |
| LLM provider | **OpenCode Zen** (OpenAI-compatible endpoint) | Base URL: `https://opencode.ai/zen/v1`; default model: **Nemotron 3.5 Lightning Free (`nemotron-3.5-lightning-free`)** — see §7 |

**No custom domain required anywhere.** AWS Lambda's Function URL (`*.lambda-url.<region>.on.aws`) and Cloudflare's `*.pages.dev` URLs are sufficient for the full MVP and for demoing in interviews. A domain is a purely cosmetic, optional later addition (~$10/yr).

**Cost at scale:** all of the above comfortably covers <50 users, realistically into the hundreds, at $0 total cost.

---

## 7. Model & Provider Configuration

- **Default provider:** OpenCode Zen, OpenAI-compatible API.
  - Base URL: `https://opencode.ai/zen/v1`
- **Default model (dev + v1):** `nemotron-3.5-lightning-free` (free tier via OpenCode Zen / NVIDIA free endpoints).
  - Note: NVIDIA's free endpoint logs requests for trial/improvement purposes — **do not send real user data, credentials, or private repo contents through it while on the free tier.** Fine for Haunter's own dev/testing and for public-repo CI logs, but worth stating explicitly in the docs/README for transparency.
  - Context window: 1M tokens; supports tool calling — fits the subagent tool-use pattern directly.
- **Model/provider must be swappable at runtime, not hardcoded.** Build a thin provider abstraction (single interface, e.g. `LLMClient.complete(...)`) so orchestrator and every subagent call through one client, and the client reads active model/provider config from Neon (or env fallback), not from code.
- **UI requirement:** dashboard must include a **model/provider switcher** — lets you (or a future user) pick provider (OpenCode Zen, OpenAI-compatible custom endpoint, etc.) and model per-repo or globally, stored in Neon, applied live without redeploy.
- Rationale: free-tier models on OpenCode Zen are explicitly **limited-time offers** (confirmed — Nemotron 3.5 Lightning, Big Pickle, MiMo-V2.5, etc. are all "free for a limited time" while OpenCode collects feedback). Hardcoding one model/provider is a single point of failure the moment that offer ends. The switcher is a resilience feature, not just a nice-to-have — and it's also a good resume talking point (multi-provider abstraction, no vendor lock-in).

---

## 8. Deployment Notes

- **Backend (AWS Lambda):** Deployed via Terraform (`infra/aws/lambda.tf`). Configured with ARM64 architecture, 512MB RAM, and 900s timeout. Uses Lambda Function URL (`auth_type = "NONE"`, secured via HMAC webhook signatures and session auth). Employs asynchronous self-invocation via `boto3` (`AWSHostingAdapter`) to immediately return 202 to GitHub webhooks within the 10-second contract while orchestrator runs in background.
- **Sandbox Execution (GitHub Actions CI):** Production sandbox runner (`SANDBOX_PROVIDER=github_actions`). Uses per-user mirror repos on `kaiizer777` with AWS SSM-backed GitHub App credentials and PAT template delivery. All legacy sandbox infrastructure has been decommissioned.
- **Frontend (Cloudflare Pages):** Next.js 16 App Router dashboard deployed on Cloudflare Pages, calling backend via `NEXT_PUBLIC_API_URL`.
- **Cost safety:** Set a $1 budget alert in AWS Budgets. Always-free tier covers 1M Lambda invocations.

---

## 9. Why This Is Resume/Interview-Grade

- Orchestrator/subagent architecture demonstrates real context-engineering and token-cost awareness, not just "call an LLM."
- Mandatory sandbox verification loop is the difference between a real agent and a shallow "AI suggests a fix" demo — this alone is a strong technical story.
- Golden eval set + per-subagent accuracy tracking + regression detection is rare at any experience level, let alone intern-level — this is the single highest-signal part of the project.
- Full production stack proves Python backend competence and cloud-native serverless deployment — directly closing the two gaps identified before this project was scoped.
- Multi-provider LLM abstraction with a live-swappable model/provider config shows awareness of vendor lock-in and production resilience, not just "wire up one API key and ship."

## 10. Limitations & Future Work (Pending)

- **Multi-language expansion:** Currently scoped tightly; pending addition of Go, Rust, and Java test workflows.
- **Test mirror cleanup:** A cron job is needed to automatically delete old `haunter-test-*` mirror repos to keep the `kaiizer777` namespace clean.
- **Dynamic CI parsing:** Currently relying on template overrides; pending feature to use the user's actual CI config by parsing `.github/workflows/*.yml` from the real repo.

---

## Sandbox providers

Haunter strictly supports **GitHub Actions CI** (`github_actions_runner`) as its sole sandbox verification engine. Verification workloads execute in isolated, per-user test mirror repositories (`kaiizer777/haunter-test-{hash}`) using polled GitHub Actions check-runs. All legacy secondary runners have been decommissioned to maintain a lean, single-provider architecture (see `cleanup.md`).[^sandbox-history]

[^sandbox-history]: Haunter originally supported AWS CodeBuild and GCP Cloud Build as sandbox runners. CodeBuild was blocked by account-level concurrent-build quotas (`AccountLimitExceededException` in `us-east-1`), and GCP was never provisioned. Both dormant implementations were dropped in Phase 17.

---

## Hosting providers

Haunter is architected exclusively for **AWS Lambda** via Function URL and Mangum. The orchestrator handles incoming GitHub webhooks via `AWSHostingAdapter`, immediately returning an HTTP 202 acknowledgment within the 10-second contract while delegating the long-running execution pipeline to an asynchronous Lambda self-invocation via `boto3`. Haunter runs entirely serverless on AWS Lambda with zero background container hosting dependencies.

---

## Build phases

### Phase 17 (Cleanup)
Post-MVP codebase cleanup reducing Haunter to a single-provider, single-hosting system. Removed dormant GCP Cloud Build and AWS CodeBuild sandbox runners, dropped the unused GCP Cloud Run hosting adapter, eliminated the `google-cloud-build` dependency (saving ~30MB in `lambda.zip`), and narrowed config schemas to active providers (`github_actions` and `aws`). See `cleanup.md` for the full 5-session execution plan.
