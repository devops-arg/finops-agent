# FinOps Intelligence Platform

**Built by [DevOps ARG](https://www.devopsarg.com) · powered with Claude**

<p align="center">
  <img src="docs/screenshots/logo.png" alt="DevOps ARG" width="140" />
</p>

An AI-powered FinOps agent that analyzes AWS cloud costs and infrastructure using
conversational AI. Ask questions in natural language — the agent reasons across
Cost Explorer, infrastructure metrics, and AWS's native recommendation APIs (Cost
Optimization Hub, Compute Optimizer, Rightsizing, Savings Plans) to answer them.

> **Read-only by design.** The agent uses a dedicated IAM user with the AWS-managed
> `ReadOnlyAccess` policy. It can't create, modify, or delete anything in your
> account — it reads metrics and suggests changes that you apply yourself.

---

## 🖼 Screenshots

### Conversational chat with live reasoning trace
The main entry point. Users ask questions in plain English; the right-hand panel streams the reasoning — every tool call, every intermediate result, and the final synthesis.

<p align="center">
  <img src="docs/screenshots/chat.png" alt="Chat interface with reasoning trace" width="900" />
</p>

### Weekly cost report dashboard
Auto-generated from Cost Explorer — breakdown by service, account, region, environment, and team tags. Pulls last 4 weeks by default, configurable via `REPORT_WEEKS`.

<p align="center">
  <img src="docs/screenshots/dashboard-report.png" alt="Weekly cost report" width="900" />
</p>

### Live infrastructure view
EC2 / RDS / EKS / ElastiCache / S3 health. Single-region by default; `region=all` fans out to every enabled region in parallel (~20s round-trip on an 18-region account).

<p align="center">
  <img src="docs/screenshots/dashboard-infra.png" alt="Infrastructure health view" width="900" />
</p>

### Optimization recommendations
Real savings numbers pulled from AWS **Cost Optimization Hub** — rightsizing, Savings Plans, Compute Optimizer, idle detection. The agent explains each recommendation in context during chat.

<p align="center">
  <img src="docs/screenshots/dashboard-optimize.png" alt="Optimization recommendations" width="900" />
</p>

---

## 📖 Table of contents

