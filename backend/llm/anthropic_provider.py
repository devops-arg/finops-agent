import logging
from typing import Any, Optional

from backend.llm.provider import ChatResponse, LLMProvider
from backend.models.conversation import ToolCall

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        logger.info(f"Anthropic provider initialized: {model}")

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def format_tool_for_provider(self, tool_def: dict[str, Any]) -> dict[str, Any]:
        params = tool_def.get("parameters", {})
        if params.get("type") == "object" and "properties" in params:
            input_schema = params
        else:
            input_schema = {
                "type": "object",
                "properties": params,
                "required": list(params.keys()),
            }
        return {
            "name": tool_def["name"],
            "description": tool_def["description"],
            "input_schema": input_schema,
        }

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        import anthropic

        system_prompt = ""
        conversation_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                system_prompt = msg.get("content", "")
            else:
                conversation_messages.append(msg)

        if not conversation_messages:
            conversation_messages = [{"role": "user", "content": "Hello"}]

        api_params = {
            "model": self._model,
            "messages": conversation_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_prompt:
            api_params["system"] = system_prompt
        if tools:
            formatted = [self.format_tool_for_provider(t) for t in tools]
            api_params["tools"] = formatted

        try:
            response = self._client.messages.create(**api_params)
            return self._parse_response(response)
        except anthropic.RateLimitError as e:
            raise Exception(f"Anthropic rate limit: {e}")
        except anthropic.APIStatusError as e:
            raise Exception(f"Anthropic API error ({e.status_code}): {e.message}")
        except anthropic.APIError as e:
            raise Exception(f"Anthropic API error: {e}")

    def _parse_response(self, response) -> ChatResponse:
        content = ""
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        tool_name=block.name,
                        parameters=block.input if isinstance(block.input, dict) else {},
                    )
                )

        reason_map = {
            "tool_use": "tool_calls",
            "end_turn": "stop",
            "max_tokens": "length",
        }
        finish_reason = reason_map.get(response.stop_reason, response.stop_reason or "")

        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            }

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )
