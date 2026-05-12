"""
Insights scheduler — runs billing checks at startup (respects TTL).
"""

import asyncio
import functools
import logging
import os

from backend.config.manager import AWSConfig, LocalStackConfig
from backend.tools.insights_engine import run_all_insights
from backend.tools.insights_store import InsightsStore

logger = logging.getLogger(__name__)


async def run_insights(store: InsightsStore, aws: AWSConfig, ls: LocalStackConfig):
    store.set_scanning(True)
    try:
        loop = asyncio.get_event_loop()

        def _progress(name, done, total):
            logger.info(f"Insights ({done}/{total}) {name}")

        insights = await loop.run_in_executor(
            None, functools.partial(run_all_insights, aws, ls, progress_cb=_progress)
        )
        store.save(insights)
        total_savings = sum(i.savings_usd for i in insights)
        critical = sum(1 for i in insights if i.status == "critical")
        warning = sum(1 for i in insights if i.status == "warning")
        logger.info(
            f"Insights complete — {len(insights)} checks, "
            f"{_fmt(total_savings)}/mo savings found, "
            f"{critical} critical, {warning} warnings"
        )
    except Exception as e:
        logger.error(f"Insights run failed: {e}", exc_info=True)
    finally:
        store.set_scanning(False)


def _fmt(n: float) -> str:
    return f"${n:,.0f}"


async def insights_scheduler_loop(store: InsightsStore, aws: AWSConfig, ls: LocalStackConfig):
    ttl_hours = float(os.environ.get("INSIGHTS_TTL_HOURS", "12"))
    age = store.last_run_age_hours()

    if age is not None and age < ttl_hours:
        logger.info(
            f"Skipping insights run — last run {age:.1f}h ago (TTL={ttl_hours}h). "
            f"POST /api/insights/refresh to force."
        )
        return

    if age is not None:
        logger.info(f"Insights data is {age:.1f}h old (TTL={ttl_hours}h) — refreshing.")
    else:
        logger.info("No previous insights — running initial checks.")

    await run_insights(store, aws, ls)
