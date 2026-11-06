<div align="center">

<h3>Your personal 24/7 GitHub bot.<br/>It reviews your PRs, fixes findings & heals CI — while you sleep.</h3>

Open source, self-hosted, yours. Bring your own keys and point it at your repos — there is no hosted service and no signup.

<br/>

[![Frontend](https://img.shields.io/badge/Frontend-Next.js%2016%20%7C%20React%2019-black?style=flat-square&logo=nextdotjs)](https://nextjs.org/)
[![Backend](https://img.shields.io/badge/Backend-FastAPI%20%7C%20Python%203.11-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com/)
[![Database](https://img.shields.io/badge/Database-Neon%20Postgres%20(Async)-00E599?style=flat-square&logo=postgresql)](https://neon.tech/)
[![Sandbox](https://img.shields.io/badge/Sandbox-GitHub%20Actions%20CI-181717?style=flat-square&logo=githubactions)](https://github.com/features/actions)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue?style=flat-square)](LICENSE)

*Self-host stack — you run the frontend + backend yourself and deploy on your own infra.*

<br/>

</div>

---

---

<br/>

## 🔍 Your personal PR & DevOps bot

Fork this repo, point it at your GitHub repositories, and Haunter works for you around the clock. Every pull request gets reviewed across four dimensions — with confident findings reported in the review.

<br/>

**What Haunter reviews:**

| Dimension | What it catches |
|:---|:---|
| 🔒 **Security** | Vulnerabilities, unsafe patterns, secrets exposure, injection risks |
| 🔁 **Regressions** | Logic changes that silently break existing behaviour |
| 🏛 **Architecture** | Design drift, coupling violations, structural anti-patterns |
| ⚡ **Performance** | Inefficient queries, unindexed paths, hot-path regressions |

<br/>

A failing workflow is just another kind of PR problem. Haunter reads the failure, traces it to the exact root cause, generates a patch, and runs it through a **sandbox mirror** (a real mirror repo on GitHub Actions, on your own infra) before proposing anything. No guessing. No noise. Just a clean fix PR.

```
CI fails  →  Root cause traced  →  Fix generated  →  Sandboxed in real CI  →  Fix PR opened
```

> **Human merge gate** — Haunter never auto-merges, never force-pushes, never touches your default branch uninvited.

<br/>

**Governance presets** — configure per repository in your own deployment, choosing how aggressively Haunter acts:

| Preset | Behaviour |
|:---|:---|
| `autonomous` | Reviews + fixes + CI healing, fully automated |
| `conservative` | Reviews only; fixes require explicit approval |
| `standard` | Reviews + fixes for high-confidence findings only |
| `audit_only` | Read-only audits; never opens branches or PRs |
| `custom` | Full control over confidence thresholds and spend caps |

---

## 🚀 Self-host quickstart

Run the whole thing on your own machine in two terminals. Prerequisites: **Node 20+** and **Python 3.11**.

**1. Clone it.**

```
git clone https://github.com/kaiizer777/Haunter.git
cd Haunter
```

**2. Backend (terminal 1 — port 7555).**

```
cd backend
cp .env.example .env   # then fill in your own keys: DATABASE_URL, GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET,
                       # FRONTEND_URL, OPENCODE_ZEN_API_KEY, GITHUB_WEBHOOK_SECRET, GITHUB_TOKEN
python -m venv .venv
.venv\Scripts\activate   # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn main:app --reload --port 7555
```

**3. Frontend (terminal 2 — port 3011).**

```
cd frontend
npm install
npm run dev   # http://localhost:3011
```

**4. Point it at your repo.** Add a webhook on your GitHub repository targeting your backend URL (`POST /webhooks/github`, HMAC-signed with your `GITHUB_WEBHOOK_SECRET`). For local smoke tests, skip GitHub and fire the real pipeline directly:

```
cd backend
python scripts/trigger_local_review.py --repo <owner>/<repo> --pr <number> --dry-run --secret devsecret
```

(A `queued` response means accepted — watch the uvicorn log for the pipeline run. `GITHUB_WEBHOOK_SECRET` in `backend/.env` must match the `--secret` value.)

**Deploy yours when ready** — same code, your infra: backend on AWS Lambda, the dashboard SPA on Cloudflare Workers, Postgres on Neon. Your keys, your data, your bot, 24/7.

---

## 📊 Full Audit Trail

Every action Haunter takes is logged, timestamped, and surfaced on your local-first dashboard — confidence scores, cost, latency, and full per-run agent traces. Nothing is a black box, and the data lives in your own database.

An **eval harness** with 20 golden fixtures lets you benchmark and compare quality across any configuration change.

<br/>

---

<div align="center">

**Open source — [Apache License 2.0](LICENSE).** Fork it, self-host it, make it yours.

</div>
