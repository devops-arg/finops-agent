import json
import logging
from typing import Any, Dict, List, Optional
from backend.llm.provider import LLMProvider, ChatResponse
from backend.models.conversation import ToolCall

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "gpt-4o"):
        from openai import OpenAI
        self._client = OpenAI(api_key=api_key)
        self._model = model
        logger.info(f"OpenAI provider initialized: {model}")

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def provider_name(self) -> str:
        return "openai"

    def format_tool_for_provider(self, tool_def: Dict[str, Any]) -> Dict[str, Any]:
        params = tool_def.get("parameters", {})
        if not params.get("type"):
            params = {
                "type": "object",
                "properties": params,
                "required": list(params.keys()),
            }
        return {
            "type": "function",
            "function": {
                "name": tool_def["name"],
                "description": tool_def["description"],
                "parameters": params,
            },
        }

    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> ChatResponse:
        from openai import RateLimitError, APIError

        api_params = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            formatted = [self.format_tool_for_provider(t) for t in tools]
            api_params["tools"] = formatted

        try:
            response = self._client.chat.completions.create(**api_params)
            return self._parse_response(response)
        except RateLimitError as e:
            raise Exception(f"OpenAI rate limit: {e}")
        except APIError as e:
            raise Exception(f"OpenAI API error: {e}")

    def _parse_response(self, response) -> ChatResponse:
        choice = response.choices[0]
        message = choice.message
        content = message.content or ""

        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    params = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    params = {}
                tool_calls.append(
                    ToolCall(
                        id=tc.id,
                        tool_name=tc.function.name,
                        parameters=params,
                    )
                )

        reason_map = {"tool_calls": "tool_calls", "stop": "stop", "length": "length"}
        finish_reason = reason_map.get(choice.finish_reason, choice.finish_reason or "")

        usage = {}
        if response.usage:
            usage = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            }

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )
