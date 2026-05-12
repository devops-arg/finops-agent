from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class Query:
    content: str
    session_id: Optional[str] = None
    id: str = ""
    timestamp: str = ""

    def __post_init__(self):
        if not self.id:
            import uuid

            self.id = str(uuid.uuid4())
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat()


@dataclass
class ToolResult:
    tool_name: str
    operation: str
    success: bool
    data: Any = None
    error: Optional[str] = None
    execution_time: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
