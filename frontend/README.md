# Haunter Dashboard Frontend

The web dashboard for [Haunter](file:///C:/Users/bari2/Desktop/Haunter/README.md) — an autonomous CI failure diagnosis and fix agent. The dashboard provides run history, detailed step-by-step diagnostic traces, eval harness results, active repository controls, and live model switching.

## Tech Stack
- **Framework**: [Next.js](https://nextjs.org/) 16 (App Router)
- **UI Library**: React 19, [Tailwind CSS](https://tailwindcss.com/) v4, Lucide React
- **Architecture**: Single Page Application (SPA) Static Export (`output: "export"` in [`next.config.ts`](file:///C:/Users/bari2/Desktop/Haunter/frontend/next.config.ts))
- **Hosting**: [Cloudflare Workers Static Assets](https://developers.cloudflare.com/workers/static-assets/) configured via [`wrangler.jsonc`](file:///C:/Users/bari2/Desktop/Haunter/frontend/wrangler.jsonc) (`haunter-ci-agent`)
- **Testing**: [Vitest](https://vitest.dev/) with `@testing-library/react`
- **Backend API**: AWS Lambda (FastAPI + Mangum via Lambda Function URL in `us-east-1`)

## Environment Variables

The dashboard compiles as a client-side static bundle. Environment variables must be set at build time in `frontend/.env.local`:

| Variable | Description | Example |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | Backend AWS Lambda Function URL (no trailing slash or space) | `https://gjdbtzw5h36jhniqgdcxvhmjxu0tcjqr.lambda-url.us-east-1.on.aws` |

> **Note**: If the AWS Lambda Function URL changes after a backend redeployment, update `NEXT_PUBLIC_API_URL` in `.env.local`, re-run `npm run build`, and redeploy with `npm run deploy`.

## Development Commands

```bash
# Install dependencies
npm install

# Start local Next.js dev server (http://localhost:3000)
npm run dev

# Run Vitest component & unit tests
npm test

# Run tests in watch mode
npm run test:watch
```

## Build & Deployment (Cloudflare Workers)

The frontend is deployed to Cloudflare Workers Static Assets (`./out`) using Wrangler:

```bash
# 1. Compile Next.js SPA static export into frontend/out/
npm run build

# 2. Deploy static assets to Cloudflare Workers
npm run deploy

# 3. Stream real-time Worker logs
npm run tail
```

## Project Structure
- `src/app/` — Next.js App Router pages (`/login`, `/runs`, `/eval`, `/repos`, `/mock`, `/config`)
- `src/components/` — Modular UI components (trace timelines, run filters, status badges, layout topbar/sidebar)
- `src/lib/` — API client ([`api.ts`](file:///C:/Users/bari2/Desktop/Haunter/frontend/src/lib/api.ts)), authentication context, and helper utilities
- `wrangler.jsonc` — Cloudflare Workers Static Assets configuration

