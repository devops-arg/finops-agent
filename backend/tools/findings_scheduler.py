"""
Background scheduler for waste analyzer scans.

Runs once at startup only. Manual re-scans are triggered via POST /api/findings/refresh.
Results are persisted to SQLite via FindingsStore with a 'scanning' status flag while running.
"""

import asyncio
import logging
import os
from datetime import datetime

from backend.config.manager import AWSConfig, LocalStackConfig
from backend.tools.findings_store import FindingsStore
from backend.tools.waste_analyzers import run_all_analyzers

logger = logging.getLogger(__name__)

MOCK_ACCOUNT_ID = "666666666666"


def _resolve_account_id(is_mock: bool, aws_config: AWSConfig, localstack_config: LocalStackConfig) -> str:
    """Return the AWS account ID for this scan run.

    Mock mode → '666666666666' (sentinel, never a real account).
    Live mode  → real account from STS GetCallerIdentity.
    """
    if is_mock:
        return MOCK_ACCOUNT_ID
    try:
        import boto3

        kwargs: dict = {"region_name": aws_config.region}
        if aws_config.access_key_id:
            kwargs["aws_access_key_id"] = aws_config.access_key_id
        if aws_config.secret_access_key:
            kwargs["aws_secret_access_key"] = aws_config.secret_access_key
        sts = boto3.client("sts", **kwargs)
        return sts.get_caller_identity()["Account"]
    except Exception as e:
        logger.warning(f"Could not resolve AWS account via STS: {e}")
        return "unknown"


async def run_scan(store: FindingsStore, aws_config: AWSConfig, localstack_config: LocalStackConfig):
    """Execute one full scan and persist results."""
    is_mock = localstack_config.enabled or os.environ.get("USE_MOCK_DATA", "").lower() in ("true", "1", "yes")
    mode = "mock" if is_mock else "live"
    account_id = _resolve_account_id(is_mock, aws_config, localstack_config)
    logger.info(f"Starting waste scan (mode={mode}, account={account_id})...")
    start = datetime.utcnow()

    # Signal to the frontend that a scan is in progress
    store.set_scanning(True)
    scan_id = store.open_scan(mode, account_id)
    try:
        loop = asyncio.get_event_loop()

        def _progress(analyzer, region, done, total):
            store.set_progress(analyzer, region, done, total)

        def _findings_cb(findings, analyzer_name, region):
            """Persist each batch immediately so the UI shows results as they arrive."""
            store.append_batch(findings, scan_id)
            logger.debug(f"Saved {len(findings)} findings from {analyzer_name} [{region}]")

        import functools

        findings = await loop.run_in_executor(
            None,
            functools.partial(
                run_all_analyzers,
                aws_config,
                localstack_config,
                progress_cb=_progress,
                findings_cb=_findings_cb,
            ),
        )
        store.close_scan(scan_id)
        elapsed = round((datetime.utcnow() - start).total_seconds(), 1)
        total_savings = sum(f.estimated_savings_usd for f in findings)
        logger.info(
            f"Waste scan complete — {len(findings)} findings, "
            f"${total_savings:,.0f}/mo savings identified, "
            f"{elapsed}s elapsed (scan_id={scan_id})"
        )
    except Exception as e:
        logger.error(f"Waste scan failed: {e}", exc_info=True)
        store.close_scan(scan_id)
    finally:
        store.set_scanning(False)


async def findings_scheduler_loop(
    store: FindingsStore,
    aws_config: AWSConfig,
    localstack_config: LocalStackConfig,
):
    """Run a scan at startup — unless this AWS account already has results in the DB.

    Logic:
      1. Resolve the current account_id (mock=666666666666, live=real STS account).
      2. Check if a completed scan for that account exists.
      3. If yes → skip (user can trigger manually from the UI via POST /api/findings/refresh).
      4. If no  → run the initial scan automatically.
    """
    ttl_hours = float(os.environ.get("WASTE_SCAN_TTL_HOURS", "72"))  # default 3 days

    is_mock = localstack_config.enabled or os.environ.get("USE_MOCK_DATA", "").lower() in ("true", "1", "yes")
    account_id = _resolve_account_id(is_mock, aws_config, localstack_config)

    age = store.last_completed_scan_age_hours_for_account(account_id)

    if age is not None:
        if age < ttl_hours:
            logger.info(
                f"Skipping startup scan — account {account_id} already has a scan "
                f"completed {age:.1f}h ago (TTL={ttl_hours}h). "
                f"Use POST /api/findings/refresh to force a rescan."
            )
        else:
            logger.info(
                f"Account {account_id}: last scan was {age:.1f}h ago (>{ttl_hours}h TTL) — data is stale. "
                f"User must trigger a manual rescan via the UI or POST /api/findings/refresh."
            )
        return  # never auto-scan on restart if we already have data for this account

    # No scan for this account — run the initial scan automatically
    logger.info(f"No previous scan found for account {account_id} — running initial scan.")
    await run_scan(store, aws_config, localstack_config)
