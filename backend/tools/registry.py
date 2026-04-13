import logging
from typing import Any, Dict, List, Optional
from backend.tools.base import BaseTool
from backend.models.core import ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry that maps tool names to their handler classes."""

    def __init__(self):
        self._providers: List[BaseTool] = []
        self._tool_map: Dict[str, BaseTool] = {}

    def register(self, provider: BaseTool):
        self._providers.append(provider)
        for name in provider.get_tool_names():
            if name in self._tool_map:
                logger.warning(f"Tool '{name}' already registered, overwriting")
            self._tool_map[name] = provider
        logger.info(f"Registered {len(provider.get_tool_names())} tools from {provider.__class__.__name__}")

    def get_all_definitions(self) -> List[Dict[str, Any]]:
        defs = []
        for provider in self._providers:
            defs.extend(provider.get_definitions())
        return defs

    def execute(self, tool_name: str, parameters: Dict[str, Any]) -> ToolResult:
        provider = self._tool_map.get(tool_name)
        if not provider:
            return ToolResult(
                tool_name=tool_name,
                operation="unknown",
                success=False,
                error=f"Unknown tool: {tool_name}",
            )
        try:
            return provider.execute(tool_name, parameters)
        except Exception as e:
            logger.error(f"Tool execution error [{tool_name}]: {e}")
            return ToolResult(
                tool_name=tool_name,
                operation=tool_name,
                success=False,
                error=str(e),
            )

    def has_tool(self, name: str) -> bool:
        return name in self._tool_map

    @property
    def tool_count(self) -> int:
        return len(self._tool_map)
