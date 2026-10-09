<div align="center">

<br/>

<img src="https://img.shields.io/badge/-%F0%9F%91%BB%20Haunter-1a1a2e?style=for-the-badge&labelColor=1a1a2e" alt="Haunter" height="42"/>

<h3>Your engineering team's autonomous AI agent.<br/>It reviews code, heals CI, and ships fixes — before you've opened Slack.</h3>

<br/>

[![Frontend](https://img.shields.io/badge/Frontend-Next.js%2016%20%7C%20React%2019-black?style=flat-square&logo=nextdotjs)](https://nextjs.org/)
[![Backend](https://img.shields.io/badge/Backend-FastAPI%20%7C%20Python%203.11-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com/)
[![Compute](https://img.shields.io/badge/Compute-AWS%20Lambda%20(x86__64)-FF9900?style=flat-square&logo=amazon-aws)](https://aws.amazon.com/lambda/)
[![Edge Hosting](https://img.shields.io/badge/Edge-Cloudflare%20Workers-F38020?style=flat-square&logo=cloudflare)](https://workers.cloudflare.com/)
[![Database](https://img.shields.io/badge/Database-Neon%20Postgres%20(Async)-00E599?style=flat-square&logo=postgresql)](https://neon.tech/)
[![Sandbox](https://img.shields.io/badge/Sandbox-GitHub%20Actions%20CI-181717?style=flat-square&logo=githubactions)](https://github.com/features/actions)
[![Runtime Preview](https://img.shields.io/badge/In--Browser%20Preview-StackBlitz%20WebContainer-1389FD?style=flat-square)](https://webcontainers.io/)
[![License](https://img.shields.io/badge/License-Business%20Source%201.1-yellow?style=flat-square)](LICENSE)

<br/>

</div>

---

<br/>

<details>
<summary><strong>📋 &nbsp;Table of Contents</strong></summary>

<br/>

- [PR Review & Fix](#-pr-review--fix)
- [Cloud Coding Sessions](#-cloud-coding-sessions)
- [Full Audit Trail](#-full-audit-trail)
- [License](#license)

</details>

<br/>

---

<br/>

## 🔍 PR Review & Fix

> Haunter doesn't just leave comments. It opens fixes.

Connect any GitHub repository and Haunter becomes an always-on code intelligence layer. Every pull request gets reviewed across four dimensions — and every finding it's confident about gets a verified, sandbox-tested fix PR attached to it.

<br/>

**What Haunter reviews:**

| Dimension | What it catches |
|:---|:---|
| 🔒 **Security** | Vulnerabilities, unsafe patterns, secrets exposure, injection risks |
| 🔁 **Regressions** | Logic changes that silently break existing behaviour |
| 🏛 **Architecture** | Design drift, coupling violations, structural anti-patterns |
| ⚡ **Performance** | Inefficient queries, unindexed paths, hot-path regressions |

<br/>

**When CI breaks, Haunter heals it — automatically.**

A failing workflow is just another kind of PR problem. Haunter reads the failure, traces it to the exact root cause, generates a patch, and runs it through an **ephemeral sandbox** (a real mirror repo on GitHub Actions) before proposing anything. No guessing. No noise. Just a clean fix PR.

```
CI fails  →  Root cause traced  →  Fix generated  →  Sandboxed in real CI  →  Fix PR opened
```

> **Human merge gate** — Haunter never auto-merges, never force-pushes, never touches your default branch uninvited.

<br/>

<details>
<summary><strong>Governance presets</strong></summary>

<br/>

Configure per repository how aggressively Haunter acts:

| Preset | Behaviour |
|:---|:---|
| `autonomous` | Reviews + fixes + CI healing, fully automated |
| `conservative` | Reviews only; fixes require explicit approval |
| `standard` | Reviews + fixes for high-confidence findings only |
| `audit_only` | Read-only audits; never opens branches or PRs |
| `custom` | Full control over confidence thresholds and spend caps |

</details>

<br/>

---

<br/>

## 🖥 Cloud Coding Sessions

> A real development environment. In your browser. Backed by your actual CI.

Haunter's **Live Studio** is a full cloud coding environment where your team can tackle bugs, implement new features, and ship changes — without touching a local machine.

<br/>

**What you get in a session:**

| Capability | Details |
|:---|:---|
| 🟢 **Real Node.js runtime** | WebContainer-powered — full npm ecosystem, no Docker, no local setup |
| 📝 **Monaco diff editor** | VS Code-grade editing with surgical patch precision |
| 🚀 **One-click CI verify** | Your changes run against your real GitHub Actions pipeline in the sandbox before you push a single commit |
| 💾 **Session checkpoints** | Save and resume any session; hand off to a teammate mid-work |
| 🔗 **Multi-repo** | Connect any GitHub repository; governance and spend caps apply per repo |

<br/>

Whether you're **debugging an open issue**, **implementing a new feature**, or **iterating on a Haunter-suggested fix** — Live Studio gives you the full development loop in the cloud. Write → verify against real CI → push. No environment setup, no pipeline surprises.

<br/>

---

<br/>

## 📊 Full Audit Trail

Every action Haunter takes is logged, timestamped, and surfaced on the dashboard — confidence scores, cost, latency, and full per-run agent traces. Nothing is a black box.

An **eval harness** with 20 golden fixtures lets you benchmark and compare quality across any configuration change.

<br/>

---

<br/>

<div align="center">

**Source-available — [Business Source License 1.1](LICENSE).** &nbsp;&nbsp;Not OSI open source.

<br/>

*Built for teams that ship.*

</div>
