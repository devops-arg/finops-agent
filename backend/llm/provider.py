from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.models.conversation import ToolCall


@dataclass
class ChatResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)


class LLMProvider(ABC):
    @abstractmethod
    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        pass

    @abstractmethod
    def format_tool_for_provider(self, tool_def: dict[str, Any]) -> dict[str, Any]:
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        pass

    @property
    @abstractmethod
    def provider_name(self) -> str:
        pass
