# FIN — v1 Build Work Plan

Excludes deployment (separate later phase). Project init assumed done. Each phase sized for one focused coding session with a 1M-context agent.

---

## Security Invariants

- **Least privilege everywhere**: login OAuth App gets `read:user` only; repo/webhook scopes live exclusively in the separate GitHub App (Phase 3+); DB roles and service accounts get only the permissions their phase needs.
- **No secrets in code, logs, or error responses**: all secrets (`GITHUB_CLIENT_ID/SECRET`, `SESSION_SECRET_KEY`, `DATABASE_URL`, `DATABASE_URL_UNPOOLED`, webhook secret) live in `.env` locally / GCP Secret Manager in deploy, never committed, never interpolated into log lines or exception messages returned to clients.
- **Validate all external input with Pydantic**: every request body, query param, webhook payload, and LLM-produced structured output that crosses a trust boundary is parsed through a Pydantic schema before use — reject on mismatch rather than coerce.
- **Parameterized queries only**: all DB access goes through SQLAlchemy 2.0 async ORM/Core constructs with bound parameters; no raw string-interpolated SQL anywhere, including in scratch scripts.
- **Multi-tenant isolation**: every query and mutation on `repos`, `runs`, `attempts`, `run_steps`, `eval_results`, `model_configs` is scoped to the authenticated user's ownership — no endpoint trusts a client-supplied `repo_id`/`run_id` without an ownership check against `get_current_user`.
- **Defense in depth on trust boundaries**: webhook signatures are timing-safe compared, OAuth `state` is single-use and signed, session cookies are signed + short-lived, sandbox execution is isolated per-run with no access to other tenants' secrets or filesystems.
- **Fail closed, log safely**: auth, signature, and authorization failures return generic errors to the client (no internal detail, no stack trace, no secret fragments) while structured (secret-redacted) detail goes to server-side logs only.
- **Assume the LLM output is untrusted input**: patches, PR text, and structured JSON from any subagent are treated as attacker-influenceable and validated/sandboxed before being applied, executed, or displayed — never `eval`'d, never given implicit filesystem/network privileges beyond the sandboxed Cloud Build job.

---

## Phase 1 — Database Schema & Models
- [x] Design Postgres schema in SQLAlchemy (async, SQLAlchemy 2.0 style): `users` (FastAPI-owned, Alembic-managed — populated by GitHub OAuth callback), `repos`, `runs`, `run_steps` (subagent trace log), `attempts` (fix attempts within a run), `eval_results`, `model_configs` (provider/model switcher)
- [x] Define relationships: repo → runs → attempts/steps, run → eval_result
- [x] Set up Alembic for migrations
- [x] Configure two DB connections: pooled (app runtime, `NullPool` on SQLAlchemy side since Neon PgBouncer pools already) and direct/unpooled (migrations only)
- [x] Write initial migration and verify it applies cleanly against a fresh Neon DB
- [x] Add simple DB session dependency (`get_db`) for FastAPI routes
- [x] **[SECURE]** `users.access_token` column typed to hold ciphertext (`TEXT`, not `VARCHAR(255)`) — already satisfied in `backend/app/models.py:31`, encryption of value itself is Phase 2
- [x] **[SECURE]** Alembic direct/unpooled connection string is only ever read from `DATABASE_URL_UNPOOLED` at migration-run time (CI/local), never exposed to the running FastAPI app process — already satisfied in `backend/app/config.py:38`
- [x] **[SECURE]** No column stores raw secrets (webhook secrets, API keys) in plaintext without a documented encryption plan; DB user used by the app has only `SELECT/INSERT/UPDATE/DELETE` on app tables, no `DROP`/`ALTER`/superuser rights — documented in Security Invariants, enforced via Neon role config

> **[SECURE] Note:** Full `users.id` ownership FK chain (`repos.user_id UUID FK users.id`) is Phase 2's first migration (`backend/app/models.py:42` currently no `user_id`) — tracked in Phase 2, not blocking Phase 1 DONE.

