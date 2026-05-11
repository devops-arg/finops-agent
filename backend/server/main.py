import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.config.manager import ConfigurationManager
from backend.knowledge.store import KnowledgeStore
from backend.llm.anthropic_provider import AnthropicProvider
from backend.llm.openai_provider import OpenAIProvider
from backend.models.session import SessionState
from backend.reasoning.engine import ReasoningEngine
from backend.reports.generator import ReportGenerator
from backend.tools.aws_api import AWSAPITool
from backend.tools.aws_costs import AWSCostTools
from backend.tools.aws_resources import AWSResourceTools
from backend.tools.findings_scheduler import findings_scheduler_loop
from backend.tools.findings_store import FindingsStore
from backend.tools.insights_scheduler import insights_scheduler_loop, run_insights
from backend.tools.insights_store import InsightsStore
from backend.tools.knowledge import KnowledgeTools
from backend.tools.registry import ToolRegistry
from backend.tools.waste_analyzers import WasteTools

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

sessions: Dict[str, SessionState] = {}
engine: ReasoningEngine = None
report_generator: ReportGenerator = None
knowledge_store: KnowledgeStore = None
findings_store: FindingsStore = None
insights_store: InsightsStore = None
_localstack_enabled: bool = False
_use_mock_data: bool = True
_aws_config = None         # AWSConfig reference for live boto3 calls
_localstack_config = None  # LocalStackConfig reference


def get_or_create_session(session_id: str = None) -> SessionState:
    if session_id and session_id in sessions:
        session = sessions[session_id]
        session.last_activity = datetime.utcnow().isoformat()
        return session
    sid = session_id or str(uuid.uuid4())
    session = SessionState(session_id=sid)
    sessions[sid] = session
    return session


