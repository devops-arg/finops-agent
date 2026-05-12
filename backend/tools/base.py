from abc import ABC, abstractmethod
from typing import Any

from backend.models.core import ToolResult


class BaseTool(ABC):
    """Base class for all FinOps tools."""

    @abstractmethod
    def get_definitions(self) -> list[dict[str, Any]]:
        """Return list of tool definitions (name, description, parameters)."""
        pass

    @abstractmethod
    def execute(self, tool_name: str, parameters: dict[str, Any]) -> ToolResult:
        """Execute a tool by name with given parameters."""
        pass

    @abstractmethod
    def get_tool_names(self) -> list[str]:
        """Return list of tool names this class handles."""
        pass
