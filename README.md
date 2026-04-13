# FinOps Intelligence Platform

**Built by [DevOps ARG](https://www.devopsarg.com) · powered with Claude**

An AI-powered FinOps agent that analyzes AWS cloud costs and infrastructure using
conversational AI. Ask questions in natural language — the agent reasons across
Cost Explorer, infrastructure metrics, and AWS's native recommendation APIs (Cost
Optimization Hub, Compute Optimizer, Rightsizing, Savings Plans) to answer them.

> **Read-only by design.** The agent uses a dedicated IAM user with the AWS-managed
> `ReadOnlyAccess` policy. It can't create, modify, or delete anything in your
> account — it reads metrics and suggests changes that you apply yourself.

## What it answers

The sidebar ships with 27 high-value FinOps questions across 9 categories, all
drawn from real DevOps ARG case studies:

- **Quick insights** — biggest cost driver, projected spend, anomalies, cost by region
- **Networking & data transfer** — NAT Gateway cost, VPC endpoints, cross-AZ, inter-region
- **Compute optimization** — EC2 rightsizing, Spot coverage, Graviton migration, scale-to-zero
- **Commitments** — Savings Plans coverage, RI opportunities for RDS/ElastiCache
- **Storage & databases** — orphaned EBS, S3 lifecycle, RDS downsize, gp2→gp3
- **Observability** — CloudWatch Logs cost trend, cost by team/tag
- **Real-time workloads** — WebSocket connections, event-based pre-scaling
- **Predictive scaling** — baseline reduction, Spot risk, RDS connection forecasting
- **AI Ops** — MTTR→$ ROI, LLM cost optimization (Haiku vs Sonnet routing)

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
  Account: 620309325636
  ARN:     arn:aws:iam::620309325636:user/finops-agent-readonly
  UserId:  AIDAZA3KRE5CCMX2VXP63
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

`backend/reasoning/engine.py` runs a multi-round loop:

1. User query arrives
2. LLM sees SYSTEM_PROMPT + conversation history + 14 tool definitions
3. **Round 1**: LLM calls tools (typically `get_current_date` → `query_aws_costs`)
4. **Reflection**: engine injects "do you have enough data?"
5. **Rounds 2-4**: additional tool calls if needed
6. **Final synthesis**: structured markdown with real numbers

SSE events: `thinking`, `tool_call`, `tool_result`, `answer`, `done`, `error`.
The frontend renders them in the **Reasoning Trace** panel on the right of the
chat tab, in real time as they fire.

### How multi-region scan works

- **Cost Explorer (`/api/report`)** — calls `GetCostAndUsage` in `us-east-1`
  without a region filter, so you get **all-region totals** by default.
  Optionally groups by `REGION` for per-region breakdown.
- **Infrastructure (`/api/infrastructure`)** — by default scans the region set
  in `AWS_DEFAULT_REGION`. Pass `?region=all` to parallel-scan every enabled
  region (18+ on typical accounts). The UI exposes a dropdown to switch.

## The read-only setup script

`create-read-only.sh` is the safety moat. It:

1. Creates an IAM user `finops-agent-readonly` using your admin profile
2. Attaches the AWS-managed `ReadOnlyAccess` policy (covers `ce:*`, `ec2:Describe*`,
   `rds:Describe*`, all the `*List*` and `*Get*`)
3. Generates access keys and writes them to:
   - `.env` (for the backend container)
   - `~/.aws/credentials` as profile `finops` (for your shell)
4. Tests `sts get-caller-identity` to verify read access works
5. **Tests that `s3 mb` fails** — if the user can create a bucket, the script
   aborts with a security warning

Re-running the script rotates the access keys (deletes old, creates new).

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
