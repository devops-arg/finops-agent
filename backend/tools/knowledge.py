import logging
import time
from typing import Any

from backend.knowledge.store import KnowledgeStore
from backend.models.core import ToolResult
from backend.tools.base import BaseTool

logger = logging.getLogger(__name__)

TOOL_DEFINITIONS = [
    {
        "name": "search_knowledge_base",
        "description": (
            "Search the pre-indexed knowledge base for cost reports, AWS account info, "
            "service lists, historical trends, and any custom context. "
            "Use this FIRST before making live API calls — it returns cached data instantly. "
            "Good for: 'what services do we use', 'what accounts do we have', "
            "'what was last week cost', 'show the cost trend', 'environment breakdown'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query about costs, accounts, services, or trends.",
                },
                "doc_type": {
                    "type": "string",
                    "description": (
                        "Optional filter by document type: cost_summary, cost_trend, "
                        "cost_by_service, cost_by_account, cost_by_environment, "
                        "aws_account, aws_services, aws_regions, custom."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return. Default 8.",
                },
            },
            "required": ["query"],
        },
    },
]


class KnowledgeTools(BaseTool):
    def __init__(self, store: KnowledgeStore):
        self._store = store

    def get_definitions(self) -> list[dict[str, Any]]:
        if self._store.document_count == 0:
            return []
        return TOOL_DEFINITIONS

    def get_tool_names(self) -> list[str]:
        if self._store.document_count == 0:
            return []
        return [t["name"] for t in TOOL_DEFINITIONS]

    def execute(self, tool_name: str, parameters: dict[str, Any]) -> ToolResult:
        start = time.time()
        query = parameters.get("query", "")
        doc_type = parameters.get("doc_type")
        limit = parameters.get("limit", 8)

        results = self._store.search(query, limit=limit, doc_type=doc_type)

        if not results:
            return ToolResult(
                tool_name=tool_name,
                operation="search",
                success=True,
                data={"results": [], "message": "No matching documents found in knowledge base."},
                execution_time=round(time.time() - start, 4),
            )

        formatted = []
        for r in results:
            formatted.append(
                {
                    "content": r["content"],
                    "type": r["type"],
                    "score": r["score"],
                }
            )

        return ToolResult(
            tool_name=tool_name,
            operation="search",
            success=True,
            data={"results": formatted, "total_in_kb": self._store.document_count},
            execution_time=round(time.time() - start, 4),
        )
