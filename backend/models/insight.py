"""
Insight model — pre-computed AWS billing/cost checks (no LLM needed).
Each check runs against live AWS APIs and returns a structured result.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid

STATUS_OK       = "ok"        # within normal range
STATUS_INFO     = "info"      # informational, no action required
STATUS_WARNING  = "warning"   # attention needed, potential savings
STATUS_CRITICAL = "critical"  # immediate action recommended

CATEGORY_COST          = "cost"
CATEGORY_COMPUTE       = "compute"
CATEGORY_NETWORKING    = "networking"
CATEGORY_STORAGE       = "storage"
CATEGORY_COMMITMENTS   = "commitments"
CATEGORY_OBSERVABILITY = "observability"


@dataclass
class Insight:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    category: str = CATEGORY_COST
    title: str = ""
    value: str = ""                  # human-readable value, e.g. "$4,231/mo" or "67%"
    status: str = STATUS_INFO
    detail: str = ""                 # 1-2 sentence explanation
    recommendation: str = ""         # what to do
    savings_usd: float = 0.0         # estimated monthly savings
    affected_count: int = 0          # number of affected resources
    region: str = "global"
    detected_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    context: Dict[str, Any] = field(default_factory=dict)   # raw data for AI

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "title": self.title,
            "value": self.value,
            "status": self.status,
            "detail": self.detail,
            "recommendation": self.recommendation,
            "savings_usd": round(self.savings_usd, 2),
            "affected_count": self.affected_count,
            "region": self.region,
            "detected_at": self.detected_at,
            "context": self.context,
        }
