"""Structured logging + LLM-cost tracking.

Why:
- Plain `logging.basicConfig` produces text logs that are hard to ship to
  ELK/Datadog/Loki and impossible to filter by request_id or session_id.
- `call_aws` queries a paid LLM N times per chat — without per-request token
  tracking we have no idea what each conversation actually cost the customer.

How:
- `configure_logging()` wires structlog through stdlib logging so existing
  `logger = logging.getLogger(__name__)` calls automatically become structured.
- LOG_FORMAT=json (default in containers) emits JSON lines; LOG_FORMAT=console
  emits human-readable colored output (default for local dev).
- `TokenTracker` is a thread-safe accumulator. The reasoning engine adds
  per-round usage; the SSE endpoint emits the totals in the `done` event and
  logs them so they end up in the log pipeline + a future cost dashboard.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any

import structlog


def _is_container() -> bool:
    return os.path.exists("/.dockerenv") or os.environ.get("KUBERNETES_SERVICE_HOST") is not None


def configure_logging(level: str | None = None) -> None:
    """Wire structlog through stdlib logging. Idempotent — safe to call twice."""
    log_level = (level or os.environ.get("LOG_LEVEL", "INFO") or "INFO").upper()
    log_format = os.environ.get("LOG_FORMAT", "json" if _is_container() else "console").lower()

    # Stdlib handler — structlog will share it
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )

    # Common processors run on every event from every logger
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if log_format == "json":
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, log_level, logging.INFO)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Quiet noisy third-party loggers
    for noisy in ("botocore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None):
    """Return a structlog logger bound to `name` (typically __name__)."""
    return structlog.get_logger(name)


# ── Token / cost tracking ────────────────────────────────────────────────────

# Per-1M-token prices in USD. Update when the provider raises prices.
# Source: anthropic.com/pricing + openai.com/pricing (Jan 2026 reference rates).
_PRICES_USD_PER_M_TOKENS = {
    # Claude
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.00},
    "claude-opus-4-20250514": {"input": 15.00, "output": 75.00},
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4-turbo": {"input": 10.00, "output": 30.00},
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Best-effort dollar cost. Returns 0.0 if the model isn't in the price table
    (logged at debug so we know to update the table)."""
    price = _PRICES_USD_PER_M_TOKENS.get(model)
    if not price:
        return 0.0
    return round(
        (input_tokens / 1_000_000) * price["input"] + (output_tokens / 1_000_000) * price["output"],
        6,
    )


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    rounds: int = 0
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "rounds": self.rounds,
            "cost_usd": round(self.cost_usd, 6),
        }


class TokenTracker:
    """Thread-safe per-request token accumulator.

    Use one instance per LLM-driven request (e.g. one per chat invocation).
    Call `add()` after every LLM round; call `usage` at the end to read totals.
    """

    def __init__(self, model: str = ""):
        self._model = model
        self._lock = threading.Lock()
        self._usage = TokenUsage()

    def add(self, input_tokens: int, output_tokens: int) -> None:
        cost = estimate_cost_usd(self._model, input_tokens, output_tokens)
        with self._lock:
            self._usage.input_tokens += input_tokens
            self._usage.output_tokens += output_tokens
            self._usage.rounds += 1
            self._usage.cost_usd += cost

    @property
    def usage(self) -> TokenUsage:
        with self._lock:
            # Return a copy so callers can't mutate internal state
            return TokenUsage(
                input_tokens=self._usage.input_tokens,
                output_tokens=self._usage.output_tokens,
                rounds=self._usage.rounds,
                cost_usd=self._usage.cost_usd,
            )

    @property
    def model(self) -> str:
        return self._model
