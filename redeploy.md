# Haunter — Redeployment Runbook

> This file exists because agents and humans keep hitting the same gotchas every single
> time they deploy. **Read it top to bottom before touching anything.**
>
> Supersedes the old `aws.md`. Everything that was in it is preserved here, plus the
> security checklist that was added after a real credential leak.

---

## 0. Critical facts

| Fact | Detail |
|------|--------|
| **Terraform binary** | `C:\Terraform\terraform.exe` — NOT in PATH. Always absolute path. Never install it. |
| **MANDATORY DNS flag** | `$env:GODEBUG='netdns=cgo'` before **every single** Terraform invocation. Terraform's Go resolver intermittently fails on this host with `dial tcp: lookup lambda.us-east-1.amazonaws.com: no such host` while the Python AWS CLI works fine. This is not an optimisation. It has cost three failed deploys. |
| **Terraform working dir** | `C:\Users\bari2\Desktop\Haunter\infra\aws\` |
| **AWS credentials** | `C:\Users\bari2\.aws\credentials`, picked up automatically. Do NOT export `AWS_ACCESS_KEY_ID` manually. |
| **Secrets** | `infra/aws/terraform.tfvars` holds real secrets (GitHub PAT, provider API keys, Fernet key). **Never echo it to chat or logs. Never overwrite it.** It is gitignored on purpose. |
| **Lambda function names** | `haunter` (main) and `haunter-audit-dispatcher` (**not** `audit_dispatcher` — that's the Terraform *resource* name, `lambda.tf:417`) |
| **Runtime / arch** | Python 3.11, `x86_64` (FastAPI + Mangum via Function URL) |
| **Region** | `us-east-1` · **Account** `452258152602` |
| **Lambda zip** | Built at repo root: `C:\Users\bari2\Desktop\Haunter\lambda.zip` |
| **Zip builder** | `backend/rebuild_lambda_zip.py` — the ONLY correct way. |
| **Function URL** | `https://gjdbtzw5h36jhniqgdcxvhmjxu0tcjqr.lambda-url.us-east-1.on.aws/` |
| **Workers frontend** | `https://haunter.sufiyanx.workers.dev` (worker name `haunter`, `wrangler.jsonc`) |
| **Sandbox verifier** | GitHub Actions via test mirror repos (`backend/app/sandbox/github_actions_runner.py`). CodeBuild/GCP/Docker are retired. |
| **SSM** | `/haunter/GITHUB_SANDBOX_APP_PRIVATE_KEY` (SecureString; fetched at runtime with `WithDecryption=True`, cached in memory) |

---

## 1. Deployment policy — DEFAULT: code-only, URL preserved

**Unless the user explicitly asks for a new URL, always do a code-only apply.**
That means: rebuild the zip, then `plan` / `apply` with **no `terraform taint`**.

A code-only apply updates only `source_code_hash`. The Function URL is a separate,
stable resource and **does not change**. Therefore:

- The §6 URL checklist **does not apply**.
- GitHub Webhook Payload URL and GitHub OAuth callback URL **stay valid** — nothing to do.
- The Workers frontend does **not** need rebuilding against a new URL.

Only `terraform taint aws_lambda_function.haunter` when the user **explicitly** requests a
brand-new URL. Never infer it. Never taint "just to be safe" — tainting is what causes the
silent breakage §6 exists to prevent.

If a plan shows anything other than `source_code_hash` / `last_modified`, that is drift —
**stop and classify it**, don't apply.

---

## 2. Architecture — two parallel subagents

```
                    ┌──────────────────────────┐
   code-only apply  │  BACKEND SUBAGENT        │  owns: backend/, infra/, lambda.zip
   (URL unchanged)  │  terraform plan → apply  │  must NOT touch frontend/ or wrangler
                    └──────────────────────────┘
                                     ┌──────────────────────────┐
                                     │  FRONTEND SUBAGENT       │  owns: frontend/
                                     │  npm ci → lint → test →  │  must NOT run terraform
                                     │  build → wrangler deploy │  must NOT touch backend/
                                     └──────────────────────────┘
```