**Exit criteria:** tables exist in Neon, migrations run cleanly, can insert/query a dummy row via a scratch script. **[SECURE]** `users.access_token` holds `TEXT`, `DATABASE_URL_UNPOOLED` never touches runtime, invariants documented — Phase 1 DONE (9/9).

---

## Phase 2 — GitHub OAuth (FastAPI + authlib) & Repo Management API
- [x] Auth endpoints (FastAPI-native):
  - `GET /auth/login` → 302 to GitHub OAuth authorize (OAuth App, `read:user` scope only)
  - `GET /auth/callback?code=` → exchange via authlib, fetch `https://api.github.com/user`, upsert `users` table, set `httpOnly Secure SameSite=Lax` signed cookie (itsdangerous, `user_id` only, 14d), 302 to `FRONTEND_URL`
  - `POST /auth/logout` → clear cookie
  - `GET /auth/me` → 200 `{id, github_username, avatar_url}` or 401
- [x] `users` Alembic migration applied (table: `id UUID PK`, `github_id BIGINT UNIQUE`, `github_username`, `avatar_url`, `access_token`, `created_at`, `updated_at`)
- [x] `get_current_user` FastAPI dependency: reads signed cookie, verifies with itsdangerous, loads user from DB — protects all repo/run routes
- [x] **GitHub App decision**: use a SEPARATE GitHub OAuth App for login (read:user scope) vs a separate GitHub App for repo installation and webhooks (Phase 3+, needs repo-admin scope). Reason: least privilege — login must never request destructive scopes.
- [x] CORS configured: `allow_origins=[FRONTEND_URL]`, `allow_credentials=True`
- [x] CRUD endpoints: add repo (owner/name, GitHub install info), list connected repos, remove repo
- [x] Store per-repo config: default branch, language hint (optional), active model/provider override
- [x] Basic request/response Pydantic schemas for repo + run objects (used by both this phase and later ones)
- [x] **[SECURE]** `repos.user_id UUID FK users.id NOT NULL` migration (Phase 1 → Phase 2 ownership fix) — add indexed FK + backfill/cascade, unique constraint becomes `(user_id, owner, name)` so two users can track same public repo independently
- [x] **[SECURE]** `GET /auth/login` generates a cryptographically random `state` value, signs it with `itsdangerous.TimestampSigner` (keyed off `SESSION_SECRET_KEY`), and stores it in a short-lived (5–10 min) `httpOnly Secure SameSite=Lax` cookie (or server-side cache keyed by a nonce) — never in localStorage or a query param round-tripped by the client
- [x] **[SECURE]** `GET /auth/callback` validates the returned `state` against the stored signed value (constant-time compare via the signer's own verification, single-use — invalidate/delete immediately after check) before exchanging `code`; reject with a generic 400 on mismatch, missing, or expired `state` (CSRF protection on the OAuth flow)
- [x] **[SECURE]** `redirect_uri` used in the authorize request and validated on callback is a hardcoded, exact-match value from server config (never derived from request headers, `Host`, or any client-supplied parameter) — prevents open-redirect/OAuth token theft via manipulated `redirect_uri`
- [x] **[SECURE]** Evaluate PKCE (`code_verifier`/`code_challenge`, S256) for the GitHub OAuth App authorize/token exchange; GitHub's OAuth Apps do not mandate PKCE, so document the decision explicitly (adopt if authlib supports it cleanly for GitHub, otherwise note as accepted risk with `state`-CSRF protection as the compensating control)
- [x] **[SECURE]** Session cookie (`haunter_session` or equivalent) is set with `HttpOnly; Secure; SameSite=Lax; Path=/`, and uses the `__Host-` prefix when the deploy domain setup allows it (same-origin API+frontend on apex/subdomain without a separate cookie domain); document why `__Host-` is or isn't used for this deploy topology
- [x] **[SECURE]** `itsdangerous.TimestampSigner` (or `URLSafeTimedSerializer`) signs the cookie payload with `max_age` enforced server-side at 14 days on every read in `get_current_user`; `SESSION_SECRET_KEY` is versioned/rotatable (e.g. key id embedded in payload or a `SESSION_SECRET_KEY_PREVIOUS` fallback checked on verify) so rotating the key doesn't force-logout every user without a migration path
- [x] **[SECURE]** `users.access_token` is encrypted at rest before insert/update (Fernet symmetric encryption keyed from `SESSION_SECRET_KEY`-derived key or a dedicated `TOKEN_ENCRYPTION_KEY`, ideally sourced from GCP KMS/Secret Manager) and decrypted only at the point of use for GitHub API calls; if shipped as plaintext for velocity, add an explicit `# TODO(security): encrypt access_token before prod` marker and track it as a pre-prod blocker
- [x] **[SECURE]** Rate-limit `GET /auth/login`, `GET /auth/callback`, and `POST /auth/logout` (e.g. `slowapi`, per-IP and per-session) to blunt credential-stuffing/OAuth-flow abuse and callback replay attempts
- [x] **[SECURE]** All auth error paths (bad `state`, failed code exchange, GitHub API failure, malformed cookie signature) return a generic, non-descriptive error to the client and log full detail (with the `code`, `access_token`, and `SESSION_SECRET_KEY` values redacted/never logged) server-side only
- [x] **[SECURE]** CORS `allow_origins` is the exact `FRONTEND_URL` string (no wildcard `*`, no regex, no `allow_origins=["*"]` combined with `allow_credentials=True` which browsers reject anyway but must never be attempted) — confirm no additional origins are added ad hoc later without review
- [x] **[SECURE]** `POST /auth/logout` clears the cookie using the identical attribute set it was set with (`HttpOnly; Secure; SameSite=Lax; Path=/`, same name/domain/`__Host-` prefix if used) so browsers actually remove it rather than leaving a stale cookie under different attributes
- [x] **[SECURE]** Repo CRUD endpoints (`add`, `list`, `remove`) all resolve the target repo through `get_current_user`'s `user.id` — `remove`/mutating endpoints 404 (not 403, to avoid confirming existence to non-owners) on a `repo_id` that exists but isn't owned by the caller
- [x] **[SECURE]** `GET/PUT /config/model` (introduced fully in Phase 4) is stubbed here with the same ownership check pattern so later phases inherit it rather than bolting it on afterward

**Exit criteria:** can register/login, and via API add/list/remove a connected repo, persisted in Neon, auth-gated. **[SECURE]** a forged/tampered `state` or session cookie is rejected with a generic error (verified by manual tamper test), `redirect_uri` cannot be overridden via request manipulation, CORS rejects a request from an arbitrary origin, logout provably clears the cookie (verified in browser devtools), and a second test user cannot list/remove the first user's repos via direct `repo_id` manipulation — Phase 2 DONE (21/21).

---

## Phase 3 — GitHub Webhook Ingestion
- [ ] `POST /webhooks/github` endpoint accepting `workflow_run` events
- [ ] Verify `X-Hub-Signature-256` HMAC against stored webhook secret; reject on mismatch
- [ ] Filter to `action: completed` + `conclusion: failure`; ignore everything else with fast 200
- [ ] Idempotency: dedupe on GitHub delivery id (or workflow run id) before creating a new `run` row — store seen delivery ids or check existing run status
- [ ] On valid new failure: create `run` row (status=`pending`), return 2xx immediately, kick off pipeline via FastAPI `BackgroundTasks`
- [ ] Stub pipeline entrypoint function (logs "pipeline started for run X") — actual orchestrator logic comes in later phases
- [ ] GitHub REST API client wrapper: fetch workflow run logs, diff, commit metadata (needed by next phase, build the client now)
- [ ] **[SECURE]** HMAC signature comparison uses a constant-time function (`hmac.compare_digest`, not `==`) to prevent timing side-channel attacks on the webhook secret
- [ ] **[SECURE]** Webhook secret is stored only in Secret Manager/`.env` (never in the `repos` table or logs) and is unique per GitHub App installation if the App supports per-installation secrets; raw request body (not re-serialized JSON) is what's HMAC'd, since re-serialization can alter bytes and break/weaken verification
- [ ] **[SECURE]** Delivery-id dedupe store (`X-GitHub-Delivery` header) has a uniqueness constraint at the DB level (not just an application-level check-then-insert), closing the race window between a duplicate delivery arriving concurrently and the check completing
- [ ] **[SECURE]** Payload size limit enforced on `POST /webhooks/github` (reject oversized bodies before parsing) to prevent resource-exhaustion via an oversized/malformed payload
- [ ] **[SECURE]** Webhook payload fields consumed (repo full name, commit SHA, workflow run id, etc.) are validated against a Pydantic schema and cross-checked that the referenced repo exists in `repos` and is owned/installed by a known tenant before a `run` row is created — an unrecognized repo is logged and dropped, not silently processed
- [ ] **[SECURE]** GitHub REST API client wrapper never logs the App's installation access token; token is fetched per-use (or cached with short TTL matching GitHub's expiry) and never persisted to the `run`/`run_steps` rows

