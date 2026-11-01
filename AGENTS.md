# AGENTS.md — Haunter(Personal Project of saif)

## Project Description
Haunter is an autonomous GitHub PR review and issue-fixing agent/bot. It is triggered only by GitHub tags or webhooks (e.g. `pull_request`, `issue_comment`, and tag events). For now it operates as a PR agent: it reviews pull requests and, when an issue is mentioned in a PR (or it is explicitly told to), fixes that issue in the PR. An AWS Lambda FastAPI orchestrator (via Mangum) coordinates subagents (Reviewer, Context Gatherer, Fix Generator, Sandbox Verifier via GitHub Actions mirror repo, PR Writer). Evaluates security, regressions, architecture, and performance. Fixes are verified in an isolated Actions runner; PRs are opened under a strict human-merge gate. State and traces logged to Neon Postgres, surfaced on a Cloudflare Workers dashboard. See `HAUNTER.md`.

## Dev & Test Commands
- **Frontend**: `cd frontend && npm run dev` (Next.js, port 3011).
- **Backend**: `cd backend && python -m uvicorn main:app --reload --port 7555` (or `.\.venv\Scripts\python.exe -m uvicorn main:app --reload --port 7555`).
- **Tests**: `pytest` (fast hermetic unit), `pytest tests/test_auth.py`, `pytest --all` (DB/E2E with `TEST_DATABASE_URL`), or by marker: `-m fast`, `-m db`, `-m eval`, `-m slow`.

## Repo Rules & Quality Standards
- **Stack & Architecture**: FastAPI, SQLAlchemy 2.0 async + `NullPool` (Neon pooled URL), Alembic (direct URL), `asyncpg`, `pydantic-settings`. No unjustified dependencies or hardcoded models/URLs.
- **Scope & Verification**: Keep scope strictly to assigned task in `WORK.md`. Verify execution (migrations, endpoints, exit scripts) before claiming done.
- **Deployments**: Follow `infra/redeploy.md` and its pre-deploy checklist for Lambda or Cloudflare Workers.
- **Commit Rule (Personal Project)**: Commit frequently and atomically in-process (especially when fixing bugs or resolving issues). Never wait until the end; keep state trackable and rollbacks safe.
- **Honesty**: Zero tolerance for faking work or hallucinating test results. State blockers/errors plainly with evidence (`file:line`, logs).
- **GitHub Issues**: Title `[BUG]` or `[FEAT]` + description (never commit syntax). Body: repro steps, expected vs actual. Apply valid type & `priority/*` labels.
- **Pull Requests**: Title: conventional commit (`fix(scope):`, `feat(scope):`). Body: changes, rationale, linked issue, test evidence. One task per PR.
- **Mandatory Testing (Strict)**: Writing tests is ordered and mandatory. Any new or modified logic, endpoint, or service requires automated tests (unit tests at minimum) covering happy paths, edge cases, and failure modes. Incomplete tests = rejected task.

Violation of honesty, testing, or quality rules is a failure, even if tests appear green.
