# Contributing to Haunter

Haunter is a production system (real repos, real CI, real PRs). Treat every change as shippable. See [`AGENTS.md`](AGENTS.md) (repo conventions, test commands), [`HAUNTER.md`](HAUNTER.md) (architecture), and [`WORK.md`](WORK.md) (phased plan) before starting.

## Setup

Prerequisites: Python 3.11+, Node 20+, Postgres (only for `--all` / `db` tests).

### Frontend

```sh
cd frontend
npm install
npm run dev      # Next.js on port 3011
```

### Backend

```sh
cd backend
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn main:app --reload --port 7555
# Windows venv without activating: .\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 7555
```

### Backend `.env`

Create `backend/.env` (never commit it). Required keys:

```sh
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/haunter
DATABASE_URL_UNPOOLED=postgresql+asyncpg://user:password@localhost:5432/haunter
TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/haunter_test
GITHUB_CLIENT_ID=<oauth-app-client-id>
GITHUB_CLIENT_SECRET=<oauth-app-client-secret>
CALLBACK_URL=http://localhost:7555/auth/callback
SESSION_SECRET_KEY=<generated-session-secret>
TOKEN_ENCRYPTION_KEY=<generated-fernet-key>
FRONTEND_URL=http://localhost:3011
OPENCODE_ZEN_API_KEY=<opencode-zen-api-key>
OPENCODE_ZEN_BASE_URL=https://opencode.ai/zen/v1
DEFAULT_MODEL=nemotron-3.5-lightning-free
```

`DATABASE_URL` serves the app; `TEST_DATABASE_URL` must point at a local throwaway Postgres (never production or a shared Neon branch — the test harness refuses to run otherwise). OAuth keys gate the multi-repo dashboard; model URL/keys select the provider.

### Trigger a local PR review

Exercise the real review pipeline by POSTing a signed `pull_request` webhook to a running dev server. The script is only a client: it hits the same `POST /webhooks/github` ingress GitHub uses, so no review logic is bypassed.

Add `GITHUB_WEBHOOK_SECRET=devsecret` to `backend/.env`, restart uvicorn, then from `backend/`:

```sh
python scripts/trigger_local_review.py --repo saif-org/haunter-demo --pr 7 --seed-repo
# --seed-repo creates the `saif-dev` user and repo row when they are missing.
# Inspect the payload + curl equivalent without sending:
python scripts/trigger_local_review.py --repo saif-org/haunter-demo --pr 7 --dry-run --secret devsecret
```

The secret must match the server's `GITHUB_WEBHOOK_SECRET`; `--secret` overrides the `.env` value. Each run uses a fresh random head SHA so repeat runs are not rejected as duplicates — pass `--sha <40-hex>` to replay a specific commit.

A `queued` response means the delivery was accepted, not that the review finished: locally the pipeline runs as a FastAPI `BackgroundTask` after the response, so watch the uvicorn log. `--seed-repo` sets no `github_install_id`, so fetching the diff for a fully successful review still needs GitHub App credentials.

## Tests

From `backend/`:

| Command | What it runs |
|---|---|
| `pytest` | Fast hermetic suite (default, zero external I/O) |
| `pytest tests/test_auth.py` | One module; `::test_name` for a single test |
| `pytest -m fast` / `-m db` / `-m eval` / `-m slow` | By marker |
| `TEST_DATABASE_URL=<local-pg> pytest --all` | Full suite incl. DB, E2E, eval harness |

Write deterministic, isolated tests for new logic + edge cases. If you didn't run it, you didn't do it.

## Local PR review trigger

Local dev has no auto-trigger — GitHub never reaches localhost. To run a PR review locally, trigger the real production ingress manually:

```sh
cd backend
python scripts/trigger_local_review.py --repo <owner>/<name> --pr <N> --seed-repo
```

- `GITHUB_WEBHOOK_SECRET` in `backend/.env` must match the value the server uses (any non-empty value works locally).
- Repeat runs need a fresh commit SHA (dedup guard blocks identical `repo+sha+pr`); the script generates one by default, or pass `--sha`.
- `status="queued"` means the review was enqueued — the pipeline runs in `BackgroundTasks` after the HTTP response, so check the review status afterwards.

## Workflow: branches and commits

1. Open or pick an issue first. One focused PR per issue — NEVER flood PRs with unrelated changes.
2. Branch from `main` — never commit to `main`/`master`/`dev`:
  - `fix/<short-name>`, `feat/<short-name>`, `chore/<short-name>`, `refactor/<short-name>`
3. Minimal diffs: touch only what the issue requires. No drive-by refactors, no new deps without justification, no dead code or commented-out blocks.
4. Commit style: one clean commit per task (`git add` only your files; never `git stash` in this shared workspace). Conventional, imperative subject, e.g. `fix(auth): reject expired tokens`.
5. Before pushing: run migrations if touched, re-run the relevant tests, and confirm `git status` shows only your files.

## PR process

- Push to the same branch and open a PR with `gh` (clean title, short body). Link the issue it closes; describe what changed and why.
- PR requirements: linked issue, brief what/why, test evidence (`pytest` output or clean CI), no secrets.
- Do not merge unprompted — wait for review. If told to merge, use merge commits (`gh pr merge --merge`) and delete the branch locally + remote.
- Out-of-scope issues found mid-task: file with `gh issue create`, don't fix inline. Use plain titles (no `fix:`/`feat:` prefixes) and clean Markdown with backticked paths/symbols.

## AI assistance and anti-slop rules

AI help is welcome; AI slop is not. Every AI-assisted PR must meet the same bar as a human one:

- Never flood PRs: one focused PR per issue. No mega-PRs bundling unrelated changes.
- No unverified code: run the relevant tests yourself and paste evidence. Never claim green without running it.
- No invented APIs, libs, or paths: use only APIs you confirmed exist in this repo; cite file paths with line numbers (e.g. `backend/app/db.py:42`).
- Disclose AI assistance in the PR body (what tool wrote what, what you verified and how).
- Concrete anti-slop checks before you open a PR:
  - Read every file you touched end-to-end; delete dead code, commented-out blocks, and duplicate helpers you introduced.
  - Confirm each import, type, and path actually resolves in this repo — no invented APIs, libs, or file paths.
  - Run the touched area's tests yourself and paste the output; `npm run dev` booting is not test evidence.
  - Keep the diff minimal: one issue, one PR. Split anything unrelated into its own branch.
  - Match existing patterns exactly (SQLAlchemy 2.0 async, `pydantic-settings`, async `asyncpg`); a "cleaner" rewrite that diverges from repo convention is still slop.
- Violations (fake passing tests, hallucinated results, unverified mega-diffs) are grounds for close-without-review, even if CI looks green.

By contributing, you agree your contributions are licensed under Apache-2.0.
