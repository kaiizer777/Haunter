# FIN — v1 Build Work Plan

Excludes deployment (separate later phase). Project init assumed done. Each phase sized for one focused coding session with a 1M-context agent.

---

## Phase 1 — Database Schema & Models
- [x] Design Postgres schema in SQLAlchemy (async, SQLAlchemy 2.0 style): `repos`, `runs`, `run_steps` (subagent trace log), `attempts` (fix attempts within a run), `eval_results`, `model_configs` (provider/model switcher) — app-owned tables only (Alembic-managed). Better Auth's own tables (`user`/`session`/`account`/`verification`) are created/migrated from the Next.js/Better Auth side, not via Alembic
- [x] Define relationships: repo → runs → attempts/steps, run → eval_result
- [x] Set up Alembic for migrations
- [x] Configure two DB connections: pooled (app runtime, `NullPool` on SQLAlchemy side since Neon PgBouncer pools already) and direct/unpooled (migrations only)
- [x] Write initial migration and verify it applies cleanly against a fresh Neon DB
- [x] Add simple DB session dependency (`get_db`) for FastAPI routes

**Exit criteria:** tables exist in Neon, migrations run cleanly, can insert/query a dummy row via a scratch script.

---

## Phase 2 — Auth (Better Auth) & Repo Management API
- [ ] Better Auth runs natively on the Next.js frontend (Vercel) via Next.js API routes — it owns its own auth tables in Neon and issues JWTs (via Better Auth `jwt` plugin)
- [ ] FastAPI auth via JWT verification dependency: fetch/cache JWKS from Better Auth JWKS endpoint, verify JWT signature (RS256/EdDSA), validate `iss`/`aud`/`exp` claims, extract user identity — protect routes via this dependency (no session cookie check in FastAPI)
- [ ] CRUD endpoints: add repo (owner/name, GitHub install info), list connected repos, remove repo
- [ ] Store per-repo config: default branch, language hint (optional), active model/provider override
- [ ] Basic request/response Pydantic schemas for repo + run objects (used by both this phase and later ones)

**Exit criteria:** can register/login, and via API add/list/remove a connected repo, persisted in Neon, auth-gated.

---

## Phase 3 — GitHub Webhook Ingestion
- [ ] `POST /webhooks/github` endpoint accepting `workflow_run` events
- [ ] Verify `X-Hub-Signature-256` HMAC against stored webhook secret; reject on mismatch
- [ ] Filter to `action: completed` + `conclusion: failure`; ignore everything else with fast 200
- [ ] Idempotency: dedupe on GitHub delivery id (or workflow run id) before creating a new `run` row — store seen delivery ids or check existing run status
- [ ] On valid new failure: create `run` row (status=`pending`), return 2xx immediately, kick off pipeline via FastAPI `BackgroundTasks`
- [ ] Stub pipeline entrypoint function (logs "pipeline started for run X") — actual orchestrator logic comes in later phases
- [ ] GitHub REST API client wrapper: fetch workflow run logs, diff, commit metadata (needed by next phase, build the client now)

**Exit criteria:** pushing a failing workflow_run payload (simulated via curl/Postman with valid signature) creates a `run` row, responds fast, dedupes on retry, logs pipeline stub trigger.

---

## Phase 4 — LLM Provider Abstraction
- [ ] Build `LLMClient` interface: single `.complete(messages, tools=None, ...)` method, provider-agnostic
- [ ] Implement OpenCode Zen adapter (OpenAI-compatible, base URL `https://opencode.ai/zen/v1`, default model `nemotron-3.5-lightning-free`)
- [ ] Read active provider/model from `model_configs` table (DB-driven, not hardcoded), fall back to env var if DB empty
- [ ] Support tool-calling passthrough (needed for subagents that call tools, e.g. sandbox trigger)
- [ ] Add simple retry/error handling wrapper (timeouts, rate limit backoff)
- [ ] Endpoint: `GET/PUT /config/model` to read/update active model+provider (per-repo or global) — backs the future dashboard switcher
- [ ] Token usage + latency captured on every `.complete()` call, returned alongside response (used for observability logging starting Phase 5)

