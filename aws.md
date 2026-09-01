# Haunter — AWS Lambda Deployment Runbook

> **This file exists because agents (and humans) keep hitting the same 3-5 gotchas
> every single time they try to deploy or update the Lambda function.
> Read it top to bottom before touching anything.**

---

## 0. Critical facts — read before invoking any tool

| Fact | Detail |
|------|--------|
| **Terraform binary** | `C:\Terraform\terraform.exe` — NOT in PATH, NOT in WSL `/usr/bin`. Always call by absolute path. |
| **Terraform working dir** | `infra/aws/` relative to repo root, i.e. `C:\Users\bari2\Desktop\Haunter\infra\aws\` |
| **AWS credentials** | Live at `C:\Users\bari2\.aws\credentials`. Picked up automatically by the AWS provider. Do NOT export `AWS_ACCESS_KEY_ID` manually. |
| **Secrets** | `infra/aws/terraform.tfvars` contains real secrets (GitHub PAT, OpenCode Zen API key, Fernet key). Never echo its contents to chat/logs. Never overwrite it. It is `.gitignore`'d for a reason. |
| **Lambda function name** | `haunter` |
| **Region** | `us-east-1` |
| **Lambda zip** | Built at repo root: `C:\Users\bari2\Desktop\Haunter\lambda.zip` |
| **Deployment zip builder** | `backend/rebuild_lambda_zip.py` — the ONLY correct way to build the zip. |
| **Function URL** | `https://gjdbtzw5h36jhniqgdcxvhmjxu0tcjqr.lambda-url.us-east-1.on.aws/` |

---

## 1. The ONE correct way to invoke Terraform from PowerShell

```powershell
# Plan (dry-run — always do this first)
& 'C:\Terraform\terraform.exe' -chdir='C:\Users\bari2\Desktop\Haunter\infra\aws' plan -no-color

# Apply (only after reviewing the plan and getting user approval)
& 'C:\Terraform\terraform.exe' -chdir='C:\Users\bari2\Desktop\Haunter\infra\aws' apply -auto-approve -no-color
```