async def cleanup_sessions():
    while True:
        await asyncio.sleep(300)
        cutoff = (datetime.utcnow() - timedelta(hours=1)).isoformat()
        expired = [sid for sid, s in sessions.items() if s.last_activity < cutoff]
        for sid in expired:
            del sessions[sid]
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired sessions")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, report_generator, knowledge_store, findings_store, insights_store, _localstack_enabled, _use_mock_data, _aws_config, _localstack_config

    config_mgr = ConfigurationManager()
    config = config_mgr.load_config()
    _use_mock_data = config.flags.use_mock_data
    _aws_config = config.aws

    errors = config_mgr.validate()
    if errors:
        for err in errors:
            logger.error(f"Config error: {err}")
        raise RuntimeError(f"Configuration errors: {'; '.join(errors)}")

    _localstack_enabled = config.localstack.enabled
    _localstack_config = config.localstack
    mode = "LocalStack" if _localstack_enabled else "AWS Live"
    logger.info(f"Starting in {mode} mode")
    logger.info(f"Feature flag USE_MOCK_DATA={_use_mock_data} (dashboard endpoints will use {'MOCK' if _use_mock_data else 'LIVE AWS'} data)")

    # ── Identity check: confirm which AWS identity we're using ─────────
    try:
        import boto3
        sts_kwargs = {}
        if _localstack_enabled:
            sts_kwargs = {
                "endpoint_url": config.localstack.url,
                "aws_access_key_id": "test",
                "aws_secret_access_key": "test",
                "region_name": "us-east-1",
            }
        sts = boto3.client("sts", **sts_kwargs)
        ident = sts.get_caller_identity()
        logger.info("=" * 60)
        logger.info(f"AWS IDENTITY CHECK")
        logger.info(f"  Account: {ident['Account']}")
        logger.info(f"  ARN:     {ident['Arn']}")
        logger.info(f"  UserId:  {ident['UserId']}")

        if not _localstack_enabled:
            # Dry-run write check: ec2 RunInstances with DryRun=True never creates anything.
            # AWS returns DryRunOperation  → caller WOULD have succeeded → has write access → warn.
            # AWS returns UnauthorizedOperation → no write permission → all good.
            # Supports role assumption via AWS_ASSUME_ROLE_ARN.
            try:
                ec2_session = boto3.Session(
                    region_name=config.aws.region or "us-east-1",
                    **({"profile_name": config.aws.profile} if config.aws.profile else {}),
                )
                if config.aws.assume_role_arn:
                    _sts = ec2_session.client("sts")
                    _creds = _sts.assume_role(
                        RoleArn=config.aws.assume_role_arn,
                        RoleSessionName="finops-write-check",
                    )["Credentials"]
                    ec2_session = boto3.Session(
                        aws_access_key_id=_creds["AccessKeyId"],
                        aws_secret_access_key=_creds["SecretAccessKey"],
                        aws_session_token=_creds["SessionToken"],
                        region_name=config.aws.region or "us-east-1",
                    )
                ec2 = ec2_session.client("ec2")
                ec2.run_instances(
                    ImageId="ami-00000000",
                    MinCount=1,
                    MaxCount=1,
                    DryRun=True,
                )
                # If we reach here (no exception) something unexpected happened
                has_write = True
            except Exception as dry_err:
                error_code = getattr(dry_err, "response", {}).get("Error", {}).get("Code", "")
                has_write = error_code == "DryRunOperation"

            if has_write:
                logger.warning("!" * 60)
                logger.warning("  ⚠  WARNING: WRITE ACCESS DETECTED")
                logger.warning(f"  Current identity: {ident['Arn']}")
                logger.warning("  This identity can create/terminate EC2 instances.")
                logger.warning("  The agent itself is read-only, but running with")
                logger.warning("  admin/write credentials is a security risk.")
                logger.warning("  Please create a dedicated read-only user:")
                logger.warning("    bash create-read-only.sh <your-admin-profile>")
                logger.warning("!" * 60)
            else:
                logger.info("  ✓ Write access check passed (ec2 dry-run → UnauthorizedOperation)")

        logger.info("=" * 60)
    except Exception as e:
        logger.error(f"AWS identity check FAILED: {e}")
        if not _localstack_enabled:
            raise RuntimeError(f"Cannot verify AWS identity in live mode: {e}")

    has_llm_key = (
        (config.llm.provider == "anthropic" and config.llm.anthropic_api_key)
        or (config.llm.provider == "openai" and config.llm.openai_api_key)
    )

    llm_provider = None
    if has_llm_key:
        if config.llm.provider == "anthropic":
            llm_provider = AnthropicProvider(
                api_key=config.llm.anthropic_api_key,
                model=config.llm.anthropic_model,
            )
        else:
            llm_provider = OpenAIProvider(
                api_key=config.llm.openai_api_key,
                model=config.llm.openai_model,
            )

    knowledge_store = KnowledgeStore()
    tool_registry = ToolRegistry()

    # Cost Explorer tools
    aws_tools = AWSCostTools(config.aws, config.localstack)
    tool_registry.register(aws_tools)

    # Resource/infrastructure tools
    resource_tools = AWSResourceTools(config.aws, config.localstack)
    tool_registry.register(resource_tools)

    # Knowledge base tools (optional)
    kb_tools = KnowledgeTools(knowledge_store)
    if knowledge_store.document_count > 0:
        tool_registry.register(kb_tools)
        logger.info(f"Knowledge base loaded: {knowledge_store.document_count} documents")
    else:
        logger.info("Knowledge base empty — run 'python scripts/setup.py' to populate it")

    # Generic AWS API tool (call_aws — any read-only AWS CLI command via boto3)
    aws_api_tool = AWSAPITool(config.aws, config.localstack)
    tool_registry.register(aws_api_tool)
    logger.info("Registered call_aws tool — LLM can now query any AWS API directly")

    # ── Findings store (SQLite) + waste tools ─────────────────────────────────
    findings_store = FindingsStore()
    waste_tools = WasteTools(findings_store)
    tool_registry.register(waste_tools)

    if llm_provider:
        engine = ReasoningEngine(llm_provider, tool_registry)
    else:
        logger.warning("No LLM API key — chat endpoint disabled, dashboard available")

    report_generator = ReportGenerator(config.aws, config.localstack, config.report.weeks)
    report_generator.use_mock_data = _use_mock_data

    insights_store = InsightsStore()

    cleanup_task = asyncio.create_task(cleanup_sessions())
    scan_task = asyncio.create_task(
        findings_scheduler_loop(findings_store, config.aws, config.localstack)
    )
    insights_task = asyncio.create_task(
        insights_scheduler_loop(insights_store, config.aws, config.localstack)
    )

    provider_info = f"provider={config.llm.provider}, model={llm_provider.model_name}" if llm_provider else "provider=NONE (no API key)"
    logger.info(
        f"FinOps Agent ready — mode={mode}, {provider_info}, tools={tool_registry.tool_count}"
    )

    yield

    for task in (cleanup_task, scan_task, insights_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="DevOps ARG — FinOps Intelligence Platform",
    description="AI-powered AWS cost analysis and infrastructure optimization",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    reset: bool = False


class ResetRequest(BaseModel):
    session_id: str


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "devopsarg-finops-agent",
        "version": "2.0.0",
        "website": "https://www.devopsarg.com",
    }


