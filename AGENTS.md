# AGENTS.md — Haunter

## Project Description
Haunter is an autonomous CI failure diagnosis and fix agent. On `workflow_run` failure (GitHub Actions), a FastAPI orchestrator on Cloud Run wakes via webhook, delegates to subagents (Context Gatherer → Fix Generator → Sandbox Verifier via Cloud Build → PR Writer), verifies fixes in an isolated sandbox, opens a PR with fix+explanation or posts a diagnosis-only comment on exhaust. Every step (tokens, latency, cost, confidence, attempts) is logged to Neon Postgres and surfaced on a Cloudflare dashboard with run history, per-run trace, eval harness (15-20 golden cases), and live model/provider switcher (OpenCode Zen, default `nemotron-3.5-lightning-free` at `https://opencode.ai/zen/v1`). Multi-repo, Better Auth gated, auditable. See `HAUNTER.md` and `WORK.md`.

## Instructions for AI Agents Working on This Repo

This is a **production-grade, user-facing system** — not a side project, demo, or prototype. Real repos, real CI, real PRs. Code quality must be shippable. Treat every change as if it will run in production tomorrow.

### 1. Quality Bar — Non-Negotiable
- Write production-level code: typed, async-correct (SQLAlchemy 2.0 async, FastAPI), tested, and documented where it matters. No placeholders, no `TODO` left behind, no stub that pretends to work.
- Follow existing patterns and stack exactly: FastAPI, SQLAlchemy async + `NullPool` for Neon pooled URL, Alembic with direct URL for migrations, `asyncpg`, `pydantic-settings`. Do not introduce new deps without justification.
- Keep changes minimal and focused to the assigned phase/task. One phase at a time as defined in `WORK.md`. Do not scope-creep or refactor unrelated files.
- No mess: no duplicate files, no dead code, no commented-out blocks, no secrets in code, no hardcoded model names/URLs (all DB/env-driven). Leave the repo cleaner than you found it.
- Verify before you claim done: run migrations, run the scratch script from exit criteria, hit the endpoint with curl, check logs. If you didn't run it, you didn't do it.

### 2. Absolute Honesty — Zero Tolerance for Lying
- **Do not lie. Do not fake work. Do not hallucinate results.** This is a strict prohibition. Agents that fake passing tests or invent outputs will cause real user-facing failures.
- Be 100% direct and honest, 0% sugarcoating. If you made a mistake, say so plainly. If you broke something, report it. If you are unsure, say "I am unsure because X" — do not guess.
- Report exactly what you did, what passed, what failed, with evidence (command output, logs, file paths with line numbers like `backend/app/db.py:42`). No evidence = not done.
- Never claim a task is complete when exit criteria in `WORK.md` are not met. Incomplete is acceptable; dishonest completion is not.

### 3. When Stuck or Blocked
- It is OK to get stuck, hit a rate limit, or fail to complete. It is NOT OK to silently pick a cheap, messy workaround, mock data to look like success, or skip a requirement.
- Stop, state the blocker clearly (error message, what you tried, what you considered), and ask for direction. Wait for explicit approval before choosing an alternative approach.
- Do not ask to "make it simpler" by cutting quality. Ask: "Here are 2 options with tradeoffs — which should I take?"

### 4. Focus and Discipline
- Work with full focus. One task, fully done, fully verified — then next. No partial work across 5 files that nothing ties together.
- Read files before editing. Use `read` on directories, respect `workdir`, and keep edits precise.
- This project will be used by users and evaluated in interviews. Every line is a reflection of engineering judgment. Make it count.

Violation of honesty or quality rules is a failure, even if tests appear green.
