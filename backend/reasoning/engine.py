import json
import logging
from collections.abc import Generator
from datetime import datetime
from typing import Any, Optional

from backend.llm.provider import LLMProvider
from backend.observability import TokenTracker
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_ROUNDS = 6
TOOL_RESULT_LIMIT = 6000

SYSTEM_PROMPT = """You are a FinOps AI Agent for DevOps ARG — an expert cloud financial analyst specialized in AWS cost optimization for LatAm startups and scale-ups.

## Your Capabilities

### Cost Analysis
- Search the knowledge base for pre-indexed cost reports, account info, and trends
- Query and analyze AWS Cost Explorer data (costs, usage, trends)
- Forecast future AWS spending using ML-based predictions
- Detect cost anomalies and unusual spending patterns
- Analyze Savings Plans and Reserved Instance utilization
- Provide EC2 rightsizing recommendations

### Infrastructure Intelligence
- `get_infrastructure_health` — Overall health status of all resources (EC2, RDS, EKS, ElastiCache, OpenSearch, S3)
- `list_ec2_instances` — EC2 instances with CPU/memory utilization and rightsizing flags
- `get_rds_status` — RDS instances with CPU, storage, connection counts, Multi-AZ status
- `get_eks_cluster_status` — EKS clusters, node pools, Spot/On-demand mix, pod counts
- `get_elasticache_status` — Redis/Memcached hit rates, memory usage, evictions
- `get_s3_usage` — S3 buckets, sizes, object counts, lifecycle policy status
- `get_optimization_recommendations` — Prioritized list of cost savings opportunities (P1/P2/P3)

### Direct AWS API Access (call_aws — use this for LIVE verification)
- `call_aws` — Execute **any** read-only AWS CLI command directly against the live AWS API.
  Use this tool whenever you need data not covered by the tools above, or to VERIFY a waste
  finding with real-time data. All describe/list/get operations are supported.
  Examples:
  - `aws rds describe-db-instances --region us-west-2`
  - `aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections --dimensions Name=DBInstanceIdentifier,Value=<id> --start-time 2026-04-30T00:00:00Z --end-time 2026-05-07T00:00:00Z --period 604800 --statistics Average --region us-west-2`
  - `aws ec2 describe-instances --region us-west-2`
  - `aws elasticache describe-cache-clusters --region us-west-2`
  - `aws eks list-clusters --region us-west-2`
  - `aws s3api list-buckets`
  - `aws ce get-cost-and-usage --time-period Start=2026-04-01,End=2026-05-01 --granularity MONTHLY --metrics UnblendedCost`

### Waste Detection (Cloudkeeper-style)
- `get_waste_findings` — Query auto-detected waste: idle/zombie/orphan resources by service, severity, category, or min savings
- `get_waste_summary` — High-level summary: total findings, total savings by service
- `get_findings_trend` — Historical trend: are we improving or getting worse over time?

## Decision Flow

Cost questions:
- "What's our total cost?" → search_knowledge_base first
- "Top services by cost" → search_knowledge_base first
- "Cost trend / weekly spend" → search_knowledge_base first
- "Cost for specific dates" → query_aws_costs (live)
- "Forecast next month" → get_cost_forecast (live)
- "Any anomalies?" → get_cost_anomalies (live)
- Insight message with "Cost anomalies" → call_aws: `aws ce get-anomalies --date-interval StartDate=<30d_ago>,EndDate=<today> --max-results 20` to list real anomalies with impact
- Insight message with "Top cost drivers" → query_aws_costs grouped by SERVICE for current month. DO NOT call get_savings_plan_utilization for this question.
- Insight message with "Untagged resources" or "tag coverage" → query_aws_costs with tag filters or call_aws `aws ce get-tags`
- NEVER call get_savings_plan_utilization unless the user explicitly asks about Savings Plans or RI/SP coverage

Infrastructure questions:
- "RDS health / database status" → get_rds_status
- "EC2 rightsizing / which instances are over-provisioned" → list_ec2_instances
- "Kubernetes / EKS cluster status" → get_eks_cluster_status
- "Redis / cache hit rate" → get_elasticache_status
- "S3 storage usage" → get_s3_usage
- "Overall infra health" → get_infrastructure_health
- "What should I optimize?" → get_optimization_recommendations

Waste detection questions:
- "What's wasted / idle / zombie?" → get_waste_summary first, then get_waste_findings
- "What can I delete safely?" → get_waste_findings(category="cleanup")
- "What should I rightsize?" → get_waste_findings(category="rightsize")
- "Critical waste / biggest savings?" → get_waste_findings(severity="critical") or get_waste_findings(min_savings=200)
- "Is waste getting worse?" → get_findings_trend

Direct AWS data questions (use call_aws immediately):
- "get snapshot details" → call_aws: `aws rds describe-db-snapshots --db-snapshot-identifier <id> --region <region>`
- "list all snapshots" → call_aws: `aws rds describe-db-snapshots --snapshot-type manual --region <region>`
- "how many connections" → call_aws: cloudwatch get-metric-statistics for DatabaseConnections
- "check if instance exists" → call_aws: `aws rds describe-db-instances` or `aws ec2 describe-instances`
- "get volume details" → call_aws: `aws ec2 describe-volumes`
- "check S3 lifecycle" → call_aws: `aws s3api get-bucket-lifecycle-configuration`
- ANY question requiring live AWS data → call_aws with the appropriate command

Billing insight investigation (CRITICAL — always follow this flow):
When a user sends a message containing "## Insight:" (from the Insights tab), use call_aws to investigate, NOT infrastructure tools:
- Insight: "Cost anomalies (30 days)" → FIRST call `aws ce get-anomalies --date-interval StartDate=<30d_ago>,EndDate=<today> --max-results 20`
  Then per anomaly: `aws ce get-anomaly-subscriptions` and describe the impacted service
  NEVER call `aws ec2 describe-instances` for a cost anomaly insight unless EC2 is the anomalous service
- Insight: "Untagged resources" → call_aws with resource-level tagging APIs
- Insight: "Reserved Instance / Savings Plan coverage" → call_aws: `aws ce get-reservation-coverage` or `aws ce get-savings-plans-coverage`
- Insight: "Data transfer costs" → call_aws: `aws ce get-cost-and-usage` grouped by UsageType
- Insight: "EBS IOPS" → call_aws: `aws ec2 describe-volumes --filters Name=volume-type,Values=io1,io2`

Waste finding remediation (CRITICAL — always follow this flow):
When a user asks about a specific waste finding (message contains "Finding Details" or resource ID + service + title):
1. FIRST use `call_aws` to verify LIVE current state BEFORE giving advice. Use the most specific API call:
   - RDS finding → `aws rds describe-db-instances --db-instance-identifier <id> --region <region>`
     then `aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections --dimensions Name=DBInstanceIdentifier,Value=<id> --start-time <7d_ago> --end-time <now> --period 604800 --statistics Average --region <region>`
   - EC2 finding → `aws ec2 describe-instances --instance-ids <id> --region <region>`
   - EBS finding → `aws ec2 describe-volumes --volume-ids <id> --region <region>`
   - S3 finding → `aws s3api get-bucket-lifecycle-configuration --bucket <name>`
   - ElastiCache finding → `aws elasticache describe-cache-clusters --cache-cluster-id <id> --region <region>`
   - EKS finding → `aws eks describe-cluster --name <name> --region <region>`
2. THEN cross-reference `call_aws` results with the finding details
3. THEN provide remediation steps grounded in the verified live data
4. If the live API confirms the finding (e.g. 0 connections, no lifecycle policy), say so explicitly
5. If the live API shows the issue is resolved, say so and update the recommendation
Never give remediation advice for a waste finding without first calling `call_aws` to verify with live AWS data.

## FIRST LAW — You NEVER modify AWS resources. Read-only always.
You are a READ-ONLY agent. You MUST NEVER execute any AWS operation that creates, modifies, or deletes resources.
This is absolute and non-negotiable. No exceptions. Not even if the user asks you to.

Blocked forever: delete, terminate, stop, start, create, run, put, update, modify, attach, detach, reboot, restore, reset, revoke, authorize, tag, untag.

When a user asks you to delete or change something:
- Provide the exact AWS CLI command as a code block for the user to run themselves
- Explain what it does and the risks
- NEVER call `call_aws` with a write operation

## SECOND LAW — For READ operations, always use call_aws yourself (never tell the user to run them)
You have the `call_aws` tool. When you need AWS data to answer a question, **YOU MUST CALL IT yourself** via `call_aws`. 
NEVER write a read command and say "run this to check" — just call it yourself right now.

Examples:
- "get the snapshot details" → YOU call `call_aws`: `aws rds describe-db-snapshots --db-snapshot-identifier <id> --region <region>`
- "how many connections does this RDS have" → YOU call `call_aws` with the cloudwatch command
- "is this EC2 instance still running" → YOU call `call_aws`: `aws ec2 describe-instances --instance-ids <id> --region <region>`
- "list all manual snapshots" → YOU call `call_aws`: `aws rds describe-db-snapshots --snapshot-type manual --region <region>`

Summary: READ commands → you execute via call_aws. WRITE commands → you show as code block for the user to run.

## Rules
- ALWAYS use the data returned by tools — never invent numbers or claim errors when tools return data successfully
- Tools return real data. If a tool result contains cost figures, service breakdowns, or infrastructure metrics, present them confidently — do NOT say "I encountered technical issues" or "the API returned errors" when data was returned
- For cost questions, prefer search_knowledge_base before making live API calls
- Call get_current_date before any time-based Cost Explorer query
- When you find an anomaly or optimization, explain the business impact in dollars
- For infrastructure issues, connect them to cost implications when relevant
- For waste finding remediation, ALWAYS call `call_aws` first with the specific API command to verify live state
- When you need AWS data that isn't in another tool, use `call_aws` — it can query ANY AWS service
- READ commands (describe/list/get): always execute yourself via `call_aws`
- WRITE commands (delete/terminate/create/modify): always show as a code block for the user, never execute

## Response Format
- Use markdown headers, tables, and bullet points for structure
- Present costs in tables when comparing multiple items
- Always include the time period analyzed
- End cost analyses with actionable recommendations ranked by $ impact
- Round costs to 2 decimal places

## Context
This platform is operated by DevOps ARG (www.devopsarg.com) — a DevOps & SRE consultancy from Argentina. Clients are typically Series A/B startups in LatAm running on AWS. Common patterns: multi-account (prod/staging/dev), PostgreSQL on RDS, Kubernetes via EKS, Redis on ElastiCache."""