@app.get("/api/health")
async def health():
    findings_summary = findings_store.get_summary() if findings_store else {}
    return {
        "status": "ok",
        "version": "2.0.0",
        "mode": "localstack" if _localstack_enabled else "aws-live",
        "use_mock_data": _use_mock_data,
        "provider": engine._llm.provider_name if engine else "not initialized",
        "model": engine._llm.model_name if engine else "",
        "tools": engine._tools.tool_count if engine else 0,
        "knowledge_base_docs": knowledge_store.document_count if knowledge_store else 0,
        "findings_count": findings_summary.get("findings_count", 0),
        "total_savings_identified": findings_summary.get("total_savings_usd", 0),
        "last_scan_at": findings_summary.get("last_scan_at"),
        "last_scan_mode": findings_summary.get("last_scan_mode", "unknown"),
        "account_id": findings_summary.get("account_id", "unknown"),
        "scanning": findings_store.is_scanning() if findings_store else False,
    }


@app.get("/api/findings")
async def get_findings(
    service: Optional[str] = None,
    category: Optional[str] = None,
    severity: Optional[str] = None,
    min_savings: float = 0,
    region: Optional[str] = None,
):
    """Waste findings from automated infrastructure analyzers.

    Query params:
      service:     Filter by AWS service (EBS, RDS, EC2, Lambda, ...)
      category:    "cleanup" | "rightsize"
      severity:    "critical" | "warning" | "info"
      min_savings: Minimum monthly savings in USD
      region:      AWS region
    """
    if not findings_store:
        raise HTTPException(status_code=503, detail="Findings store not initialized")

    findings = findings_store.get_findings(
        service=service,
        category=category,
        severity=severity,
        min_savings=min_savings,
        region=region,
    )
    summary = findings_store.get_summary()
    ttl_hours = float(os.environ.get("WASTE_SCAN_TTL_HOURS", "72"))
    age_hours = findings_store.last_completed_scan_age_hours()
    scan_stale = (age_hours is not None) and (age_hours >= ttl_hours)
    return {
        "findings": findings,
        "total_count": len(findings),
        "total_savings_usd": round(sum(f.get("estimated_savings_usd", 0) for f in findings), 2),
        "total_waste_usd": round(sum(f.get("monthly_cost_usd", 0) for f in findings), 2),
        "last_scan_at": summary.get("last_scan_at"),
        "last_scan_mode": summary.get("last_scan_mode"),
        "account_id": summary.get("account_id", "unknown"),
        "scanning": findings_store.is_scanning(),
        "scan_progress": findings_store.get_progress() if findings_store.is_scanning() else {},
        "scan_stale": scan_stale,
        "scan_age_hours": age_hours,
        "summary": summary,
    }


@app.post("/api/findings/refresh")
async def refresh_findings():
    """Trigger an immediate waste scan (outside the hourly schedule)."""
    if not findings_store:
        raise HTTPException(status_code=503, detail="Findings store not initialized")
    from backend.tools.findings_scheduler import run_scan
    global _aws_config
    config_mgr = ConfigurationManager()
    config = config_mgr.load_config()
    asyncio.create_task(run_scan(findings_store, config.aws, config.localstack))
    return {"status": "ok", "message": "Scan triggered — results available in a few seconds"}


