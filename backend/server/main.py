import asyncio
import json
import logging
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
from backend.tools.aws_costs import AWSCostTools
from backend.tools.aws_resources import AWSResourceTools
from backend.tools.knowledge import KnowledgeTools
from backend.tools.registry import ToolRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

sessions: Dict[str, SessionState] = {}
engine: ReasoningEngine = None
report_generator: ReportGenerator = None
knowledge_store: KnowledgeStore = None
_localstack_enabled: bool = False
_use_mock_data: bool = True
_aws_config = None  # AWSConfig reference for live boto3 calls


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
    global engine, report_generator, knowledge_store, _localstack_enabled, _use_mock_data, _aws_config

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
        if not _localstack_enabled and "readonly" not in ident["Arn"].lower() and "read-only" not in ident["Arn"].lower():
            logger.warning(
                f"  ⚠ ARN does not contain 'readonly' — verify this user has "
                f"read-only permissions! Current ARN: {ident['Arn']}"
            )
        logger.info("=" * 60)
    except Exception as e:
        logger.error(f"AWS identity check FAILED: {e}")
        if not _localstack_enabled:
            raise RuntimeError(f"Cannot verify AWS identity in live mode: {e}")

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

    engine = ReasoningEngine(llm_provider, tool_registry)
    report_generator = ReportGenerator(config.aws, config.localstack, config.report.weeks)

    cleanup_task = asyncio.create_task(cleanup_sessions())

    logger.info(
        f"FinOps Agent ready — mode={mode}, provider={config.llm.provider}, "
        f"model={llm_provider.model_name}, tools={tool_registry.tool_count}"
    )

    yield

    cleanup_task.cancel()
    try:
        await cleanup_task
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
    return {
        "status": "ok",
        "version": "2.0.0",
        "mode": "localstack" if _localstack_enabled else "aws-live",
        "use_mock_data": _use_mock_data,
        "provider": engine._llm.provider_name if engine else "not initialized",
        "model": engine._llm.model_name if engine else "",
        "tools": engine._tools.tool_count if engine else 0,
        "knowledge_base_docs": knowledge_store.document_count if knowledge_store else 0,
    }


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest):
    if not engine:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    session = get_or_create_session(request.session_id)

    if request.reset:
        session.context.messages.clear()

    session.add_message("user", request.message)

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
                for ev in engine.process_query_stream(request.message, history_arg):
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

    for event in engine.process_query_stream(request.message, history[:-1] if len(history) > 1 else None):
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
        logger.error(f"Report generation error: {e}")
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


@app.get("/api/knowledge/stats")
async def knowledge_stats():
    if not knowledge_store:
        return {"documents": 0, "status": "not initialized"}
    return {
        "documents": knowledge_store.document_count,
        "status": "loaded" if knowledge_store.document_count > 0 else "empty",
    }


@app.post("/api/reset")
async def reset_session(request: ResetRequest):
    if request.session_id in sessions:
        sessions[request.session_id].context.messages.clear()
    return {"status": "ok"}