- [What it answers](#what-it-answers) — the 27 preset questions and what tools they trigger
- [Architecture](#architecture) — services, data flow, diagram
- [Quick start](#quick-start) — mock mode + live AWS mode
- [Feature flags](#feature-flags-env) — all `.env` variables
- [Endpoints](#endpoints) — HTTP API reference
- [The reasoning engine](#the-reasoning-engine) — multi-round loop, reflection, SSE events
- [The read-only setup script](#the-read-only-setup-script) — IAM provisioning + write-block verification
- [Project structure](#project-structure)
- [Security posture](#security-posture)
- [Want help running it in production?](#want-help-running-it-in-production)

## What it answers

The sidebar ships with **27 high-value FinOps questions across 9 categories**, all drawn from real DevOps ARG case studies. Pick one with a click, or ask your own in free-form text.

| Category | Example question | What it does under the hood |
|----------|-------------------|-----------------------------|
| ⚡ **Quick insights** | *"What's driving my AWS bill this month?"* | `get_current_date` → `query_aws_costs` grouped by service → ranks top 10 by $ |
| 🌐 **Networking & data transfer** | *"How much am I spending on NAT Gateway?"* | Cost Explorer filter on `AWS Data Transfer` + NAT usage type → summarizes by AZ |
| 🖥 **Compute optimization** | *"Which EC2 instances are oversized?"* | Queries `get_rightsizing_recommendations` + `get_compute_optimizer_recommendations` → annotates with monthly savings |
| 💸 **Commitments** | *"What's my Savings Plans coverage?"* | `get_savings_plans_coverage` → compares covered vs on-demand, flags gaps |
| 💾 **Storage & databases** | *"Do I have orphaned EBS volumes?"* | `list_ebs_volumes` → filters unattached/available → sums monthly $ at gp2/gp3 rates |
| 📊 **Observability** | *"How is my CloudWatch Logs cost trending?"* | Cost Explorer filter on `AWS CloudWatch` service + Logs usage type, 4-week series |
| 🔄 **Real-time workloads** | *"How many WebSocket connections am I running?"* | `describe_load_balancers` + CloudWatch active connections metric |
| 📈 **Predictive scaling** | *"What's my safe baseline with Spot?"* | `get_spot_instance_price_history` + EC2 inventory → spot interruption risk per family |
| 🤖 **AI Ops** | *"What's the ROI of reducing MTTR?"* | Knowledge-base lookup for past incident ARR impact + recent cost burst patterns |

Every preset question in the sidebar has a hover tooltip explaining which tools it triggers — a nice teaching moment for anyone new to FinOps.

<p align="center">
  <img src="docs/screenshots/sidebar-questions.png" alt="Sidebar with the 27 preset questions" width="320" />
</p>

## Architecture

```
docker compose up --build    (3 services)

┌─────────────────┐     ┌────────────────────────────┐     ┌──────────────────┐
│  Frontend       │────▶│  FastAPI Backend            │────▶│  AWS APIs        │
│  nginx (:3000)  │◀────│  (:8000)                    │◀────│  (read-only)     │
│                 │ SSE │                             │     │                  │
│  Chat + Dash    │     │  Reasoning Engine (4 rounds)│     │  Cost Explorer   │
│  Infrastructure │     │  14 tool definitions        │     │  EC2/RDS/EKS/... │
│  Optimizer      │     │  Claude Sonnet 4            │     │  Cost Opt Hub    │
└─────────────────┘     └────────────────────────────┘     └──────────────────┘
```

**Data flow for the chat:** user message → reasoning engine picks tools → boto3
calls AWS (or returns mock) → LLM synthesizes answer → streamed back over SSE so
the trace panel shows reasoning in real time.

## Quick start

### 1. Mock mode (no AWS account needed)

Great for demos, screencasts, and playing with the UI.

```bash
cp .env.example .env
# Edit .env: ANTHROPIC_API_KEY=sk-ant-...
# Set USE_MOCK_DATA=true
docker compose up --build
# Open http://localhost:3000
```

The mock data is a **fictional Series B LatAm fintech called "Ribbon"** with
~$28K/mo AWS spend across 3 regions. All dates are relative to today, so the
demo never looks stale.

### 2. Live AWS mode (read-only)

For running against your real AWS account with safety guarantees.

```bash
# Step 1: create a dedicated read-only IAM user
./create-read-only.sh <your-admin-profile>

# This creates an IAM user named `finops-agent-readonly` with ReadOnlyAccess,
# generates keys, writes them to .env + ~/.aws/credentials as profile "finops",
# and verifies write attempts are blocked (tries s3 mb, expects 403).

# Step 2: start the stack
docker compose up --build
# Open http://localhost:3000
```

On startup the backend prints an identity check so you know which ARN is
being used:

```
============================================================
AWS IDENTITY CHECK
  Account: <your-aws-account-id>
  ARN:     arn:aws:iam::<your-aws-account-id>:user/finops-agent-readonly
  UserId:  <IAM-user-unique-id>
============================================================
```

If the ARN doesn't contain "readonly" you'll get a WARNING in the logs — the
identity check **fails the startup** if it can't validate live-mode credentials.

## Feature flags (`.env`)

| Variable | Default | What it does |
|----------|---------|--------------|
| `AI_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `ANTHROPIC_API_KEY` | — | Required for anthropic |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-20250514` | Model for reasoning |
| `USE_LOCALSTACK` | `false` | Force LocalStack demo (no real AWS) |
| `USE_MOCK_DATA` | `false` in live, `true` in localstack | Override: return mock data from `/api/report`, `/api/infrastructure`, `/api/optimize` even in live mode |
| `AWS_DEFAULT_REGION` | `us-east-1` | Default region for single-region infra scans |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | — | Read-only keys from `create-read-only.sh` |

When `USE_MOCK_DATA=true` the backend returns the fictional "Ribbon" data. The
dashboard header shows a yellow **MOCK DATA** badge so users can't mistake it for
real numbers.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/health` | Status + mode + USE_MOCK_DATA flag |
| `POST` | `/api/chat/stream` | SSE chat with live reasoning trace |
| `POST` | `/api/chat` | Non-streaming chat (returns full response) |
| `GET`  | `/api/report` | Weekly cost report (by service/account/region/env/team) |
| `POST` | `/api/report/refresh` | Regenerate from live data |
| `GET`  | `/api/infrastructure?region=<name>` | EC2/RDS/EKS/... health. `region=all` scans every enabled region in parallel (~20s). Omitted → uses `AWS_DEFAULT_REGION`. |
| `GET`  | `/api/optimize` | Recommendations from AWS Cost Optimization Hub |

## The reasoning engine

`backend/reasoning/engine.py` runs a **multi-round agentic loop** — not a single prompt with a canned answer. This is what lets the agent adapt when the first query doesn't return enough data, or when a user asks a layered question (e.g. "compare this month vs last month and tell me what changed").

### Flow

1. **User query arrives** over `/api/chat/stream`.
2. LLM (Claude Sonnet 4 by default) sees:
   - `SYSTEM_PROMPT` — role, constraints, conversational tone
   - Full conversation history (for follow-ups)
   - **14 tool definitions** — each with JSON schema and usage hints
3. **Round 1** — the LLM calls one or more tools. Typical trajectory: `get_current_date` → `query_aws_costs` → maybe `get_cost_forecast`.
4. **Reflection step** — the engine injects a short system message asking *"do you have enough data to answer? If not, what would you call next?"*. This is the hook that catches incomplete reasoning.
5. **Rounds 2-4** — up to 3 additional tool-call rounds if the LLM decides it needs more. Hard cap at 4 total rounds to control cost.
6. **Final synthesis** — structured markdown with **real numbers**, formatted tables, and next-step recommendations. If a tool returned no data (empty account, no RIs, etc.), the agent states that explicitly rather than hallucinating.

<p align="center">
  <img src="docs/screenshots/reasoning-trace.png" alt="Reasoning trace panel showing round-by-round tool calls" width="420" />
</p>

### SSE event types (live streaming)

| Event | When fired | Payload |
|-------|------------|---------|
| `thinking` | LLM generates a `<thinking>` block | `{text}` |
| `tool_call` | LLM decides to call a tool | `{name, args}` |
| `tool_result` | Backend returns tool output | `{name, result}` |
| `answer` | LLM produces final markdown | `{text}` (streamed token-by-token) |
| `done` | Conversation turn ends | `{rounds, total_tokens}` |
| `error` | Any failure | `{message, retriable}` |

The frontend renders these in the **Reasoning Trace** panel on the right of the chat tab, colored by type, in real time. No spinner theater — if the agent is on round 3 of 4, the user sees exactly which tool is running.

### Why this matters

Most "chat with your data" demos use a single tool call and hope for the best. FinOps questions often need cross-referencing: *cost by service* then *instances in that service* then *rightsizing recs for those instances*. A multi-round loop with reflection lets the agent plan, execute, check, and re-plan — which is why the answers cite specific instance IDs and real dollar figures instead of vague strategies.

### How multi-region scan works

- **Cost Explorer (`/api/report`)** — calls `GetCostAndUsage` in `us-east-1`
  without a region filter, so you get **all-region totals** by default.
  Optionally groups by `REGION` for per-region breakdown.
- **Infrastructure (`/api/infrastructure`)** — by default scans the region set
  in `AWS_DEFAULT_REGION`. Pass `?region=all` to parallel-scan every enabled
  region (18+ on typical accounts). The UI exposes a dropdown to switch.

## The read-only setup script

`create-read-only.sh` is the **safety moat**. You run it once with an admin AWS profile; it provisions everything the agent needs and proves the agent can't write:

1. **Creates IAM user** `finops-agent-readonly` using your admin profile
2. **Attaches** the AWS-managed `ReadOnlyAccess` policy (covers `ce:*`, `ec2:Describe*`, `rds:Describe*`, all the `*List*` / `*Get*` — plus `coh:*` for Cost Optimization Hub)
3. **Generates access keys** and writes them to:
   - `.env` (for the backend container — `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
   - `~/.aws/credentials` as profile `finops` (for your shell / debugging)
4. **Verifies read works** — runs `aws sts get-caller-identity` using the new keys
5. **Verifies writes are BLOCKED** — runs `aws s3 mb s3://devopsarg-finops-verify-readonly` with the new keys; expects HTTP 403 Forbidden. If the bucket gets created, the script **aborts with a security warning** and rolls back.
6. **Prints the ARN** for audit (redacted in public output — see `AWS IDENTITY CHECK` block below)

Re-running the script **rotates the access keys** — deletes old, creates new, rewrites `.env` and `~/.aws/credentials`. Safe to run on a schedule.

<p align="center">
  <img src="docs/screenshots/readonly-setup.png" alt="Output of create-read-only.sh showing the 403 verification step" width="780" />
</p>

### Recommended rotation cadence

- Dev machines: every 30 days
- Shared demo boxes: every 7 days
- CI runners: per-run (the script takes ~15 seconds end to end)

## Project structure

```
finops-agent/
├── backend/
│   ├── config/manager.py          — env-based config + feature flags
│   ├── llm/                       — Anthropic / OpenAI providers
│   ├── models/                    — Pydantic models
│   ├── tools/
│   │   ├── aws_costs.py           — 8 Cost Explorer tools
│   │   ├── aws_resources.py       — 7 infra inspection tools
│   │   ├── live_resources.py      — multi-region live AWS queries
│   │   ├── mock_data.py           — "Ribbon" fictional fintech data
│   │   ├── knowledge.py           — KB search tool
│   │   └── registry.py            — tool registry
│   ├── reasoning/engine.py        — 4-round loop with reflection
│   ├── reports/generator.py       — cost report builder
│   └── server/main.py             — FastAPI app, SSE streaming
├── frontend/
│   ├── index.html                 — single-page UI with 27 preset questions
│   └── devopsarg-logo.png         — brand asset
├── scripts/
│   ├── seed_localstack.py         — LocalStack AWS resource seed
│   ├── setup.py                   — knowledge base setup
│   └── test_connection.py         — AWS + LLM connectivity test
├── create-read-only.sh            — IAM read-only user provisioning
├── docker-compose.yml             — 3 services (localstack, backend, frontend)
├── nginx.conf                     — reverse proxy with SSE-safe buffering
└── .env.example                   — all config documented
```

## Security posture

- **Read-only IAM** by construction (managed policy, not custom).
- **No write paths** in any tool — grep the `backend/tools/` directory for
  `create_*`, `delete_*`, `put_*`, `modify_*` etc. — there aren't any.
- **Identity check on startup** logs the ARN and fails-fast if invalid.
- **Cost Optimization Hub** recommendations are read-only queries; implementation
  is always human-in-the-loop (the agent suggests, you execute).

## Want help running it in production?

DevOps ARG builds this kind of tooling for Series A/B fintechs and scale-ups.
If you want:

- A custom FinOps agent for your stack
- A managed deployment with auth + multi-tenant
- Actual execution on the recommendations (rightsizing, Savings Plans, Karpenter migrations)

**[Book a call at devopsarg.com](https://www.devopsarg.com/en/#contact)** or read our case studies:

- [$237K/year AWS savings](https://www.devopsarg.com/en/blog/aws-cost-optimization-case-study/) — concrete breakdown, real customer
- [Karpenter + Spot + scale-to-zero](https://www.devopsarg.com/en/blog/karpenter-spot-scale-to-zero/) — $392K/year
- [FinOps dashboard with Grafana + Prometheus](https://www.devopsarg.com/en/blog/finops-dashboard-grafana-prometheus/) — $8K/mo waste found day 1

## License

MIT

---

*Built in Buenos Aires · [devopsarg.com](https://www.devopsarg.com)*