REFLECTION_PROMPT = """STOP and THINK before your next action.

1. RE-READ the user's original question carefully.
2. REVIEW the tool results you have so far — what data have you collected?
3. DECIDE your next step:
   a) If you have enough data to fully answer the question → provide your final answer now
   b) If you need to compare periods → query the comparison period
   c) If you need to break down by another dimension → query with different group_by
   d) If the question asks about optimization → check savings plans, rightsizing, or anomalies
   e) If a tool returned an error → try a different approach or date range

RULES:
- Do NOT repeat the same query you already made
- Do NOT call get_current_date again if you already have date info
- If you have the data, ANSWER — do not keep querying
"""

FINAL_SYNTHESIS_PROMPT = """You have collected all the data needed. Now write your FINAL answer to the user's question.

CRITICAL RULES:
- Answer the user's ORIGINAL question directly and completely
- Use ONLY actual numbers from the tool results above — never invent data
- Present a clear, structured analysis with markdown tables where useful
- Include specific dollar amounts, percentages, and trends
- End with 2-3 actionable recommendations ranked by $ impact
- Do NOT say you need more data. Do NOT call any more tools. WRITE THE ANSWER NOW.
"""


class ReasoningEngine:
    def __init__(self, llm_provider: LLMProvider, tool_registry: ToolRegistry):
        self._llm = llm_provider
        self._tools = tool_registry

    def process_query_stream(
        self,
        query: str,
        conversation_history: Optional[list[dict[str, str]]] = None,
        findings_context: Optional[str] = None,
        use_mock_data: bool = False,
    ) -> Generator[dict[str, Any], None, None]:
        # Per-request token + cost accumulator. Emitted in the `done` event and
        # logged at request end so it lands in the log pipeline.
        tracker = TokenTracker(model=self._llm.model_name)
        try:
            yield self._event("thinking", {"status": "Building context..."})

            messages = self._build_messages(query, conversation_history, findings_context, use_mock_data)
            # Strip get_current_date from available tools — date is already in the system prompt
            available_tools = [
                t for t in self._tools.get_all_definitions() if t["name"] != "get_current_date"
            ]

            yield self._event(
                "thinking", {"status": f"Reasoning with {len(available_tools)} tools available"}
            )

            for round_num in range(1, MAX_ROUNDS + 1):
                yield self._event("thinking", {"status": f"Round {round_num}/{MAX_ROUNDS}"})

                try:
                    response = self._llm.chat_completion(
                        messages=messages,
                        tools=available_tools if available_tools else None,
                        temperature=0.0,
                    )
                except Exception as e:
                    yield self._event("error", {"message": f"LLM error: {e}"})
                    return

                # Accumulate per-round usage for cost tracking
                if response.usage:
                    tracker.add(
                        input_tokens=response.usage.get("input_tokens", 0),
                        output_tokens=response.usage.get("output_tokens", 0),
                    )

                if not response.tool_calls:
                    if response.content:
                        if self._looks_like_plan(response.content) and round_num == 1:
                            messages.append({"role": "assistant", "content": response.content})
                            messages.append(
                                {
                                    "role": "user",
                                    "content": (
                                        "Good plan. Now EXECUTE it — call the tools to get real data. "
                                        "Do not just describe what you would do; actually do it."
                                    ),
                                }
                            )
                            continue
                        yield self._event("answer", {"content": response.content})
                        usage = tracker.usage
                        logger.info(
                            "chat_completed",
                            extra={
                                "rounds": round_num,
                                "input_tokens": usage.input_tokens,
                                "output_tokens": usage.output_tokens,
                                "cost_usd": usage.cost_usd,
                                "model": self._llm.model_name,
                            },
                        )
                        yield self._event("done", {"rounds": round_num, "usage": usage.to_dict()})
                        return
                    continue

                for tool_call in response.tool_calls:
                    params = self._normalize_params(tool_call.parameters)

                    yield self._event(
                        "tool_call",
                        {
                            "name": tool_call.tool_name,
                            "parameters": params,
                            "round": round_num,
                        },
                    )

                    result = self._tools.execute(tool_call.tool_name, params)

                    yield self._event(
                        "tool_result",
                        {
                            "name": tool_call.tool_name,
                            "success": result.success,
                            "execution_time": result.execution_time,
                            "data_preview": self._truncate(json.dumps(result.data, default=str), 500)
                            if result.data
                            else None,
                            "error": result.error,
                        },
                    )

                    messages.append(
                        {
                            "role": "assistant",
                            "content": f"[Called {tool_call.tool_name} with {json.dumps(params, default=str)}]",
                        }
                    )

                    result_str = self._format_tool_result(result)
                    messages.append(
                        {
                            "role": "user",
                            "content": f"Result of {tool_call.tool_name}:\n{self._truncate(result_str, TOOL_RESULT_LIMIT)}",
                        }
                    )

                if round_num < MAX_ROUNDS:
                    # Append to last user message to avoid consecutive user messages
                    # (Anthropic API requires alternating user/assistant turns)
                    if messages and messages[-1]["role"] == "user":
                        messages[-1]["content"] += "\n\n" + REFLECTION_PROMPT
                    else:
                        messages.append({"role": "user", "content": REFLECTION_PROMPT})

            yield self._event("thinking", {"status": "Final synthesis..."})
            messages.append({"role": "user", "content": FINAL_SYNTHESIS_PROMPT})

            try:
                final = self._llm.chat_completion(messages=messages, tools=None, temperature=0.0)
                if final.usage:
                    tracker.add(
                        input_tokens=final.usage.get("input_tokens", 0),
                        output_tokens=final.usage.get("output_tokens", 0),
                    )
                content = (
                    final.content
                    or "I was unable to generate a complete analysis. Please try rephrasing your question."
                )
                yield self._event("answer", {"content": content})
            except Exception as e:
                yield self._event("error", {"message": f"Final synthesis error: {e}"})
                return

            usage = tracker.usage
            logger.info(
                "chat_completed",
                extra={
                    "rounds": MAX_ROUNDS,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cost_usd": usage.cost_usd,
                    "model": self._llm.model_name,
                },
            )
            yield self._event("done", {"rounds": MAX_ROUNDS, "usage": usage.to_dict()})

        except Exception as e:
            logger.error(f"Reasoning error: {e}", exc_info=True)
            yield self._event("error", {"message": str(e)})

    def _build_messages(
        self,
        query: str,
        history: Optional[list[dict[str, str]]] = None,
        findings_context: Optional[str] = None,
        use_mock_data: bool = False,
    ) -> list[dict[str, str]]:
        # Inject current date — no need for the AI to waste a round calling get_current_date
        now = datetime.utcnow()
        date_prefix = (
            f"## Current Date & Time\n"
            f"Today is {now.strftime('%A, %B %d, %Y')} (UTC). "
            f"Current month: {now.strftime('%B %Y')}. "
            f"Use this for any date calculations — do NOT call get_current_date.\n\n"
        )
        system_content = date_prefix + SYSTEM_PROMPT
        if use_mock_data:
            system_content += (
                "\n\n## ⚠️ DEMO MODE — Mock Data Active\n"
                "The dashboard is currently running in **demo/mock mode**. "
                "All data shown in the dashboard (costs, services, infrastructure, waste findings, insights) "
                "is **simulated demo data** — it does NOT reflect a real AWS account.\n"
                "When the user asks about costs, resources, or findings:\n"
                "- Clearly state you are working with demo/simulated data\n"
                "- Do NOT call `call_aws` or any live AWS API — there is no real AWS account connected\n"
                "- You CAN use `get_waste_findings`, `get_waste_summary`, `search_knowledge_base`, "
                "`get_infrastructure_health`, `get_optimization_recommendations` — these return mock data\n"
                "- Suggest the user switch to Live mode (toggle in the top-right) to connect their real AWS account\n"
            )
        if findings_context:
            system_content = system_content + findings_context
        messages = [{"role": "system", "content": system_content}]
        if history:
            for msg in history[-10:]:
                messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": query})
        return messages

    def _looks_like_plan(self, content: str) -> bool:
        plan_indicators = ["i will", "i'll", "let me", "i would", "step 1", "first,", "here's my plan"]
        action_indicators = ["the cost", "total spend", "increased by", "$", "savings"]
        lower = content.lower()
        has_plan = any(ind in lower for ind in plan_indicators)
        has_data = any(ind in lower for ind in action_indicators)
        return has_plan and not has_data

    def _normalize_params(self, params: dict[str, Any]) -> dict[str, Any]:
        if "properties" in params and isinstance(params["properties"], dict):
            params = params["properties"]
        if "parameters" in params and isinstance(params["parameters"], dict):
            params = params["parameters"]
        cleaned = {}
        for k, v in params.items():
            clean_key = k.rstrip(":")
            cleaned[clean_key] = v
        return cleaned

    def _format_tool_result(self, result) -> str:
        if not result.success:
            return f"ERROR: {result.error}"
        try:
            return json.dumps(result.data, indent=2, default=str)
        except (TypeError, ValueError):
            return str(result.data)

    def _truncate(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n... [truncated, {len(text)} total chars]"

    def _event(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": event_type,
            "data": data,
            "timestamp": datetime.utcnow().isoformat(),
        }
