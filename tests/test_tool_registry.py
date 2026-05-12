"""ToolRegistry — register, dispatch, error handling."""

from __future__ import annotations

from typing import Any

from backend.models.core import ToolResult
from backend.tools.base import BaseTool
from backend.tools.registry import ToolRegistry


class _FakeTool(BaseTool):
    """Trivial tool used to exercise the registry without touching AWS."""

    def __init__(self, names: list[str]):
        self._names = names
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_definitions(self) -> list[dict[str, Any]]:
        return [
            {"name": n, "description": f"fake {n}", "parameters": {"type": "object", "properties": {}}}
            for n in self._names
        ]

    def get_tool_names(self) -> list[str]:
        return self._names

    def execute(self, tool_name: str, parameters: dict[str, Any]) -> ToolResult:
        self.calls.append((tool_name, parameters))
        return ToolResult(tool_name=tool_name, operation=tool_name, success=True, data={"echo": parameters})


class _ExplodingTool(BaseTool):
    def get_definitions(self) -> list[dict[str, Any]]:
        return [{"name": "boom", "description": "raises", "parameters": {}}]

    def get_tool_names(self) -> list[str]:
        return ["boom"]

    def execute(self, tool_name: str, parameters: dict[str, Any]) -> ToolResult:
        raise RuntimeError("kaboom")


def test_registry_dispatches_by_name():
    reg = ToolRegistry()
    fake = _FakeTool(["alpha", "beta"])
    reg.register(fake)

    result = reg.execute("beta", {"x": 1})
    assert result.success is True
    assert result.data == {"echo": {"x": 1}}
    assert fake.calls == [("beta", {"x": 1})]


def test_registry_unknown_tool_returns_failure_not_exception():
    reg = ToolRegistry()
    result = reg.execute("nope", {})
    assert result.success is False
    assert "Unknown tool" in result.error


def test_registry_catches_tool_exceptions():
    reg = ToolRegistry()
    reg.register(_ExplodingTool())
    result = reg.execute("boom", {})
    assert result.success is False
    assert "kaboom" in result.error


def test_registry_aggregates_definitions_across_providers():
    reg = ToolRegistry()
    reg.register(_FakeTool(["a"]))
    reg.register(_FakeTool(["b", "c"]))
    names = {d["name"] for d in reg.get_all_definitions()}
    assert names == {"a", "b", "c"}
    assert reg.tool_count == 3


def test_registry_has_tool():
    reg = ToolRegistry()
    reg.register(_FakeTool(["a"]))
    assert reg.has_tool("a") is True
    assert reg.has_tool("z") is False
