<div align="center">

<br/>

# 👻 Haunter

### Autonomous CI Healing · PR Review & Fix · Cloud Coding Sessions

<br/>

[![Frontend](https://img.shields.io/badge/Frontend-Next.js%2016%20%7C%20React%2019-black?style=flat-square&logo=nextdotjs)](https://nextjs.org/)
[![Backend](https://img.shields.io/badge/Backend-FastAPI%20%7C%20Python%203.11-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com/)
[![Compute](https://img.shields.io/badge/Compute-AWS%20Lambda%20(x86__64)-FF9900?style=flat-square&logo=amazon-aws)](https://aws.amazon.com/lambda/)
[![Edge Hosting](https://img.shields.io/badge/Edge-Cloudflare%20Workers-F38020?style=flat-square&logo=cloudflare)](https://workers.cloudflare.com/)
[![Database](https://img.shields.io/badge/Database-Neon%20Postgres%20(Async)-00E599?style=flat-square&logo=postgresql)](https://neon.tech/)
[![Sandbox](https://img.shields.io/badge/Sandbox-GitHub%20Actions%20CI-181717?style=flat-square&logo=githubactions)](https://github.com/features/actions)
[![Runtime Preview](https://img.shields.io/badge/In--Browser%20Preview-StackBlitz%20WebContainer-1389FD?style=flat-square)](https://webcontainers.io/)
[![Multi--Model](https://img.shields.io/badge/LLM-OpenCode%20Zen%20%7C%20OpenAI%20%7C%20Anthropic-7C3AED?style=flat-square)](https://opencode.ai/zen/v1)
[![License](https://img.shields.io/badge/License-Business%20Source%201.1-yellow?style=flat-square)](LICENSE)

<br/>

> **Haunter** is your autonomous engineering co-pilot — it heals broken CI pipelines, reviews and fixes pull requests, and gives your team a live cloud coding environment, all without ever touching your default branch uninvited.

<br/>

</div>

---

<br/>

## ⚡ CI Healing

When your GitHub Actions workflow fails, Haunter wakes up automatically.

It reads the failure, traces it to the root cause, generates a verified patch, and opens a pull request — all before you've had a chance to look at the logs.

```
Workflow fails  →  Root cause isolated  →  Fix generated  →  Sandboxed & verified  →  PR opened
```

- Distills raw CI logs and failing diffs into a precise root cause
- Generates a candidate patch with a confidence score, retrying on feedback
- Verifies every fix in an **ephemeral sandbox** — a mirror repo running real GitHub Actions CI — before proposing anything
- Opens a clean, auditable **fix PR** on success, or posts a structured **diagnosis comment** when exhausted

> **Human merge gate** — Haunter never auto-merges, never force-pushes, never touches your default branch.

<br/>

---

<br/>

## 🔍 PR Review & Fix

Haunter doesn't just review code — it acts on it.

Connect any repository and Haunter becomes an always-on auditor: reading every PR for security holes, regressions, architecture drift, and performance risks. When it finds something, it doesn't just comment — it opens a fix.

| Mode | What Haunter does |
|---|---|
| **Security Audit** | Finds vulnerabilities, unsafe patterns, and credential exposure |
| **Regression Review** | Flags logic changes that could silently break existing behaviour |
| **Architecture Audit** | Detects design drift and coupling violations |
| **Performance Review** | Identifies inefficient queries, unindexed paths, and hot-path regressions |
| **Auto-Fix** | Generates a verified patch PR for any finding it's confident about |

Governance presets (`autonomous`, `conservative`, `standard`, `audit_only`) let you tune how aggressively Haunter acts per repository.

<br/>

---

<br/>

## 🖥 Cloud Coding Sessions

A full development environment, in your browser, backed by real CI.

Haunter's **Live Studio** gives you a WebContainer-powered Node runtime paired with a Monaco diff editor. Start a session on any PR or branch, make changes, and hit **Run CI** — your changes are verified against real GitHub Actions before you ever push a commit.

- **In-browser runtime** — real Node.js, no Docker, no local setup
- **Monaco diff editor** — surgical patch editing with full syntax awareness
- **One-click CI verify** — patches run against your actual CI pipeline in the sandbox
- **Session checkpoints** — save and resume any session state across teammates
- **Multi-repo** — connect any GitHub repository; governance and spend caps apply per repo

<br/>

---

<br/>

## 📊 Full Audit Trail

Every action Haunter takes is logged, timestamped, and surfaced on the dashboard.

Run history, per-run agent traces, confidence scores, and cost breakdowns — complete transparency, zero black boxes. An **eval harness** with 20 golden fixtures lets you benchmark and compare behaviour across model or config changes.

<br/>

---

<br/>

<div align="center">

**Source-available under [Business Source License 1.1](LICENSE).** &nbsp;|&nbsp; Not OSI open source.

</div>