@app.get("/api/findings/trends")
async def get_findings_trends(service: Optional[str] = None, days: int = 30):
    """Historical trend of waste findings across scans."""
    if not findings_store:
        raise HTTPException(status_code=503, detail="Findings store not initialized")
    return {"trends": findings_store.get_trends(service=service, days=days), "days": days}


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest):
    if not engine:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    session = get_or_create_session(request.session_id)

    if request.reset:
        session.context.messages.clear()

    session.add_message("user", request.message)

    # Build findings context for system prompt injection
    findings_context = None
    if findings_store:
        summary = findings_store.get_summary()
        if summary.get("findings_count", 0) > 0:
            top = findings_store.get_findings(min_savings=50)[:5]
            lines = []
            for f in top:
                svc = f.get("service", "")
                title = f.get("title", "")
                savings = f.get("estimated_savings_usd", 0)
                rid = f.get("resource_id", "")
                lines.append(f"- [{svc}] {rid}: {title} — ${savings:.0f}/mo savings")
            total_s = summary.get("total_savings_usd", 0)
            findings_context = (
                f"\n## Current Waste Findings (auto-detected, last scan: {(summary.get('last_scan_at') or 'unknown')[:16]})\n"
                f"Total: {summary['findings_count']} findings, ${total_s:,.0f}/mo potential savings "
                f"({summary.get('critical_count', 0)} critical, {summary.get('warning_count', 0)} warnings)\n"
                + "\n".join(lines)
                + f"\n\nUse get_waste_findings tool to explore details. Use get_waste_summary for full breakdown."
            )

    async def event_generator():
        yield f"data: {json.dumps({'type': 'session', 'data': {'session_id': session.session_id}})}\n\n"

        history = session.get_messages_for_llm()
        history_arg = history[:-1] if len(history) > 1 else None

        # The reasoning engine is a sync generator — running it directly inside
        # an async function would block the event loop during every LLM call
        # (10+ seconds), so no events get flushed until the whole thing finishes.
        # We run it in a thread executor and pipe events through an asyncio.Queue.
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        SENTINEL = object()

        def worker():
            try:
                for ev in engine.process_query_stream(request.message, history_arg, findings_context=findings_context, use_mock_data=_use_mock_data):
                    asyncio.run_coroutine_threadsafe(queue.put(ev), loop)
            except Exception as e:
                logger.exception("Reasoning engine error in stream worker")
                asyncio.run_coroutine_threadsafe(
                    queue.put({"type": "error", "data": {"message": str(e)}}), loop,
                )
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(SENTINEL), loop)

        loop.run_in_executor(None, worker)

        while True:
            event = await queue.get()
            if event is SENTINEL:
                break
            # Flush immediately — no asyncio buffer between yields
            yield f"data: {json.dumps(event, default=str)}\n\n"
            if event.get("type") == "answer":
                content = event.get("data", {}).get("content", "")
                if content:
                    session.add_message("assistant", content)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
        },
    )


@app.post("/api/chat")
async def chat_rest(request: ChatRequest):
    if not engine:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    session = get_or_create_session(request.session_id)
    if request.reset:
        session.context.messages.clear()

    session.add_message("user", request.message)
    history = session.get_messages_for_llm()

    answer = ""
    tool_calls = []

    for event in engine.process_query_stream(request.message, history[:-1] if len(history) > 1 else None, use_mock_data=_use_mock_data):
        if event["type"] == "answer":
            answer = event["data"].get("content", "")
        elif event["type"] == "tool_call":
            tool_calls.append(event["data"])
        elif event["type"] == "error":
            raise HTTPException(status_code=500, detail=event["data"].get("message", "Unknown error"))

    if answer:
        session.add_message("assistant", answer)

    return {"answer": answer, "session_id": session.session_id, "tool_calls": tool_calls}