**These are safe to run in parallel** *because* the URL is preserved. They were previously
serialised: under a taint/new-URL deploy the frontend depends on a URL that does not exist
until the backend finishes, so a parallel build bakes the **old** URL into the bundle and the
dashboard silently fails to authenticate. If a new URL is ever requested, run them
**sequentially: backend first, capture the URL, then frontend.**

### Why one agent per stack, not one agent per step

Each stack has its own toolchain, its own failure modes, and its own credentials. A single
agent doing backend-then-frontend serially holds all of it in one context and cannot recover
if one half fails. Two agents fail independently and each can be re-run alone.

---

## 3. BACKEND subagent brief

### Hard rules
- **NEVER run `terraform taint`.** See §1.
- If the plan wants to *replace* or *recreate* a function, **STOP and report**.
- Do not edit repo files during a deploy. If something must be fixed, stop and report it.
- Do not `git add/commit/checkout/stash/reset` or change branches.
- Never echo `terraform.tfvars`.

### Step 1 — Confirm clean source
```powershell
git -C C:\Users\bari2\Desktop\Haunter status --porcelain
git -C C:\Users\bari2\Desktop\Haunter rev-parse --short HEAD
```
Confirm **CI is green on that exact SHA** before deploying anything:
```powershell
gh run list --branch main --limit 5 --json headSha,conclusion,status,workflowName
```
Uncommitted changes to `backend/app/**` mean you are deploying code nobody reviewed. **Stop.**

### Step 2 — Build the zip
```powershell
cd C:\Users\bari2\Desktop\Haunter\backend
python rebuild_lambda_zip.py
```
Installs production deps with `--platform manylinux2014_x86_64 --only-binary=:all:` — that flag
is **mandatory** or Lambda dies with `No module named 'asyncpg._asyncpg'`. Expect ~31–32 MB,
~4000 files. Takes several minutes. **Be patient, do not kill it.**

### Step 3 — 🔒 SECURITY VALIDATION (a real leak happened here)
Verify inside `lambda.zip` **before deploying**:

| # | Check | Expected |
|---|-------|----------|
| 1 | **No `.env`, `.env.*` entries** | **CRITICAL.** `backend/.env` holds 30 live secrets (GitHub App PEM, `DATABASE_URL`, `TOKEN_ENCRYPTION_KEY`, `GITHUB_TOKEN`, provider API keys). |
| 2 | No `terraform.tfvars` | absent |
| 3 | `mangum` present | present (builder hard-errors, verify anyway) |
| 4 | `asyncpg/_asyncpg*.so` is **manylinux x86_64** | not a Windows wheel — #1 cause of post-deploy 502 |
| 5 | `pytest`, `respx`, `freezegun`, `pytest-asyncio`, `pytest-anyio` absent | stripped |
| 6 | No `tests/` directory | absent |
| 7 | No `terraform.tfstate*` | absent |
| 8 | File count sane | ~4025 files / ~31 MB. **< 5 MB = dep install silently failed** |

**Check #1 is not theoretical.** `EXCLUDE_FILE_NAMES` in `rebuild_lambda_zip.py` used to
contain only `{"rebuild_lambda_zip.py"}`, so `.env` was bundled and **shipped to production
twice**. The fix excludes `.env*`. If a future edit to that builder or to the copy loop
reintroduces dotfile copying, the leak returns. **Verify the zip contents every single time —
do not assume the builder is still correct.**

Verify with:
```powershell
Add-Type -AssemblyName System.IO.Compression.FileSystem
$z=[System.IO.Compression.ZipFile]::OpenRead("C:\Users\bari2\Desktop\Haunter\lambda.zip")
$z.Entries | Where-Object { $_.FullName -match '(^|/)\.env' }
$z.Dispose()
```
Empty output = clean.

