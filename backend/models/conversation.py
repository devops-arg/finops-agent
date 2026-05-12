from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class ToolCall:
    id: str
    tool_name: str
    parameters: dict[str, Any]
    result: Optional[Any] = None
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat()


@dataclass
class Message:
    role: str  # "user", "assistant", "system"
    content: str
    id: str = ""
    timestamp: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)

    def __post_init__(self):
        if not self.id:
            import uuid

            self.id = str(uuid.uuid4())
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat()


@dataclass
class ConversationContext:
    session_id: str
    messages: list[Message] = field(default_factory=list)
    max_history: int = 10

    def add_message(self, role: str, content: str) -> Message:
        msg = Message(role=role, content=content)
        self.messages.append(msg)
        max_msgs = self.max_history * 2
        if len(self.messages) > max_msgs:
            self.messages = self.messages[-max_msgs:]
        return msg

    def get_messages_for_llm(self) -> list[dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in self.messages]
