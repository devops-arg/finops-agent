"""FindingsStore — open/append/close cycle + per-account isolation (CLAUDE.md I-2).

Mock and live findings must NEVER mix in the DB. account_id is forced to the
parent scan's account on every insert.
"""

from __future__ import annotations

from backend.models.finding import SEVERITY_INFO, Finding
from backend.tools.findings_store import FindingsStore


def _make_finding(rid: str, savings: float = 100.0, account: str = "ignored") -> Finding:
    """Note the `account=` param is intentionally bogus — the store should overwrite it."""
    return Finding(
        resource_id=rid,
        resource_type="ec2_instance",
        service="EC2",
        category="cleanup",
        title=f"idle {rid}",
        description="zero CPU 30 days",
        severity=SEVERITY_INFO,
        monthly_cost_usd=savings,
        estimated_savings_usd=savings,
        region="us-east-1",
        account_id=account,
    )


def test_open_append_close_cycle(tmp_db):
    store = FindingsStore(db_path=tmp_db)
    scan_id = store.open_scan(mode="mock", account_id="666666666666")
    assert scan_id

    store.append_batch([_make_finding("i-001"), _make_finding("i-002")], scan_id)
    store.close_scan(scan_id)

    findings = store.get_findings()
    assert len(findings) == 2
    summary = store.get_summary()
    assert summary["findings_count"] == 2
    assert summary["last_scan_mode"] == "mock"
    assert summary["account_id"] == "666666666666"


def test_account_id_is_overridden_by_scan_account(tmp_db):
    """I-2 invariant: even if a Finding ships with the wrong account_id,
    the store must overwrite it with the scan's account_id."""
    store = FindingsStore(db_path=tmp_db)
    scan_id = store.open_scan(mode="live", account_id="123456789012")

    # Caller passes a "polluted" account_id — store must ignore it
    store.append_batch([_make_finding("i-001", account="999999999999")], scan_id)
    store.close_scan(scan_id)

    findings = store.get_findings()
    assert len(findings) == 1
    # Cache and DB both reflect the corrected account
    assert findings[0]["account_id"] == "123456789012"


def test_filter_by_min_savings(tmp_db):
    store = FindingsStore(db_path=tmp_db)
    scan_id = store.open_scan(mode="mock", account_id="666666666666")
    store.append_batch(
        [
            _make_finding("small", savings=10),
            _make_finding("medium", savings=75),
            _make_finding("large", savings=500),
        ],
        scan_id,
    )
    store.close_scan(scan_id)

    big = store.get_findings(min_savings=100)
    assert {f["resource_id"] for f in big} == {"large"}

    medium_or_more = store.get_findings(min_savings=50)
    assert {f["resource_id"] for f in medium_or_more} == {"medium", "large"}


def test_filter_by_service_is_case_insensitive(tmp_db):
    store = FindingsStore(db_path=tmp_db)
    scan_id = store.open_scan(mode="mock", account_id="666666666666")
    store.append_batch([_make_finding("i-001")], scan_id)
    store.close_scan(scan_id)

    assert len(store.get_findings(service="ec2")) == 1
    assert len(store.get_findings(service="EC2")) == 1
    assert len(store.get_findings(service="rds")) == 0


def test_results_sorted_by_savings_desc(tmp_db):
    store = FindingsStore(db_path=tmp_db)
    scan_id = store.open_scan(mode="mock", account_id="666666666666")
    store.append_batch(
        [_make_finding("a", 10), _make_finding("b", 500), _make_finding("c", 100)],
        scan_id,
    )
    store.close_scan(scan_id)

    results = store.get_findings()
    savings = [r["estimated_savings_usd"] for r in results]
    assert savings == sorted(savings, reverse=True)


def test_empty_batch_is_safe(tmp_db):
    store = FindingsStore(db_path=tmp_db)
    scan_id = store.open_scan(mode="mock", account_id="666666666666")
    store.append_batch([], scan_id)
    store.close_scan(scan_id)
    assert store.get_findings() == []