From **WSL bash** (if you're in a bash context):
```bash
/mnt/c/Terraform/terraform.exe -chdir='C:\Users\bari2\Desktop\Haunter\infra\aws' plan -no-color
```

> **STOP.** Do NOT try `terraform plan`, `winget install terraform`, `apt install terraform`, or
> any other install attempt. Terraform is already installed at `C:\Terraform\terraform.exe`.
> It is just not in PATH. Use the absolute path above, every single time, no exceptions.

---

## 2. Deploying a code change to Lambda

### Step 1 — Rebuild the zip

```powershell
cd C:\Users\bari2\Desktop\Haunter\backend
python rebuild_lambda_zip.py
```

This script:
- Reads `requirements.txt`, strips test-only deps (`pytest`, `respx`, `freezegun`, `pytest-asyncio`, `pytest-anyio`)
- Installs production deps using `--platform manylinux2014_x86_64 --only-binary=:all:` so wheels are compatible with Lambda's Linux runtime (critical — building on Windows/Mac without this flag produces broken native extensions)
- Copies the `app/` tree, `main.py`, `lambda_handler.py` on top of the deps
- Writes `lambda.zip` to repo root (`C:\Users\bari2\Desktop\Haunter\lambda.zip`)
- Verifies `mangum` is present in the bundle (hard error if missing)

Expected output:
```
Wrote C:\Users\bari2\Desktop\Haunter\lambda.zip (NNNN files, NN.NN MB)
```

> The zip is ~39–41 MB. If it's < 5 MB, the deps install step silently failed — check stderr.

### Step 2 — Audit the plan (mandatory before apply)

```powershell
& 'C:\Terraform\terraform.exe' -chdir='C:\Users\bari2\Desktop\Haunter\infra\aws' plan -no-color
```

For a code-only update you should see exactly **1 change**:
```
~ aws_lambda_function.haunter will be updated in-place
  ~ source_code_hash = "OLD_HASH" -> "NEW_HASH"

Plan: 0 to add, 1 to change, 0 to destroy.
```

If you see more changes than `source_code_hash`, stop and classify each one before applying:
- **(a) Desired** — matches an intentional `.tf` edit on disk
- **(b) Drift** — AWS drifted from config (rare; happens if someone edited the function in the console)
- **(c) Noise** — env var re-apply with unchanged value (safe to apply)

> **`haunter` is the production webhook handler.** A bad apply will 502 every failing CI run
> until rolled back. Never apply without a plan review and explicit user approval.

### Step 3 — Verify the hash matches what you expect

Cross-check what's live vs. what's in the zip:

```powershell
# Hash of the local zip (what Terraform will upload)
python -c "import base64, hashlib; data=open('lambda.zip','rb').read(); print(base64.b64encode(hashlib.sha256(data).digest()).decode())"

# Hash of the live Lambda code
aws lambda get-function --function-name haunter --region us-east-1 --query "Configuration.CodeSha256" --output text
```

If they match → no code update needed (plan should be a no-op already).
If they differ → the zip needs to be deployed.

### Step 4 — Apply (after user approval)

```powershell
& 'C:\Terraform\terraform.exe' -chdir='C:\Users\bari2\Desktop\Haunter\infra\aws' apply -auto-approve -no-color
```

A 39 MB zip takes **3–4 minutes** to upload. The `Still modifying...` polling lines are normal — do not cancel.

---

## 3. How `source_code_hash` works (and why you must use `rebuild_lambda_zip.py`)

In `infra/aws/lambda.tf`:
```hcl
source_code_hash = filebase64sha256(var.lambda_zip_path)
```

Terraform computes a SHA-256 of the zip at plan time and compares it to the hash stored in `terraform.tfstate`. If the hashes differ, Terraform uploads the new zip. If they match, it's a no-op.

This means:
- Editing code **without rebuilding the zip** → Terraform sees no change → old code stays deployed. Always rebuild first.
- Building the zip **without using `rebuild_lambda_zip.py`** (e.g. bare `zip -r`) → missing production dependencies → Lambda crashes with `ModuleNotFoundError: No module named 'mangum'` on every request.

---

## 4. Infra layout

```
infra/aws/
├── lambda.tf          # Lambda function, Function URL, IAM role, permissions
├── codebuild.tf       # CodeBuild sandbox project (Phase 13)
├── variables.tf       # All input variables (secrets marked sensitive=true)
├── outputs.tf         # Function URL, ARNs, log group names
├── terraform.tfvars   # ⚠️ SECRETS — gitignored, never echo, never overwrite
└── terraform.tfstate  # Local state — do not manually edit
```

Key resources currently in state:

| Resource | ID / ARN |
|---|---|
| Lambda function | `haunter` |
| Lambda Function URL | `haunter` |
| Lambda IAM role | `haunter-lambda-role` |
| CloudWatch log group | `/aws/lambda/haunter` |
| CodeBuild project | `haunter-sandbox` |
| SSM param (GitHub token) | `/haunter/GITHUB_TOKEN` |
| Account ID | `452258152602` |

---

## 5. Verification after deploy

```powershell
# Confirm live hash matches what you just uploaded
aws lambda get-function --function-name haunter --region us-east-1 `
  --query "Configuration.{CodeSha256:CodeSha256,LastModified:LastModified}" `
  --output json

# Tail live logs (requires aws CLI with CloudWatch Logs access)
aws logs filter-log-events `
  --log-group-name /aws/lambda/haunter `
  --region us-east-1 `
  --start-time ([DateTimeOffset]::UtcNow.AddMinutes(-10).ToUnixTimeMilliseconds()) `
  --query "events[*].message" `
  --output text
```

---

## 6. Rollback

If a deploy causes issues, the fastest rollback is:
1. Restore the previous version of the changed Python files (via `git checkout` or `git stash`)
2. Run `python backend/rebuild_lambda_zip.py` to rebuild the zip from the old code
3. Run `terraform apply -auto-approve` — Terraform detects the hash changed back and uploads the previous bundle

Lambda swaps code atomically. There is no blue/green here, but upload + activation is fast (~30s once the zip is fully uploaded).

---

## 7. Common failure modes

| Symptom | Root cause | Fix |
|---|---|---|
| `terraform: command not found` (PowerShell) | `terraform` not in PATH | Use `& 'C:\Terraform\terraform.exe'` |
| `/bin/bash: terraform: command not found` | WSL PATH doesn't include Windows dirs | Use `/mnt/c/Terraform/terraform.exe` |
| `ModuleNotFoundError: No module named 'mangum'` | Zip built with bare `zip -r`, deps not included | Rebuild with `rebuild_lambda_zip.py` |
| `ModuleNotFoundError: No module named 'asyncpg._asyncpg'` | Deps built with host platform wheels (Windows/Mac), not Linux | Always use `rebuild_lambda_zip.py` — it passes `--platform manylinux2014_x86_64 --only-binary=:all:` |
| Plan shows 0 changes but old code is still live | Forgot to rebuild zip before running `plan` | Run `rebuild_lambda_zip.py` first, then re-run `plan` |
| `Error: Provider produced inconsistent result` | Transient AWS API hiccup | Re-run `terraform apply` — it's idempotent |
| 502 on every webhook after deploy | Import error or syntax error in deployed code | Check `/aws/lambda/haunter` CloudWatch logs immediately; rollback |
| `Error: Invalid function argument` on `filebase64sha256` | `lambda.zip` does not exist yet | Run `rebuild_lambda_zip.py` first |
| Plan shows 0 changes but you know code changed | `rebuild_lambda_zip.py` wasn't run after the code edit | Always rebuild before planning |

---

## 8. Environment variables on the Lambda function

All env vars are injected via Terraform (`infra/aws/lambda.tf` `environment` block) and sourced from `terraform.tfvars`. **Do not change them in the AWS console** — they will be overwritten on the next `terraform apply`.

Key vars:

| Variable | Value | Purpose |
|---|---|---|
| `HOSTING_PROVIDER` | `aws` | Tells orchestrator to use Lambda async self-invoke |
| `SANDBOX_PROVIDER` | `github_actions` | Tells sandbox to use GitHub Actions CI (post-Phase 17 cleanup; CodeBuild is gone) |
| `DATABASE_URL` | (pooled Neon URL) | App queries — NullPool in app, Neon PgBouncer pools |
| `DATABASE_URL_UNPOOLED` | (direct Neon URL) | Alembic migrations only |

> **Post-cleanup note (Phase 17, 2026-09-01):** CodeBuild and GCP Cloud Build are removed. `AWS_CODEBUILD_PROJECT_NAME` env var is gone. The only sandbox provider is `github_actions`; the only hosting provider is `aws` (Lambda). There is no rollback path to GCP — that stack is dead code.

---

## 9. Post-deploy URL-change checklist

`terraform taint aws_lambda_function.haunter && terraform apply` (the standard redeploy) **assigns a brand-new random URL to the Lambda Function URL** every time. After every deploy, update the URL in **every** place that hardcodes it. Missing one = silent breakage.

| # | Where | What to change |
|---|---|---|
| 1 | `aws.md` line 21 | This file's `Function URL` row in the Quick reference table |
| 2 | `backend/.env` (if using locally) | `CALLBACK_URL` and `FRONTEND_URL` |
| 3 | Lambda env vars (set by terraform, no manual edit) | `CALLBACK_URL` and `FRONTEND_URL` are injected from `terraform.tfvars` — `terraform apply` updates them automatically |
| 4 | GitHub Webhook Payload URL | https://github.com/<owner>/<repo>/settings/hooks → click Edit → paste new URL + `/webhooks/github` |
| 5 | GitHub OAuth App Authorization callback URL | https://github.com/settings/developers → your OAuth App → "Authorization callback URL" → paste new URL + `/auth/callback`. **Must match `CALLBACK_URL` exactly or login breaks** |
| 6 | Cloudflare Pages env var | `NEXT_PUBLIC_API_URL` in the project's Settings → Environment variables |
| 7 | Terraform state (`terraform.tfvars`) | `callback_url` and `frontend_url` — committed locally, used by next deploy |

**Verify after deploy:** `curl <new-url>/health` should return `{"status":"ok"}`. If it 404s, the URL is wrong or the Function URL didn't update.

**Symptom → likely cause:**
- Dashboard says `{"Message":null}` on login → API Gateway "not found" — old URL still cached somewhere
- `404 on /auth/callback` after successful GitHub OAuth → GitHub OAuth App callback URL or Lambda `CALLBACK_URL` is stale (most common culprit)
- Lambda `/health` works but dashboard can't login → frontend `NEXT_PUBLIC_API_URL` is stale
- `error: page not found` from webhook deliveries → GitHub Webhook Payload URL is stale

**Mitigation (parked, Phase 5):** pin a custom domain to the Lambda Function URL so deploys don't change the address. Until then, this checklist is mandatory after every redeploy.
