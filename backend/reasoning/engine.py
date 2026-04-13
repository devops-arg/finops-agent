import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Generator, List, Optional
from backend.llm.provider import LLMProvider
from backend.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_ROUNDS = 4
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

### Infrastructure Intelligence (NEW)
- `get_infrastructure_health` — Overall health status of all resources (EC2, RDS, EKS, ElastiCache, OpenSearch, S3)
- `list_ec2_instances` — EC2 instances with CPU/memory utilization and rightsizing flags
- `get_rds_status` — RDS instances with CPU, storage, connection counts, Multi-AZ status
- `get_eks_cluster_status` — EKS clusters, node pools, Spot/On-demand mix, pod counts
- `get_elasticache_status` — Redis/Memcached hit rates, memory usage, evictions
- `get_s3_usage` — S3 buckets, sizes, object counts, lifecycle policy status
- `get_optimization_recommendations` — Prioritized list of cost savings opportunities (P1/P2/P3)

## Decision Flow

Cost questions:
- "What's our total cost?" → search_knowledge_base first
- "Top services by cost" → search_knowledge_base first
- "Cost trend / weekly spend" → search_knowledge_base first
- "Cost for specific dates" → query_aws_costs (live)
- "Forecast next month" → get_cost_forecast (live)
- "Any anomalies?" → get_cost_anomalies (live)

Infrastructure questions:
- "RDS health / database status" → get_rds_status
- "EC2 rightsizing / which instances are over-provisioned" → list_ec2_instances
- "Kubernetes / EKS cluster status" → get_eks_cluster_status
- "Redis / cache hit rate" → get_elasticache_status
- "S3 storage usage" → get_s3_usage
- "Overall infra health" → get_infrastructure_health
- "What should I optimize?" → get_optimization_recommendations

## Rules
- ALWAYS use the data returned by tools — never invent numbers or claim errors when tools return data successfully
- Tools return real data. If a tool result contains cost figures, service breakdowns, or infrastructure metrics, present them confidently — do NOT say "I encountered technical issues" or "the API returned errors" when data was returned
- For cost questions, prefer search_knowledge_base before making live API calls
- Call get_current_date before any time-based Cost Explorer query
- When you find an anomaly or optimization, explain the business impact in dollars
- For infrastructure issues, connect them to cost implications when relevant

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

FINAL_SYNTHESIS_PROMPT = """Based on all the data collected from the tools, provide your FINAL answer.

RULES:
- Only reference actual numbers from tool results — never make up data
- Present a clear, structured analysis
- Include specific dollar amounts, percentages, and trends
- If comparing periods, show the delta and percentage change
- End with actionable recommendations if relevant
- If any tools returned errors, mention what data was unavailable
"""


class ReasoningEngine:
    def __init__(self, llm_provider: LLMProvider, tool_registry: ToolRegistry):
        self._llm = llm_provider
        self._tools = tool_registry

    def process_query_stream(
        self,
        query: str,
        conversation_history: Optional[List[Dict[str, str]]] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        try:
            yield self._event("thinking", {"status": "Building context..."})

            messages = self._build_messages(query, conversation_history)
            available_tools = self._tools.get_all_definitions()

            yield self._event("thinking", {"status": f"Reasoning with {len(available_tools)} tools available"})

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

                if not response.tool_calls:
                    if response.content:
                        if self._looks_like_plan(response.content) and round_num == 1:
                            messages.append({"role": "assistant", "content": response.content})
                            messages.append({
                                "role": "user",
                                "content": (
                                    "Good plan. Now EXECUTE it — call the tools to get real data. "
                                    "Do not just describe what you would do; actually do it."
                                ),
                            })
                            continue
                        yield self._event("answer", {"content": response.content})
                        yield self._event("done", {"rounds": round_num})
                        return
                    continue

                for tool_call in response.tool_calls:
                    params = self._normalize_params(tool_call.parameters)

                    yield self._event("tool_call", {
                        "name": tool_call.tool_name,
                        "parameters": params,
                        "round": round_num,
                    })

                    result = self._tools.execute(tool_call.tool_name, params)

                    yield self._event("tool_result", {
                        "name": tool_call.tool_name,
                        "success": result.success,
                        "execution_time": result.execution_time,
                        "data_preview": self._truncate(json.dumps(result.data, default=str), 500) if result.data else None,
                        "error": result.error,
                    })

                    messages.append({
                        "role": "assistant",
                        "content": f"[Called {tool_call.tool_name} with {json.dumps(params, default=str)}]",
                    })

                    result_str = self._format_tool_result(result)
                    messages.append({
                        "role": "user",
                        "content": f"Result of {tool_call.tool_name}:\n{self._truncate(result_str, TOOL_RESULT_LIMIT)}",
                    })

                if round_num < MAX_ROUNDS:
                    messages.append({"role": "user", "content": REFLECTION_PROMPT})

            yield self._event("thinking", {"status": "Final synthesis..."})
            messages.append({"role": "user", "content": FINAL_SYNTHESIS_PROMPT})

            try:
                final = self._llm.chat_completion(messages=messages, tools=None, temperature=0.0)
                content = final.content or "I was unable to generate a complete analysis. Please try rephrasing your question."
                yield self._event("answer", {"content": content})
            except Exception as e:
                yield self._event("error", {"message": f"Final synthesis error: {e}"})
                return

            yield self._event("done", {"rounds": MAX_ROUNDS})

        except Exception as e:
            logger.error(f"Reasoning error: {e}", exc_info=True)
            yield self._event("error", {"message": str(e)})

    def _build_messages(
        self,
        query: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> List[Dict[str, str]]:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
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

    def _normalize_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
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

    def _event(self, event_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": event_type,
            "data": data,
            "timestamp": datetime.utcnow().isoformat(),
        }
