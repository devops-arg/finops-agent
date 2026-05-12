"""Severity is derived from monthly savings — keep this contract stable."""

from __future__ import annotations

from backend.models.finding import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    Finding,
    _severity_from_savings,
)


def test_severity_thresholds():
    assert _severity_from_savings(500) == SEVERITY_CRITICAL
    assert _severity_from_savings(200) == SEVERITY_CRITICAL  # boundary
    assert _severity_from_savings(199.99) == SEVERITY_WARNING
    assert _severity_from_savings(50) == SEVERITY_WARNING  # boundary
    assert _severity_from_savings(49.99) == SEVERITY_INFO
    assert _severity_from_savings(0) == SEVERITY_INFO


def test_finding_default_account_is_mock_sentinel():
    """The default account_id is the mock sentinel — see CLAUDE.md I-2."""
    f = Finding(
        resource_id="i-test",
        resource_type="ec2_instance",
        service="EC2",
        category="cleanup",
        title="t",
        description="d",
        severity=SEVERITY_INFO,
        monthly_cost_usd=10.0,
        estimated_savings_usd=10.0,
        region="us-east-1",
    )
    assert f.account_id == "666666666666"
    assert f.id  # auto-generated UUID
    assert f.detected_at  # auto ISO timestamp