**Exit criteria:** pushing a failing workflow_run payload (simulated via curl/Postman with valid signature) creates a `run` row, responds fast, dedupes on retry, logs pipeline stub trigger. **[SECURE]** an invalid/missing signature is rejected (constant-time), a replayed identical delivery id does not create a second `run` row even under concurrent delivery, and a payload referencing an unregistered repo is safely dropped without error leakage.

---

## Phase 4 — LLM Provider Abstraction
- [ ] Build `LLMClient` interface: single `.complete(messages, tools=None, ...)` method, provider-agnostic
- [ ] Implement OpenCode Zen adapter (OpenAI-compatible, base URL `https://opencode.ai/zen/v1`, default model `nemotron-3.5-lightning-free`)
- [ ] Read active provider/model from `model_configs` table (DB-driven, not hardcoded), fall back to env var if DB empty
- [ ] Support tool-calling passthrough (needed for subagents that call tools, e.g. sandbox trigger)
- [ ] Add simple retry/error handling wrapper (timeouts, rate limit backoff)
- [ ] Endpoint: `GET/PUT /config/model` to read/update active model+provider (per-repo or global) — backs the future dashboard switcher
- [ ] Token usage + latency captured on every `.complete()` call, returned alongside response (used for observability logging starting Phase 5)
- [ ] **[SECURE]** OpenCode Zen API key is read from Secret Manager/`.env` only, injected into the adapter at construction time, never included in `run_steps` token/latency logs or error messages surfaced to the client
- [ ] **[SECURE]** `PUT /config/model` is owner-only-write: gated by `get_current_user`, and if scoped per-repo, verifies the caller owns that `repo_id`; if scoped globally, restrict to a single designated admin user id (documented) rather than any authenticated user, since a global model switch affects all tenants
- [ ] **[SECURE]** `GET/PUT /config/model` request/response bodies validated via Pydantic with an allowlist of accepted provider/model string values (not free-text) to prevent injection of an unexpected base URL or model identifier that could redirect traffic to an attacker-controlled endpoint
- [ ] **[SECURE]** Retry/backoff wrapper caps total retry time and attempt count (no unbounded retry loop) to prevent a slow/hostile upstream from exhausting `BackgroundTasks` worker capacity (a lightweight denial-of-service vector)
- [ ] **[SECURE]** LLM responses are never `eval`'d or executed directly — all structured output (patches, JSON) is treated as untrusted text and parsed via explicit schema validation (ties into Phase 6's strict output schema)