### Step 4 — Expected hash
```powershell
python -c "import base64,hashlib; d=open(r'C:\Users\bari2\Desktop\Haunter\lambda.zip','rb').read(); print(base64.b64encode(hashlib.sha256(d).digest()).decode())"
```

### Step 5 — Plan and AUDIT
```powershell
$env:GODEBUG='netdns=cgo'
& 'C:\Terraform\terraform.exe' -chdir='C:\Users\bari2\Desktop\Haunter\infra\aws' plan -no-color
```
Expected: **`0 to add, 2 to change, 0 to destroy`** — `haunter` **and**
`haunter-audit-dispatcher`, both sharing the same zip, so both hashes change together.
The only changed attributes must be `source_code_hash` and `last_modified`.

**Any new resource, replacement, destroy, IAM change, or env-var change → STOP. Report the
full plan. Do not apply.** To confirm precisely which attributes changed:
```powershell
& 'C:\Terraform\terraform.exe' -chdir='...\infra\aws' plan -no-color 2>&1 |
  Out-File -Encoding utf8 "$env:TEMP\plan.txt"
Select-String -Path "$env:TEMP\plan.txt" -Pattern '^\s*[~+-]' | Where-Object { $_.Line -match '=' } |
  ForEach-Object { ($_.Line -replace '^\s*[~+-]\s*','').Split('=')[0].Trim() } | Group-Object
```
(`-out` writes the *binary* plan format — don't grep that.)

### Step 6 — Apply (only if the plan was clean)
```powershell
$env:GODEBUG='netdns=cgo'
& 'C:\Terraform\terraform.exe' -chdir='C:\Users\bari2\Desktop\Haunter\infra\aws' apply '-target=aws_lambda_function.haunter' -auto-approve -no-color
& 'C:\Terraform\terraform.exe' -chdir='C:\Users\bari2\Desktop\Haunter\infra\aws' apply '-target=aws_lambda_function.audit_dispatcher' -auto-approve -no-color
```
> **Quote the `-target`.** PowerShell splits on the `.` and Terraform fails with
> `Error: Too many command line arguments` / `Invalid target "aws_lambda_function"`.

**Deploy BOTH functions.** They share the zip, so scoping to only `haunter` leaves the
dispatcher running old code — and, historically, old code with the secrets still in it.

Upload takes **6–7 minutes** emitting `Still modifying...`. **Normal. Do not cancel.** Allow
a ≥900 s timeout.

### Step 7 — Verify
```powershell
# 1. Hashes must match step 4 — BOTH functions
$expected="<hash from step 4>"
foreach ($fn in @("haunter","haunter-audit-dispatcher")) {
  $h = aws lambda get-function-configuration --function-name $fn --region us-east-1 --query CodeSha256 --output text
  "$fn : $(if ($h -eq $expected) {'OK'} else {"DRIFT $h"})"
}

# 2. Health
curl https://gjdbtzw5h36jhniqgdcxvhmjxu0tcjqr.lambda-url.us-east-1.on.aws/health   # {"status":"ok"}

# 3. Logs — must be free of fatal patterns
aws logs filter-log-events --log-group-name /aws/lambda/haunter --region us-east-1 `
  --start-time ([DateTimeOffset]::UtcNow.AddMinutes(-25).ToUnixTimeMilliseconds()) `
  --query "events[*].message" --output text |
  Select-String "ModuleNotFoundError|ImportError|SyntaxError|Traceback|CRITICAL"
```
**Non-atomic warning:** Terraform writes `environment` and `source_code_hash` in **separate**
API calls. A partial failure can leave the function on **old code with new env vars**. Detect
it by the hash mismatch; the remedy is to re-run apply (idempotent).

---

## 4. FRONTEND subagent brief

### Hard rules
- **Do NOT run terraform.** Do NOT touch `backend/`, `infra/`, or `lambda.zip`.
- Do not edit `.env` files, `wrangler.jsonc`, `next.config.ts`, or any source file. If
  something is wrong with them, **stop and report** — don't fix.
- Only generated output dirs may change as build side effects (`frontend/.next`,
  `frontend/.wrangler`, `frontend/out`, `frontend/node_modules`).

### Step 1 — 🔒 SECURITY / SAFETY PRE-FLIGHT (do this BEFORE building)

**1a. Prove which worker you are about to publish to.** Read `frontend/wrangler.jsonc` fully:
worker `name`, every `env` block, and every binding (KV/R2/D1/DO/services/vars).

> **Known footgun:** `package.json` defines `deploy`, `deploy:preview` and `deploy:prod` as
> **byte-identical** commands (`wrangler deploy`). Today there is a single top-level config
> with no `env` blocks, so all three hit production worker `haunter`. **If that ever changes,
> plain `npm run deploy` could silently publish to a preview.** Re-prove it every time; do not
> assume. If you cannot prove which worker the command targets, **STOP.**

**1b. Env precedence trap.** Read `frontend/.env.local` **and** `frontend/.env`.
Next.js precedence is `.env.local` > `.env.production` > `.env`. If
`NEXT_PUBLIC_API_URL` is set (uncommented) in `.env.local` **or** `.env`, it **overrides**
production and bakes the wrong API into the bundle.

> Currently: `.env.local` has it commented out (safe). `frontend/.env:2` **does** set
> `NEXT_PUBLIC_API_URL=http://localhost:7555` — harmless today only because
> `.env.production` outranks `.env`. **That is one uncomment away from shipping localhost to
> production.** Report it every deploy; consider removing that line.

**1c. Localhost fallback.** `frontend/next.config.ts:8` has a `http://localhost:7555` fallback.
Confirm it only applies in development. If a production build could fall back to localhost,
**STOP** — the dashboard would silently talk to nothing.

**1d. Secret safety.** Never print values from `.env*` beyond the public API URL (the Function
URL is not secret). Compare fingerprints for anything that is.

### Step 2 — Build
```powershell
cd C:\Users\bari2\Desktop\Haunter\frontend
npm ci            # node_modules is often empty on a fresh clone; without it lint/test fail
npm run lint
npm run test
npm run build
```
`npm run lint` and `npm run test` **must pass before building.** If either fails, **STOP and
report** — do not fix, and do not deploy a failing build.

### Step 3 — Deploy
Use the command proven in 1a (`npm run deploy:prod`). Record the published URL and version id.

### Step 4 — Verify
```powershell
curl -s -I https://haunter.sufiyanx.workers.dev          # expect 200
Select-String -Path "frontend/out/**/*.js" -Pattern "localhost:7555"    # expect NOTHING
Select-String -Path "frontend/out/**/*.js" -Pattern "gjdbtzw5h36jhniqgdcxvhmjxu0tcjqr"  # expect a hit
```
Also confirm `/login`, `/runs`, `/eval`, `/config` return 200 and the main JS asset loads.

---

## 5. Combined post-deploy checklist

- [ ] Both Lambda `CodeSha256` match the locally computed zip hash
- [ ] `/health` → `{"status":"ok"}`
- [ ] CloudWatch: zero `ModuleNotFoundError` / `ImportError` / `SyntaxError` / `Traceback`
- [ ] `zip` contains **no `.env*`** (§3 step 3)
- [ ] Frontend returns 200; bundle has the correct API URL and **zero** `localhost:7555`
- [ ] Function URL unchanged (code-only deploy) — so **no** GitHub webhook / OAuth edits needed
- [ ] Note `npm audit` findings separately; they do not block a deploy

---

## 6. URL-change checklist — ONLY when a new URL is explicitly requested

`t && terraform apply` mints a **brand-new random URL**. Only then:

| # | Where | Change | Manual? |
|---|-------|--------|---------|
| 1 | `redeploy.md` §0 Function URL row | new URL | no |
| 2 | `backend/.env` | `CALLBACK_URL`, `FRONTEND_URL` | no |
| 3 | `terraform.tfvars` → `callback_url`, `frontend_url` | new URL | no |
| 4 | **GitHub Webhook Payload URL** | append `/webhooks/github` | **yes — GitHub UI** |
| 5 | **GitHub OAuth callback URL** | must match `CALLBACK_URL` exactly | **yes — GitHub UI** |
| 6 | `frontend/.env.production` | `NEXT_PUBLIC_API_URL` | no |

Missing #4 → `error: page not found` on webhook deliveries.
Missing #5 → `404 on /auth/callback` after a successful OAuth login.
Missing #6 → dashboard can't log in.
Missing #1/#2/#3 → old URL referenced somewhere.

> **Parked (Phase 5):** pin a custom domain to the Function URL so deploys stop changing the
> address. Until then this checklist is manual and mandatory after any taint.

---

## 7. Rollback

```powershell
git checkout <previous-sha>          # or the previous branch
cd backend; python rebuild_lambda_zip.py
$env:GODEBUG='netdns=cgo'
& 'C:\Terraform\terraform.exe' -chdir='...\infra\aws' apply '-target=aws_lambda_function.haunter' -auto-approve -no-color
& 'C:\Terraform\terraform.exe' -chdir='...\infra\aws' apply '-target=aws_lambda_function.audit_dispatcher' -auto-approve -no-color
```
Terraform detects the hash changed back and re-uploads. Lambda swaps code atomically.

---

## 8. Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `terraform: command not found` | not in PATH | use `& 'C:\Terraform\terraform.exe'` |
| `No module named 'mangum'` | zip built with bare `zip -r` | rebuild with `rebuild_lambda_zip.py` |
| `No module named 'asyncpg._asyncpg'` | host-platform wheels | `rebuild_lambda_zip.py` passes `--platform manylinux2014_x86_64` |
| Plan shows 0 changes but code changed | zip not rebuilt before plan | rebuild first |
| `dial tcp: lookup lambda... no such host` | Go resolver on this host | `$env:GODEBUG='netdns=cgo'` (**mandatory**) |
| `Too many command line arguments` | unquoted `-target` | quote `'-target=...'` |
| `InvalidSignatureException: Signature expired` | local Windows clock drift | wait for time sync; retrying immediately fails again |
| Terraform hangs, no output | stale `infra/aws/.terraform.tfstate.lock.info` from a killed apply | confirm no terraform process, delete the file, retry |
| `Provider produced inconsistent result` | transient AWS hiccup | re-run apply (idempotent) |
| 502 on every webhook after deploy | import/syntax error in deployed code | check CloudWatch immediately, roll back |
| **NEW env vars but OLD code** | non-atomic apply | compare `CodeSha256` to zip hash; re-run apply |
| Deploy looks clean but secrets leak | dotfiles re-copied into zip | verify zip contents, §3 step 3 check #1 |
| `Invalid function argument` on `filebase64sha256` | `lambda.zip` missing | build it first |

---

## 9. Environment variables on Lambda

Injected via `infra/aws/lambda.tf` `environment` block from `terraform.tfvars`.
**Do not change them in the AWS console** — the next apply overwrites them.

| Variable | Purpose |
|---|---|
| `HOSTING_PROVIDER` | `aws` — Lambda async self-invoke for post-response pipeline work |
| `SANDBOX_PROVIDER` | `github_actions` — CI verification via mirror repos |
| `DATABASE_URL` | pooled Neon URL (app uses `NullPool`) |
| `DATABASE_URL_UNPOOLED` | direct Neon URL — Alembic migrations only |

> **`SESSION_SECRET_KEY`:** the prod value comes from `var.session_secret_key`
> (`lambda.tf:174`). `backend/.env` is local-only — a cookie signed with the local value
> returns `401` against prod. Compare SHA-256 fingerprints, never print values.

> **New settings added to `config.py` need no tfvars change if they declare a default.**
> Check before assuming a redeploy requires infra edits:
> `git diff <old>..<new> -- backend/app/config.py | Select-String '^\+.*:.*='`