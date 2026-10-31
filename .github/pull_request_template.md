## What this PR does

Closes #(issue number). One focused PR per issue.

## Anti-sloppy-AI checklist

- [ ] Single issue scope — no unrelated changes, no drive-by refactors
- [ ] NEVER flood PRs — one focused PR per issue, no duplicates
- [ ] No placeholders, TODOs, or stub implementations
- [ ] Error paths handled explicitly, no silent failures
- [ ] Relevant tests run and passing (`cd backend && pytest` for backend, `cd frontend && npm test` for frontend; `npm run dev` smoke for frontend changes)
- [ ] No secrets, no hardcoded model names/URLs