**Exit criteria:** a scratch script can call `LLMClient.complete()`, get a real response from Nemotron via OpenCode Zen, and switching the DB config to a different model string changes which model answers — no code redeploy needed. **[SECURE]** a non-owner cannot change another tenant's model config via `PUT /config/model`, an invalid/unlisted model string is rejected by Pydantic validation, and the OpenCode Zen API key never appears in any log line or client-facing error.

---

## Phase 5 — Orchestrator Skeleton + Context Gatherer Subagent
- [ ] Orchestrator class/function: holds only compact run state (run id, repo id, step, decisions, confidence) — never raw logs
- [ ] Orchestrator drives a simple state machine: `context_gathering → fix_generation → verification → pr_or_fallback`
- [ ] Implement Context Gatherer subagent: takes raw logs + diff + commit history (from Phase 3's GitHub client) as narrow input, returns distilled root-cause summary (few hundred tokens) via `LLMClient`
- [ ] Concurrency: if dispatching log analysis + diff analysis + commit history as separate sub-calls, run them concurrently (asyncio.gather, capped ~3), merge into one summary
- [ ] Persist each subagent call as a `run_steps` row: input tokens, output tokens, latency, cost estimate (from Phase 4 usage data)
- [ ] Wire into Phase 3's pipeline stub: webhook → orchestrator → context gatherer → summary logged to DB, run status updated
- [ ] **[SECURE]** Raw CI logs/diffs passed into the Context Gatherer are scanned/truncated for obvious secret patterns (tokens, private keys, connection strings accidentally printed in CI output) before being sent to the third-party LLM provider, and never persisted verbatim in `run_steps` (only the distilled summary + token counts are stored)
- [ ] **[SECURE]** Orchestrator state transitions are validated (no skipping from `context_gathering` directly to `pr_or_fallback`) so a malformed or replayed background task can't push a run into an inconsistent/privileged state
- [ ] **[SECURE]** `run_steps` writes are scoped to the `run_id`'s owning repo/user at the DB layer (FK constraint + ownership check on any read endpoint), preventing cross-tenant trace data leakage
- [ ] **[SECURE]** Concurrent sub-calls (asyncio.gather, capped ~3) have a per-call timeout so one hung upstream request can't stall the whole orchestrator step indefinitely

**Exit criteria:** a real (or simulated) CI failure payload flows through webhook → orchestrator → Context Gatherer → a root-cause summary is persisted and visible via a DB query, with full token/latency trace in `run_steps`. **[SECURE]** a deliberately secret-laden fake log line is confirmed redacted/truncated before leaving the service boundary, and querying `run_steps` for a run you don't own (via a second test user) returns nothing.

---

## Phase 6 — Fix Generator Subagent + Confidence Scoring
- [ ] Implement Fix Generator subagent: takes root-cause summary (+ prior failed attempt on retry) as input, returns unified diff/patch + confidence score (0–100) via `LLMClient`
- [ ] Define strict output schema (JSON mode or structured prompt) so patch + confidence are reliably parseable
- [ ] Persist each generation as an `attempts` row: attempt number, strategy notes, patch text, confidence score, timestamp
- [ ] Orchestrator step: after Context Gatherer, call Fix Generator, log attempt, advance state to `pending_verification`
- [ ] Basic patch sanity validation (valid diff format, non-empty) before handing to sandbox step
- [ ] **[SECURE]** Strict output schema is enforced via Pydantic parsing of the LLM's JSON response (reject and retry-with-error-context, don't best-effort-regex-extract) — confidence score is bounds-checked to the documented 0–100 range before storage
- [ ] **[SECURE]** Patch sanity validation includes a path-traversal/scope check: reject any diff hunk touching paths outside the target repo's checkout (e.g. `../`, absolute paths, `.git/` internals, CI config files like `.github/workflows/*` unless explicitly allowed) before it ever reaches the sandbox in Phase 7
- [ ] **[SECURE]** Patch text stored in `attempts.patch_text` is treated as untrusted content when later rendered anywhere (dashboard, PR body) — no raw HTML/markdown injection risk carried forward (escaping handled at render time in Phase 11/8)
- [ ] **[SECURE]** Attempt cap (2–3, enforced in Phase 7's retry logic) is also checked here defensively — Fix Generator refuses to run for a `run_id` that has already exhausted its attempt budget, preventing a race/replay from generating unbounded attempts

**Exit criteria:** given a logged root-cause summary, Fix Generator produces a parseable patch + confidence score, stored as an `attempts` row, orchestrator state advances correctly. **[SECURE]** a crafted patch attempting to modify a path outside the repo checkout or a CI workflow file is rejected before sandbox handoff, and a malformed/out-of-range LLM JSON response is rejected rather than silently coerced.

---

## Phase 7 — Sandbox Verifier (Cloud Build Integration)
- [ ] Cloud Build job definition (build config): clone repo fresh, detect language/runtime (or use repo's own Dockerfile if present), install deps, apply patch, run test suite
- [ ] Python `google-cloud-build` client wrapper in orchestrator: trigger build via `cloudbuild_v1.CloudBuildClient` (`create_build` or `run_build_trigger`), pass repo ref + patch as substitution/input
- [ ] Poll build status (or use build completion callback) until pass/fail; extract short failure reason (not full raw output) on failure
- [ ] Persist verification result on the `attempts` row: pass/fail, failure reason, build duration
- [ ] Orchestrator retry logic: on fail, if attempts < cap (2–3), loop back to Fix Generator with failure reason injected into context (alternate strategy); on exhaust, move to fallback state
- [ ] On pass, advance orchestrator state to `pending_pr`
- [ ] **[SECURE]** Cloud Build job runs with a dedicated, minimally-privileged service account (no access to the app's Secret Manager secrets, no access to other tenants' repo credentials, no egress beyond what's needed to clone the target repo and fetch deps) — no Docker-in-Docker, consistent with locked stack, and no shared persistent volume between concurrent builds for different tenants
- [ ] **[SECURE]** Per-build timeout and resource limits (CPU/memory/build minutes) enforced at the Cloud Build config level to prevent a malicious or runaway patch (e.g. fork bomb, infinite loop in a test) from causing resource exhaustion or cost abuse
- [ ] **[SECURE]** Build substitutions (repo ref, patch content) passed to `create_build`/`run_build_trigger` are validated/escaped for Cloud Build substitution syntax before submission, preventing substitution-injection into the build config
- [ ] **[SECURE]** Extracted failure reason stored on `attempts` is truncated/sanitized (strip potential secrets accidentally echoed by a test failure, cap length) before persistence and before being fed back into the next Fix Generator call
- [ ] **[SECURE]** Retry loop's attempt cap is enforced atomically against the DB (e.g. a DB-level check-and-increment or row lock) so concurrent webhook redeliveries for the same run can't bypass the 2–3 attempt ceiling
- [ ] **[SECURE]** Build status polling authenticates only via the orchestrator's own service account credentials (never accepts an unauthenticated public callback URL for build completion) to prevent spoofed build-result injection

**Exit criteria:** a generated patch is actually applied and tested in an isolated Cloud Build job against a real public repo, pass/fail correctly recorded, retry loop demonstrably triggers an alternate fix on failure. **[SECURE]** the Cloud Build service account is confirmed to have no access to app secrets or other tenants' data (verified via IAM policy review), a build exceeding its resource/time limit is terminated rather than left running, and concurrent duplicate triggers for the same run are confirmed not to exceed the attempt cap.

---

## Phase 8 — PR Writer Subagent + Fallback Path
- [ ] Implement PR Writer subagent: takes verified fix + root-cause summary, returns PR title + description text via `LLMClient`
- [ ] GitHub REST API integration: create branch, commit patch, open PR with generated title/description, link back to run
- [ ] Fallback path: if all attempts exhausted without a pass, post a diagnosis-only comment on the original commit/PR (root cause + attempted fixes summary, no code pushed)
- [ ] Orchestrator finalizes run status (`pr_opened` / `fallback_commented` / `error`) and writes final summary to `runs` table
- [ ] End-to-end smoke test: full pipeline webhook → PR (or fallback comment) on a real test repo with a real failing workflow
- [ ] **[SECURE]** PR branch creation uses the GitHub App's installation token scoped to only the target repo (never a broad personal access token), and the branch name is generated server-side (not derived from unsanitized LLM output) to prevent branch-name injection or collision with protected branches
- [ ] **[SECURE]** GitHub App permissions for repo-write are the minimum needed to create branches/commits/PRs and post comments (`contents: write`, `pull_requests: write`) — explicitly no `administration`, no force-push/branch-protection-bypass rights, and the App cannot push directly to the default/protected branch, only open a PR against it
- [ ] **[SECURE]** LLM-generated PR title/description is treated as untrusted text: length-capped and rendered as plain text/escaped markdown when displayed in the dashboard (Phase 11) to prevent stored injection via a maliciously-crafted commit message or log content that made it into the summary chain
- [ ] **[SECURE]** Fallback diagnosis comment is similarly sanitized/length-capped before posting, and never includes raw secrets or full raw CI logs (only the distilled, already-redacted summary from Phase 5)
- [ ] **[SECURE]** Orchestrator verifies the target repo/PR belongs to the run's associated tenant before making any GitHub write call, preventing a manipulated `run_id`/background task from writing to the wrong repository

**Exit criteria:** a real failing CI run on a connected test repo results in either an actual opened PR with working fix, or a clear diagnosis comment — full pipeline works end-to-end at least once, unassisted. **[SECURE]** the GitHub App installation token used is confirmed scoped to only the intended repo with no direct-push-to-protected-branch capability, and a test with a deliberately adversarial PR-Writer prompt injection attempt (e.g. instructions embedded in log content) is confirmed not to alter orchestrator behavior or produce unescaped output in the PR/comment.

---

## Phase 9 — Observability: Full Trace & Failure Classification
- [ ] Structured trace query/endpoint: given a run id, return full step-by-step timeline (orchestrator decision → each subagent call → tokens/latency/cost per step) in chronological order
- [ ] Failure classification logic: tag each failed/fallback run with a reason category (wrong diagnosis / wrong fix / tests still failing / sandbox error) — either LLM-assisted classification or rule-based on where in the pipeline it stopped
- [ ] Aggregate endpoints: run list with filters (status, repo, date range), per-repo stats (success rate, avg attempts, avg cost)
- [ ] Ensure every LLM call and Cloud Build call already logged (from earlier phases) is queryable in this unified trace view — backfill any gaps found
- [ ] **[SECURE]** Trace endpoint (`GET /runs/{run_id}/trace`) and aggregate endpoints all enforce ownership via `get_current_user` — a `run_id`/`repo_id` filter belonging to another tenant returns 404, and list/aggregate queries are implicitly scoped to `WHERE repo.user_id = current_user.id` at the query level, never filtered client-side after an unscoped fetch
- [ ] **[SECURE]** Date-range and filter query params are validated via Pydantic (bounded ranges, allowlisted status enum values) to prevent malformed input from causing expensive unbounded queries
- [ ] **[SECURE]** Trace view confirms no raw secrets or full raw logs leaked into `run_steps`/`attempts` content during backfill (spot-check the redaction from Phase 5 held up across all historical rows before exposing this view)

**Exit criteria:** given any run id, a single API call returns the complete step-by-step trace with costs and a failure classification if applicable; list/filter endpoints return correct aggregated stats. **[SECURE]** a second test user's authenticated request for the first user's `run_id` trace returns 404, and per-repo aggregate stats never include another tenant's runs.

---

## Phase 10 — Eval Harness
- [ ] Curate 15–20 golden test cases: real CI failures (import errors, type errors, failed assertions, dependency issues) with known-correct fixes, stored as fixtures (repo ref + commit + expected fix characteristics)
- [ ] Eval runner script/endpoint: runs each golden case through the full pipeline (or targeted subagent), records pass/fail against expected outcome
- [ ] Per-subagent eval: does Context Gatherer's summary match the actual root cause (LLM-graded or manual-labeled comparison)? Does Fix Generator's confidence score correlate with actual sandbox pass rate (compute correlation across attempts)?
- [ ] Store eval run results in `eval_results` table: overall accuracy %, per-subagent scores, timestamp, linked to the model/provider config used
- [ ] Regression comparison: given two eval runs (before/after a prompt or strategy change), diff the scores and flag regressions
- [ ] Expose eval summary via API endpoint (feeds dashboard eval display later)
- [ ] **[SECURE]** Eval runner endpoint (if exposed over HTTP rather than run only as an offline script) is restricted to the designated admin user (same pattern as the global model-config write in Phase 4), since it triggers real pipeline runs and Cloud Build jobs at will
- [ ] **[SECURE]** Golden test fixtures use dedicated, non-production, non-sensitive public test repos (no fixtures pointing at a tenant's real connected repo) to avoid the eval harness ever writing a PR/comment against a real user's project
- [ ] **[SECURE]** `GET /eval-results` endpoint scopes access consistently with the admin-only write above; eval data (which can reveal prompt/strategy details) is not exposed to arbitrary authenticated tenants by default

**Exit criteria:** running the eval harness against the golden set produces a stored accuracy score, per-subagent breakdown, and confidence-vs-outcome correlation — re-running after a prompt tweak shows a comparable before/after diff. **[SECURE]** confirm the eval harness never targets a real tenant repo (fixtures audited), and non-admin authenticated requests to trigger/read eval runs are rejected.

---

## Phase 11 — Dashboard Frontend: Core Views
- [ ] Auth pages (login via `GET /auth/login` → GitHub OAuth, session via `haunter_session` cookie + `GET /auth/me`, logout via `POST /auth/logout`) — dashboard fully gated, no Better Auth/JWKS/JWT
- [ ] Connected repos list page: add/remove repo, view config
- [ ] Runs feed/table across all repos: status, confidence, attempt count, link to resulting PR or fallback comment, filters (repo, status, date)
- [ ] Per-run detail page: expandable trace view (timeline of orchestrator + subagent steps) with token/cost breakdown per step, rendered from Phase 9's trace endpoint
- [ ] **[SECURE]** Note: auth pages consume the FastAPI-owned session (Phase 2's cookie-based flow), not a separate Better Auth session store — frontend calls `/auth/login`, `/auth/me`, `/auth/logout` and relies on the `httpOnly Secure SameSite=Lax` cookie sent with `credentials: 'include'`; do not introduce a second, parallel auth/session mechanism
- [ ] **[SECURE]** All fetches from the dashboard include `credentials: 'include'` and target only the configured API origin (no hardcoded fallback to a dev URL shipped in a prod build)
- [ ] **[SECURE]** Any user-controlled or LLM-generated text rendered in the UI (PR titles/descriptions, fallback comments, patch text, repo names) is rendered via safe text/markdown rendering with escaping (no `dangerouslySetInnerHTML`/raw HTML injection) to prevent stored XSS from content that originated in a CI log or LLM response
- [ ] **[SECURE]** Client-side route guards redirect unauthenticated users, but the real enforcement remains server-side (`get_current_user` on every API call) — the frontend gate is UX only, never treated as the security boundary

**Exit criteria:** can log in, see connected repos, browse a real run history, and drill into one run's full trace visually in the browser. **[SECURE]** confirm no dashboard view renders unescaped LLM/log-derived content (spot-check with a fixture containing HTML/script-like text in a patch or PR description), and confirm all dashboard API calls fail gracefully (redirect to login) on an expired/invalid session rather than exposing a broken authenticated view.

---

## Phase 12 — Dashboard Frontend: Eval, Config & Charts
- [ ] Eval score display: overall accuracy %, per-subagent breakdown, pulled from Phase 10's endpoint
- [ ] Confidence-vs-actual-outcome chart: does high Fix Generator confidence actually predict sandbox pass — scatter/bar visualization
- [ ] Model/provider switcher UI: select provider + model (per-repo or global), writes to Phase 4's config endpoint, applied live
- [ ] Polish pass: empty states, loading states, error states across all dashboard views built in Phase 11 + this phase
- [ ] **[SECURE]** Eval score display is only rendered/linked for the designated admin user (matching Phase 10's access restriction) — regular tenant users don't see a broken/403'd eval section, it's simply not shown
- [ ] **[SECURE]** Model/provider switcher UI only allows selecting from the same server-side allowlist enforced in Phase 4 (no free-text model/provider entry in the UI that could round-trip an unexpected value to the API)
- [ ] **[SECURE]** Error states across the dashboard display generic, user-safe messages (not raw API error bodies/stack traces that could leak internal detail) — pair with Phase 2/9's generic-error-response invariant

**Exit criteria:** dashboard fully reflects live backend state — eval scores, confidence correlation chart, and a working model switcher that changes live LLM behavior without redeploy — v1 feature-complete. **[SECURE]** confirm a non-admin tenant cannot reach eval data through the UI, the model switcher cannot submit an unlisted provider/model string, and error states never surface raw backend error text to the browser.