**Exit criteria:** a scratch script can call `LLMClient.complete()`, get a real response from Nemotron via OpenCode Zen, and switching the DB config to a different model string changes which model answers — no code redeploy needed.

---

## Phase 5 — Orchestrator Skeleton + Context Gatherer Subagent
- [ ] Orchestrator class/function: holds only compact run state (run id, repo id, step, decisions, confidence) — never raw logs
- [ ] Orchestrator drives a simple state machine: `context_gathering → fix_generation → verification → pr_or_fallback`
- [ ] Implement Context Gatherer subagent: takes raw logs + diff + commit history (from Phase 3's GitHub client) as narrow input, returns distilled root-cause summary (few hundred tokens) via `LLMClient`
- [ ] Concurrency: if dispatching log analysis + diff analysis + commit history as separate sub-calls, run them concurrently (asyncio.gather, capped ~3), merge into one summary
- [ ] Persist each subagent call as a `run_steps` row: input tokens, output tokens, latency, cost estimate (from Phase 4 usage data)
- [ ] Wire into Phase 3's pipeline stub: webhook → orchestrator → context gatherer → summary logged to DB, run status updated

**Exit criteria:** a real (or simulated) CI failure payload flows through webhook → orchestrator → Context Gatherer → a root-cause summary is persisted and visible via a DB query, with full token/latency trace in `run_steps`.

---

## Phase 6 — Fix Generator Subagent + Confidence Scoring
- [ ] Implement Fix Generator subagent: takes root-cause summary (+ prior failed attempt on retry) as input, returns unified diff/patch + confidence score (0–100) via `LLMClient`
- [ ] Define strict output schema (JSON mode or structured prompt) so patch + confidence are reliably parseable
- [ ] Persist each generation as an `attempts` row: attempt number, strategy notes, patch text, confidence score, timestamp
- [ ] Orchestrator step: after Context Gatherer, call Fix Generator, log attempt, advance state to `pending_verification`
- [ ] Basic patch sanity validation (valid diff format, non-empty) before handing to sandbox step

**Exit criteria:** given a logged root-cause summary, Fix Generator produces a parseable patch + confidence score, stored as an `attempts` row, orchestrator state advances correctly.

---

## Phase 7 — Sandbox Verifier (Cloud Build Integration)
- [ ] Cloud Build job definition (build config): clone repo fresh, detect language/runtime (or use repo's own Dockerfile if present), install deps, apply patch, run test suite
- [ ] Python `google-cloud-build` client wrapper in orchestrator: trigger build via `cloudbuild_v1.CloudBuildClient` (`create_build` or `run_build_trigger`), pass repo ref + patch as substitution/input
- [ ] Poll build status (or use build completion callback) until pass/fail; extract short failure reason (not full raw output) on failure
- [ ] Persist verification result on the `attempts` row: pass/fail, failure reason, build duration
- [ ] Orchestrator retry logic: on fail, if attempts < cap (2–3), loop back to Fix Generator with failure reason injected into context (alternate strategy); on exhaust, move to fallback state
- [ ] On pass, advance orchestrator state to `pending_pr`

**Exit criteria:** a generated patch is actually applied and tested in an isolated Cloud Build job against a real public repo, pass/fail correctly recorded, retry loop demonstrably triggers an alternate fix on failure.

---

## Phase 8 — PR Writer Subagent + Fallback Path
- [ ] Implement PR Writer subagent: takes verified fix + root-cause summary, returns PR title + description text via `LLMClient`
- [ ] GitHub REST API integration: create branch, commit patch, open PR with generated title/description, link back to run
- [ ] Fallback path: if all attempts exhausted without a pass, post a diagnosis-only comment on the original commit/PR (root cause + attempted fixes summary, no code pushed)
- [ ] Orchestrator finalizes run status (`pr_opened` / `fallback_commented` / `error`) and writes final summary to `runs` table
- [ ] End-to-end smoke test: full pipeline webhook → PR (or fallback comment) on a real test repo with a real failing workflow

**Exit criteria:** a real failing CI run on a connected test repo results in either an actual opened PR with working fix, or a clear diagnosis comment — full pipeline works end-to-end at least once, unassisted.

---

## Phase 9 — Observability: Full Trace & Failure Classification
- [ ] Structured trace query/endpoint: given a run id, return full step-by-step timeline (orchestrator decision → each subagent call → tokens/latency/cost per step) in chronological order
- [ ] Failure classification logic: tag each failed/fallback run with a reason category (wrong diagnosis / wrong fix / tests still failing / sandbox error) — either LLM-assisted classification or rule-based on where in the pipeline it stopped
- [ ] Aggregate endpoints: run list with filters (status, repo, date range), per-repo stats (success rate, avg attempts, avg cost)
- [ ] Ensure every LLM call and Cloud Build call already logged (from earlier phases) is queryable in this unified trace view — backfill any gaps found

**Exit criteria:** given any run id, a single API call returns the complete step-by-step trace with costs and a failure classification if applicable; list/filter endpoints return correct aggregated stats.

---

## Phase 10 — Eval Harness
- [ ] Curate 15–20 golden test cases: real CI failures (import errors, type errors, failed assertions, dependency issues) with known-correct fixes, stored as fixtures (repo ref + commit + expected fix characteristics)
- [ ] Eval runner script/endpoint: runs each golden case through the full pipeline (or targeted subagent), records pass/fail against expected outcome
- [ ] Per-subagent eval: does Context Gatherer's summary match the actual root cause (LLM-graded or manual-labeled comparison)? Does Fix Generator's confidence score correlate with actual sandbox pass rate (compute correlation across attempts)?
- [ ] Store eval run results in `eval_results` table: overall accuracy %, per-subagent scores, timestamp, linked to the model/provider config used
- [ ] Regression comparison: given two eval runs (before/after a prompt or strategy change), diff the scores and flag regressions
- [ ] Expose eval summary via API endpoint (feeds dashboard eval display later)

**Exit criteria:** running the eval harness against the golden set produces a stored accuracy score, per-subagent breakdown, and confidence-vs-outcome correlation — re-running after a prompt tweak shows a comparable before/after diff.

---

## Phase 11 — Dashboard Frontend: Core Views
- [ ] Auth pages (login/session via Better Auth) — dashboard fully gated
- [ ] Connected repos list page: add/remove repo, view config
- [ ] Runs feed/table across all repos: status, confidence, attempt count, link to resulting PR or fallback comment, filters (repo, status, date)
- [ ] Per-run detail page: expandable trace view (timeline of orchestrator + subagent steps) with token/cost breakdown per step, rendered from Phase 9's trace endpoint

**Exit criteria:** can log in, see connected repos, browse a real run history, and drill into one run's full trace visually in the browser.

---

## Phase 12 — Dashboard Frontend: Eval, Config & Charts
- [ ] Eval score display: overall accuracy %, per-subagent breakdown, pulled from Phase 10's endpoint
- [ ] Confidence-vs-actual-outcome chart: does high Fix Generator confidence actually predict sandbox pass — scatter/bar visualization
- [ ] Model/provider switcher UI: select provider + model (per-repo or global), writes to Phase 4's config endpoint, applied live
- [ ] Polish pass: empty states, loading states, error states across all dashboard views built in Phase 11 + this phase

**Exit criteria:** dashboard fully reflects live backend state — eval scores, confidence correlation chart, and a working model switcher that changes live LLM behavior without redeploy — v1 feature-complete.