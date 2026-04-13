"""
In-memory knowledge store with JSON persistence.
Zero external dependencies — designed for single-user FinOps agent.

Indexes cost reports, AWS metadata, and pre-fetched data so the agent
can answer common questions instantly without calling AWS APIs every time.
"""
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

INDEX_FILE = Path("knowledge_index.json")


class KnowledgeStore:
    def __init__(self, persist_path: Optional[str] = None):
        self._path = Path(persist_path) if persist_path else INDEX_FILE
        self._documents: List[Dict[str, Any]] = []
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                with open(self._path) as f:
                    data = json.load(f)
                self._documents = data.get("documents", [])
                logger.info(f"Knowledge store loaded: {len(self._documents)} documents from {self._path}")
            except Exception as e:
                logger.warning(f"Failed to load knowledge store: {e}")
                self._documents = []

    def _save(self):
        try:
            with open(self._path, "w") as f:
                json.dump({
                    "updated": datetime.utcnow().isoformat(),
                    "count": len(self._documents),
                    "documents": self._documents,
                }, f, indent=2, default=str)
            logger.info(f"Knowledge store saved: {len(self._documents)} documents")
        except Exception as e:
            logger.error(f"Failed to save knowledge store: {e}")

    def clear(self):
        self._documents = []
        self._save()

    @property
    def document_count(self) -> int:
        return len(self._documents)

    def add(self, content: str, doc_type: str, metadata: Optional[Dict] = None):
        self._documents.append({
            "content": content,
            "type": doc_type,
            "metadata": metadata or {},
            "indexed_at": datetime.utcnow().isoformat(),
        })

    def save(self):
        self._save()

    def search(self, query: str, limit: int = 10, doc_type: Optional[str] = None) -> List[Dict[str, Any]]:
        query_lower = query.lower()
        keywords = set(re.findall(r'\w+', query_lower))
        keywords.discard("")

        scored = []
        for doc in self._documents:
            if doc_type and doc.get("type") != doc_type:
                continue

            content_lower = doc["content"].lower()
            meta_str = json.dumps(doc.get("metadata", {})).lower()

            score = 0
            for kw in keywords:
                if kw in content_lower:
                    score += content_lower.count(kw)
                if kw in meta_str:
                    score += 1

            if score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {
                "content": doc["content"],
                "type": doc["type"],
                "metadata": doc.get("metadata", {}),
                "score": score,
            }
            for score, doc in scored[:limit]
        ]

    # ── Ingest helpers ──────────────────────────────────────

    def ingest_cost_report(self, report: Dict[str, Any]) -> int:
        """Index a weekly cost report into searchable documents."""
        count = 0
        summary = report.get("summary", {})
        weeks = report.get("weeks", [])

        if summary:
            text = (
                f"AWS Cost Report Summary (generated {report.get('generated', 'unknown')}):\n"
                f"- Last week cost: ${summary.get('lastWeekCost', 0):,.2f}\n"
                f"- Previous week cost: ${summary.get('previousWeekCost', 0):,.2f}\n"
                f"- Week-over-week change: {summary.get('weeklyChange', 0)}%\n"
                f"- 4-week total: ${summary.get('fourWeekTotal', 0):,.2f}\n"
                f"- 4-week average: ${summary.get('fourWeekAvg', 0):,.2f}\n"
                f"- Monthly projection: ${summary.get('monthlyProjection', 0):,.2f}\n"
                f"- Active accounts: {summary.get('activeAccounts', 0)}\n"
                f"- Top account: {summary.get('topAccount', 'N/A')}\n"
                f"- Top service: {summary.get('topService', 'N/A')}"
            )
            self.add(text, "cost_summary", {"source": "report", "weeks": weeks})
            count += 1

        trend = report.get("weeklyTrend", [])
        if trend:
            lines = ["Weekly cost trend:"]
            for w in trend:
                lines.append(f"  {w['week']}: ${w['cost']:,.2f}")
            self.add("\n".join(lines), "cost_trend", {"source": "report"})
            count += 1

        for item in report.get("byService", []):
            name = item.get("name", "Unknown")
            costs = item.get("costs", {})
            if not costs:
                continue
            last_cost = list(costs.values())[-1] if costs else 0
            if last_cost < 1.0:
                continue
            lines = [f"AWS Service: {name}"]
            for week, cost in costs.items():
                lines.append(f"  {week}: ${cost:,.2f}")
            lines.append(f"  Last week: ${last_cost:,.2f}")
            self.add("\n".join(lines), "cost_by_service", {"service": name, "last_cost": last_cost})
            count += 1

        for item in report.get("byAccount", []):
            acct_id = item.get("id", "Unknown")
            name = item.get("name", acct_id)
            costs = item.get("costs", {})
            if not costs:
                continue
            last_cost = list(costs.values())[-1] if costs else 0
            if last_cost < 1.0:
                continue
            lines = [f"AWS Account: {name} ({acct_id})"]
            for week, cost in costs.items():
                lines.append(f"  {week}: ${cost:,.2f}")
            self.add("\n".join(lines), "cost_by_account", {"account_id": acct_id, "name": name, "last_cost": last_cost})
            count += 1

        for item in report.get("byEnvironment", []):
            env = item.get("name", "Unknown")
            costs = item.get("costs", {})
            if not costs:
                continue
            last_cost = list(costs.values())[-1] if costs else 0
            if last_cost < 0.5:
                continue
            lines = [f"Environment: {env}"]
            for week, cost in costs.items():
                lines.append(f"  {week}: ${cost:,.2f}")
            self.add("\n".join(lines), "cost_by_environment", {"environment": env, "last_cost": last_cost})
            count += 1

        return count

    def ingest_account_metadata(self, accounts: List[Dict[str, Any]]) -> int:
        """Index AWS account dimension values."""
        count = 0
        for acct in accounts:
            val = acct.get("value", "")
            attrs = acct.get("attributes", {})
            name = attrs.get("description", val)
            text = f"AWS Account: {name} (ID: {val})"
            if attrs:
                for k, v in attrs.items():
                    text += f"\n  {k}: {v}"
            self.add(text, "aws_account", {"account_id": val, "name": name})
            count += 1
        return count

    def ingest_service_list(self, services: List[Dict[str, Any]]) -> int:
        """Index available AWS services."""
        names = [s.get("value", "") for s in services if s.get("value")]
        if names:
            text = f"Active AWS services ({len(names)}):\n" + "\n".join(f"  - {n}" for n in sorted(names))
            self.add(text, "aws_services", {"count": len(names)})
            return 1
        return 0

    def ingest_region_list(self, regions: List[Dict[str, Any]]) -> int:
        """Index AWS regions in use."""
        names = [r.get("value", "") for r in regions if r.get("value")]
        if names:
            text = f"AWS regions in use ({len(names)}):\n" + "\n".join(f"  - {n}" for n in sorted(names))
            self.add(text, "aws_regions", {"count": len(names)})
            return 1
        return 0

    def ingest_custom_context(self, title: str, content: str, doc_type: str = "custom") -> int:
        """Index any custom context (org-specific notes, tagging conventions, etc)."""
        self.add(f"{title}\n{content}", doc_type, {"title": title})
        return 1