@app.get("/api/report")
async def get_report():
    if not report_generator:
        raise HTTPException(status_code=503, detail="Report generator not initialized")

    cached = report_generator.load_cached()
    if cached:
        return cached

    try:
        return report_generator.generate()
    except Exception as e:
        logger.error(f"Report generation error (falling back to mock): {e}")
        from backend.tools.mock_data import generate_report
        data = generate_report(num_weeks=4)
        data["mode"] = "mock-fallback"
        data["error"] = str(e)
        return data


@app.get("/api/report/trend")
async def get_report_trend(period: str = "1m"):
    """Return cost trend + service breakdown for the given period.

    period values: 3d, 1w, 1m, 3m, 1y
    Always live (no cache) so the chart reflects current data.
    """
    if not report_generator:
        raise HTTPException(status_code=503, detail="Report generator not initialized")
    valid = {"3d", "1w", "1m", "3m", "1y"}
    if period not in valid:
        raise HTTPException(status_code=400, detail=f"period must be one of {sorted(valid)}")
    try:
        return report_generator.get_trend_data(period)
    except Exception as e:
        logger.error(f"Trend data error for period={period}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/report/refresh")
async def refresh_report():
    if not report_generator:
        raise HTTPException(status_code=503, detail="Report generator not initialized")
    try:
        report = report_generator.generate()
        return {"status": "ok", "report": report}
    except Exception as e:
        logger.error(f"Report refresh error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/infrastructure")
async def get_infrastructure(region: str = None):
    """Infrastructure health: EC2, RDS, EKS, ElastiCache, OpenSearch, S3.

    Query params:
      region: Specific region to scan (e.g. "us-west-2"). Pass "all" to scan
              every enabled region in parallel (slow, ~20s). Defaults to
              AWS_DEFAULT_REGION from .env.

    Returns mock data when USE_MOCK_DATA=true, otherwise hits AWS describe-* APIs.
    """
    from backend.tools.mock_data import generate_infrastructure
    try:
        if _use_mock_data:
            return generate_infrastructure()

        from backend.tools.live_resources import fetch_live_infrastructure
        return fetch_live_infrastructure(_aws_config, region=region)
    except Exception as e:
        logger.error(f"Infrastructure query error: {e}")
        # Fallback to mock on error rather than 500
        from backend.tools.mock_data import generate_infrastructure
        data = generate_infrastructure()
        data["mode"] = "aws-live-fallback-to-mock"
        data["error"] = str(e)
        return data


@app.get("/api/optimize")
async def get_optimization():
    """Cost optimization recommendations.

    Returns mock data when USE_MOCK_DATA=true, otherwise derives recommendations
    from live AWS data (EC2 CPU, RDS utilization, S3 lifecycle policies, etc.).
    """
    from backend.tools.mock_data import generate_optimization
    try:
        if _use_mock_data:
            return generate_optimization()

        from backend.tools.live_resources import fetch_live_optimization
        return fetch_live_optimization(_aws_config)
    except Exception as e:
        logger.error(f"Optimization query error: {e}")
        data = generate_optimization()
        data["mode"] = "aws-live-fallback-to-mock"
        data["error"] = str(e)
        return data


