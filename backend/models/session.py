from dataclasses import dataclass, field
from typing import Dict, List
from datetime import datetime
from backend.models.conversation import ConversationContext


@dataclass
class SessionState:
    session_id: str
    context: ConversationContext = None
    created_at: str = ""
    last_activity: str = ""

    def __post_init__(self):
        if not self.context:
            self.context = ConversationContext(session_id=self.session_id)
        if not self.created_at:
            self.created_at = datetime.utcnow().isoformat()
        if not self.last_activity:
            self.last_activity = self.created_at

    def add_message(self, role: str, content: str):
        self.context.add_message(role, content)
        self.last_activity = datetime.utcnow().isoformat()

    def get_messages_for_llm(self) -> List[Dict[str, str]]:
        return self.context.get_messages_for_llm()
