"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so `import backend.*` works when
# pytest is invoked from any cwd.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Strip every FINOPS/AWS-related env var before each test AND chdir to a
    tmp dir so ConfigurationManager doesn't pick up the developer's real .env
    when running locally. Tests that need a specific env set it explicitly via
    monkeypatch.setenv()."""
    for var in list(os.environ):
        if var.startswith(
            (
                "AWS_",
                "ANTHROPIC_",
                "OPENAI_",
                "USE_",
                "LOCALSTACK_",
                "FINDINGS_",
                "INSIGHTS_",
                "WASTE_",
                "COST_",
                "CORS_",
                "REPORT_",
                "AI_PROVIDER",
                "PORT",
                "HOST",
            )
        ):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    """Temp SQLite path for FindingsStore tests."""
    return tmp_path / "findings.db"