@app.get("/api/report/export")
async def export_html_report():
    """Generate and return a self-contained HTML cost report (download)."""
    import asyncio
    from fastapi.responses import HTMLResponse
    from backend.reports.html_report import generate_html_report

    all_findings = findings_store.get_findings(limit=5000) if findings_store else []
    all_insights = insights_store.get_insights() if insights_store else []

    # Determine account label
    account_label = "AWS Account"
    try:
        if _aws_config and not (_localstack_config and _localstack_config.enabled):
            import boto3
            sts = boto3.client(
                "sts",
                region_name=_aws_config.region or "us-east-1",
                **({"aws_access_key_id": _aws_config.access_key_id,
                    "aws_secret_access_key": _aws_config.secret_access_key}
                   if _aws_config.access_key_id else {}),
            )
            identity = sts.get_caller_identity()
            account_label = f"Account {identity.get('Account', '')}"
    except Exception:
        pass

    # Fetch all dashboard data in parallel
    report_data   = None
    trend_data    = None
    infra_data    = None
    optimize_data = None

    async def _safe(coro):
        try:   return await coro
        except Exception: return None

    async def _get_report():
        if not report_generator: return None
        cached = report_generator.load_cached()
        return cached or report_generator.generate()

    async def _get_trend():
        if not report_generator: return None
        return report_generator.get_trend_data("1m")

    async def _get_infra():
        if _use_mock_data:
            from backend.tools.mock_data import generate_infrastructure
            return generate_infrastructure()
        from backend.tools.live_resources import fetch_live_infrastructure
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fetch_live_infrastructure(_aws_config))

    async def _get_optimize():
        if _use_mock_data:
            from backend.tools.mock_data import generate_optimization
            return generate_optimization()
        from backend.tools.live_resources import fetch_live_optimization
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: fetch_live_optimization(_aws_config))

    report_data, trend_data, infra_data, optimize_data = await asyncio.gather(
        _safe(_get_report()),
        _safe(_get_trend()),
        _safe(_get_infra()),
        _safe(_get_optimize()),
    )

    html = generate_html_report(
        all_findings,
        all_insights,
        account_label=account_label,
        report_data=report_data,
        trend_data=trend_data,
        infra_data=infra_data,
        optimize_data=optimize_data,
    )
    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": "attachment; filename=finops-report.html"},
    )


@app.get("/api/cost-by-tags")
async def get_cost_by_tags():
    """Fast Cost Explorer breakdown by the configured COST_TAG_KEYS.
    Runs live on every request (cached by Cost Explorer internally).
    Returns one entry per tag key with cost per tag value.
    """
    import asyncio, functools
    from backend.tools.insights_engine import check_cost_by_env_tag
    loop = asyncio.get_event_loop()
    try:
        insight = await loop.run_in_executor(
            None, functools.partial(check_cost_by_env_tag, _aws_config, _localstack_config)
        )
        return {"ok": True, "insight": insight.to_dict()}
    except Exception as e:
        logger.error(f"cost-by-tags error: {e}")
        return {"ok": False, "error": str(e), "insight": None}


@app.get("/api/insights")
async def get_insights():
    """Pre-computed billing insight checks (no LLM needed)."""
    if not insights_store:
        return {"insights": [], "summary": {}, "scanning": False}
    insights = insights_store.get_insights()
    summary = insights_store.get_summary()
    ttl = float(os.environ.get("INSIGHTS_TTL_HOURS", "12"))
    age = insights_store.last_run_age_hours()
    return {
        "insights": insights,
        "summary": summary,
        "scanning": insights_store.is_scanning(),
        "stale": (age is not None and age >= ttl),
        "age_hours": age,
    }


@app.post("/api/insights/refresh")
async def refresh_insights():
    """Trigger a fresh insights run."""
    if not insights_store:
        return {"status": "error", "message": "Insights store not initialized"}
    if insights_store.is_scanning():
        return {"status": "already_running"}
    asyncio.create_task(run_insights(insights_store, _aws_config, _localstack_config))
    return {"status": "started"}


@app.get("/api/knowledge/stats")
async def knowledge_stats():
    if not knowledge_store:
        return {"documents": 0, "status": "not initialized"}
    return {
        "documents": knowledge_store.document_count,
        "status": "loaded" if knowledge_store.document_count > 0 else "empty",
    }


class MockToggleRequest(BaseModel):
    use_mock_data: bool


@app.post("/api/config/mock")
async def toggle_mock_data(request: MockToggleRequest):
    global _use_mock_data
    _use_mock_data = request.use_mock_data
    if report_generator:
        report_generator.use_mock_data = _use_mock_data
        report_generator.invalidate_cache()
    logger.info(f"USE_MOCK_DATA toggled to {_use_mock_data} via API")
    return {"status": "ok", "use_mock_data": _use_mock_data}


@app.post("/api/reset")
async def reset_session(request: ResetRequest):
    if request.session_id in sessions:
        sessions[request.session_id].context.messages.clear()
    return {"status": "ok"}
