# Contributing to Haunter

## Setup

- Backend: `cd backend && python -m uvicorn main:app --reload --port 7555`
- Frontend: `cd frontend && npm run dev`
- Tests: `cd backend && pytest` (fast suite, default)

## Workflow

1. Open or pick an issue first. One focused PR per issue — NEVER flood PRs with unrelated changes.
2. Branch from main: `fix/`, `feat/`, `chore/`, or `refactor/` prefix.
3. Keep diffs minimal. No drive-by refactors, no extra deps.
4. Run the relevant tests before pushing.

## PR requirements

- Links the issue it closes.
- Describes what changed and why, briefly.
- All touched tests pass (`pytest` output pasted or clean CI).

By contributing, you agree your contributions are licensed under Apache-2.0.
