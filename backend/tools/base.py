from abc import ABC, abstractmethod
from typing import Any, Dict, List
from backend.models.core import ToolResult


class BaseTool(ABC):
    """Base class for all FinOps tools."""

    @abstractmethod
    def get_definitions(self) -> List[Dict[str, Any]]:
        """Return list of tool definitions (name, description, parameters)."""
        pass

    @abstractmethod
    def execute(self, tool_name: str, parameters: Dict[str, Any]) -> ToolResult:
        """Execute a tool by name with given parameters."""
        pass

    @abstractmethod
    def get_tool_names(self) -> List[str]:
        """Return list of tool names this class handles."""
        pass
