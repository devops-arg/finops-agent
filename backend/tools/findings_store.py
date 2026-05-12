"""
SQLite-backed FindingsStore.

Persistence layer for waste analyzer results.
- Survives container restarts
- Stores scan history for trend analysis
- Queryable by service / severity / category / min_savings
- In-memory hot cache (1h TTL) on top of SQLite for fast reads
"""

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from backend.models.finding import Finding

logger = logging.getLogger(__name__)

# Prefer FINDINGS_DB_PATH env var (set by docker-compose to /app/data/findings.db)
# Fall back to /app/findings.db for direct `python run_server.py` runs
_db_path_str = os.environ.get("FINDINGS_DB_PATH", "/app/findings.db")
DB_PATH = Path(_db_path_str)
CACHE_TTL_SECONDS = 3600


class FindingsStore:
    """Thread-safe SQLite store with in-memory hot cache."""

    def __init__(self, db_path: Path = DB_PATH):
        self._db_path: Optional[Path] = db_path
        self._lock = threading.Lock()
        self._cache: Optional[list[dict]] = None
        self._cache_at: Optional[datetime] = None
        self._last_scan_run: Optional[dict] = None
        self._scanning: bool = False
        self._scan_progress: dict = {}
        self._current_scan_id: Optional[str] = None
        self._current_account_id: str = "unknown"
        self._init_db()

    def set_scanning(self, value: bool):
        with self._lock:
            self._scanning = value
            if not value:
                self._scan_progress = {}

    def is_scanning(self) -> bool:
        with self._lock:
            return self._scanning

    def set_progress(self, analyzer: str, region: str, done: int, total: int):
        with self._lock:
            self._scan_progress = {
                "analyzer": analyzer,
                "region": region,
                "done": done,
                "total": total,
                "pct": round(done / total * 100) if total else 0,
            }

    def get_progress(self) -> dict:
        with self._lock:
            return dict(self._scan_progress)

    # ── Init ─────────────────────────────────────────────────────────────────

    def _init_db(self):
        if self._db_path is None:
            return
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS scan_runs (
                        id TEXT PRIMARY KEY,
                        started_at TEXT NOT NULL,
                        completed_at TEXT,
                        findings_count INTEGER DEFAULT 0,
                        total_savings_usd REAL DEFAULT 0,
                        total_waste_usd REAL DEFAULT 0,
                        mode TEXT DEFAULT 'mock',
                        account_id TEXT DEFAULT 'unknown'
                    );

                    CREATE TABLE IF NOT EXISTS findings (
                        id TEXT PRIMARY KEY,
                        scan_run_id TEXT NOT NULL,
                        resource_id TEXT NOT NULL,
                        resource_type TEXT,
                        service TEXT,
                        category TEXT,
                        title TEXT,
                        description TEXT,
                        severity TEXT,
                        monthly_cost_usd REAL DEFAULT 0,
                        estimated_savings_usd REAL DEFAULT 0,
                        idle_days INTEGER,
                        region TEXT,
                        account_id TEXT,
                        tags TEXT DEFAULT '{}',
                        metadata TEXT DEFAULT '{}',
                        detected_at TEXT,
                        FOREIGN KEY (scan_run_id) REFERENCES scan_runs(id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_findings_scan ON findings(scan_run_id);
                    CREATE INDEX IF NOT EXISTS idx_findings_service ON findings(service);
                    CREATE INDEX IF NOT EXISTS idx_findings_severity ON findings(severity);
                    CREATE INDEX IF NOT EXISTS idx_findings_category ON findings(category);
                    CREATE INDEX IF NOT EXISTS idx_scan_started ON scan_runs(started_at);
                    CREATE INDEX IF NOT EXISTS idx_scan_account ON scan_runs(account_id);
                    CREATE INDEX IF NOT EXISTS idx_findings_account ON findings(account_id);
                """)
                # Auto-migrate: add account_id to scan_runs if missing (existing DBs)
                existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(scan_runs)").fetchall()}
                if "account_id" not in existing_cols:
                    conn.execute("ALTER TABLE scan_runs ADD COLUMN account_id TEXT DEFAULT 'unknown'")
                    logger.info("Migrated scan_runs: added account_id column")
            logger.info(f"FindingsStore initialized at {self._db_path}")
        except Exception as e:
            logger.warning(f"FindingsStore DB init failed (will use in-memory only): {e}")
            self._db_path = None

    def _connect(self):
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Write ─────────────────────────────────────────────────────────────────

    def open_scan(self, mode: str = "live", account_id: str = "unknown") -> str:
        """Create a new scan_run record and return its ID.

        account_id: real AWS account number in live mode, '666666666666' in mock mode.
        Call this at scan start so findings appear incrementally via append_batch().
        """
        scan_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        with self._lock:
            self._current_scan_id = scan_id
            self._last_scan_run = {
                "id": scan_id,
                "started_at": now,
                "completed_at": None,
                "findings_count": 0,
                "total_savings_usd": 0.0,
                "total_waste_usd": 0.0,
                "mode": mode,
                "account_id": account_id,
            }
            self._current_account_id = account_id
            # Reset cache so partial results come from DB
            self._cache = []
            self._cache_at = datetime.utcnow()

        if self._db_path:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT INTO scan_runs VALUES (:id,:started_at,:completed_at,"
                        ":findings_count,:total_savings_usd,:total_waste_usd,:mode,:account_id)",
                        self._last_scan_run,
                    )
            except Exception as e:
                logger.error(f"open_scan DB error: {e}")

        return scan_id

    def append_batch(self, findings: list[Finding], scan_id: str):
        """Insert a batch of findings from one analyzer into the current scan.

        Immediately invalidates the in-memory cache so the next API poll
        picks up the new findings (incremental display while scanning).
        Each finding's account_id is overridden with the scan's account_id so
        mock findings get '666666666666' and live findings get the real account.
        """
        if not findings:
            return
        with self._lock:
            scan_account = self._current_account_id
        rows = [
            {
                **f.to_dict(),
                "scan_run_id": scan_id,
                "account_id": scan_account,
                "tags": json.dumps(f.tags),
                "metadata": json.dumps(f.metadata),
            }
            for f in findings
        ]
        with self._lock:
            # Update in-memory cache incrementally. The cache MUST use the same
            # account_id override as the DB — otherwise mock and live data leak
            # across modes (CLAUDE.md invariant I-2).
            existing = self._cache or []
            existing.extend(
                [
                    {**f.to_dict(), "account_id": scan_account, "tags": f.tags, "metadata": f.metadata}
                    for f in findings
                ]
            )
            self._cache = existing
            self._cache_at = datetime.utcnow()

        if self._db_path:
            try:
                with self._connect() as conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO findings VALUES "
                        "(:id,:scan_run_id,:resource_id,:resource_type,:service,:category,"
                        ":title,:description,:severity,:monthly_cost_usd,:estimated_savings_usd,"
                        ":idle_days,:region,:account_id,:tags,:metadata,:detected_at)",
                        rows,
                    )
            except Exception as e:
                logger.error(f"append_batch DB error: {e}")

    def close_scan(self, scan_id: str):
        """Mark a scan_run as completed with final totals."""
        now = datetime.utcnow().isoformat()
        with self._lock:
            all_findings = self._cache or []
            total_savings = round(sum(r.get("estimated_savings_usd", 0) for r in all_findings), 2)
            total_waste = round(sum(r.get("monthly_cost_usd", 0) for r in all_findings), 2)
            if self._last_scan_run:
                self._last_scan_run.update(
                    {
                        "completed_at": now,
                        "findings_count": len(all_findings),
                        "total_savings_usd": total_savings,
                        "total_waste_usd": total_waste,
                    }
                )

        if self._db_path:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE scan_runs SET completed_at=?, findings_count=?, "
                        "total_savings_usd=?, total_waste_usd=? WHERE id=?",
                        (now, len(all_findings), total_savings, total_waste, scan_id),
                    )
            except Exception as e:
                logger.error(f"close_scan DB error: {e}")

    def save_scan(self, findings: list[Finding], mode: str = "mock") -> str:
        """Persist a full scan result at once (used in mock/fallback mode). Returns scan_run_id."""
        scan_id = self.open_scan(mode)
        self.append_batch(findings, scan_id)
        self.close_scan(scan_id)
        return scan_id

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_findings(
        self,
        service: Optional[str] = None,
        category: Optional[str] = None,
        severity: Optional[str] = None,
        min_savings: float = 0,
        region: Optional[str] = None,
        account_id: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return findings from hot cache (or SQLite if cache expired)."""
        with self._lock:
            rows = self._get_cached_findings()

        results = rows
        if service:
            results = [r for r in results if r.get("service", "").lower() == service.lower()]
        if category:
            results = [r for r in results if r.get("category", "") == category]
        if severity:
            results = [r for r in results if r.get("severity", "") == severity]
        if min_savings > 0:
            results = [r for r in results if r.get("estimated_savings_usd", 0) >= min_savings]
        if region:
            results = [r for r in results if r.get("region", "") == region]
        if account_id:
            results = [r for r in results if r.get("account_id", "") == account_id]

        results.sort(key=lambda x: x.get("estimated_savings_usd", 0), reverse=True)
        return results[:limit]

    def get_summary(self) -> dict[str, Any]:
        """Aggregated summary for /api/health and system prompt injection."""
        with self._lock:
            rows = self._get_cached_findings()
            scan = self._last_scan_run or {}

        total_findings = len(rows)
        total_savings = round(sum(r.get("estimated_savings_usd", 0) for r in rows), 2)
        total_waste = round(sum(r.get("monthly_cost_usd", 0) for r in rows), 2)
        critical = sum(1 for r in rows if r.get("severity") == "critical")
        warning = sum(1 for r in rows if r.get("severity") == "warning")
        cleanup_count = sum(1 for r in rows if r.get("category") == "cleanup")
        rightsize_count = sum(1 for r in rows if r.get("category") == "rightsize")

        by_service: dict[str, dict] = {}
        for r in rows:
            svc = r.get("service", "Unknown")
            if svc not in by_service:
                by_service[svc] = {"count": 0, "savings": 0, "worst_severity": "info"}
            by_service[svc]["count"] += 1
            by_service[svc]["savings"] = round(
                by_service[svc]["savings"] + r.get("estimated_savings_usd", 0), 2
            )
            if r.get("severity") == "critical":
                by_service[svc]["worst_severity"] = "critical"
            elif r.get("severity") == "warning" and by_service[svc]["worst_severity"] != "critical":
                by_service[svc]["worst_severity"] = "warning"

        return {
            "findings_count": total_findings,
            "critical_count": critical,
            "warning_count": warning,
            "cleanup_count": cleanup_count,
            "rightsize_count": rightsize_count,
            "total_savings_usd": total_savings,
            "total_waste_usd": total_waste,
            "by_service": by_service,
            "last_scan_at": scan.get("completed_at"),
            "last_scan_mode": scan.get("mode", "unknown"),
            "account_id": scan.get("account_id", "unknown"),
        }

    def last_completed_scan_age_hours(self) -> Optional[float]:
        """Return how many hours ago the last completed scan finished (any account), or None if never."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT completed_at FROM scan_runs WHERE completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT 1"
                ).fetchone()
                if not row or not row["completed_at"]:
                    return None
                completed = datetime.fromisoformat(row["completed_at"])
                age = (datetime.utcnow() - completed).total_seconds() / 3600
                return round(age, 2)
        except Exception:
            return None

    def last_completed_scan_age_hours_for_account(self, account_id: str) -> Optional[float]:
        """Return hours since the last completed scan for a specific account, or None if never scanned."""
        if not self._db_path:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT completed_at FROM scan_runs "
                    "WHERE completed_at IS NOT NULL AND account_id = ? "
                    "ORDER BY completed_at DESC LIMIT 1",
                    (account_id,),
                ).fetchone()
                if not row or not row["completed_at"]:
                    return None
                completed = datetime.fromisoformat(row["completed_at"])
                age = (datetime.utcnow() - completed).total_seconds() / 3600
                return round(age, 2)
        except Exception:
            return None

    def get_account_id_for_latest_scan(self) -> Optional[str]:
        """Return the account_id of the most recent completed scan."""
        if not self._db_path:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT account_id FROM scan_runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                return row["account_id"] if row else None
        except Exception:
            return None

    def get_trends(self, service: Optional[str] = None, days: int = 30) -> list[dict]:
        """Return per-scan summary for trend charts. SQLite only."""
        if not self._db_path:
            return []
        try:
            since = (datetime.utcnow() - timedelta(days=days)).isoformat()
            with self._connect() as conn:
                if service:
                    rows = conn.execute(
                        """
                        SELECT sr.completed_at, COUNT(f.id) AS count,
                               SUM(f.estimated_savings_usd) AS savings
                        FROM scan_runs sr
                        JOIN findings f ON f.scan_run_id = sr.id
                        WHERE sr.started_at >= ? AND f.service = ?
                        GROUP BY sr.id ORDER BY sr.started_at
                        """,
                        (since, service),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT completed_at, findings_count AS count, total_savings_usd AS savings
                        FROM scan_runs WHERE started_at >= ?
                        ORDER BY started_at
                        """,
                        (since,),
                    ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"get_trends error: {e}")
            return []

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_cached_findings(self) -> list[dict]:
        """Return in-memory cache if fresh, else reload from SQLite."""
        if self._cache is not None and self._cache_at:
            age = (datetime.utcnow() - self._cache_at).total_seconds()
            if age < CACHE_TTL_SECONDS:
                return self._cache

        if not self._db_path:
            return self._cache or []

        try:
            with self._connect() as conn:
                latest_scan = conn.execute(
                    "SELECT id, account_id FROM scan_runs ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                if not latest_scan:
                    return []
                rows = conn.execute(
                    "SELECT * FROM findings WHERE scan_run_id = ?",
                    (latest_scan["id"],),
                ).fetchall()
                from backend.models.finding import compute_fix_command

                enriched = []
                for r in rows:
                    d = dict(r)
                    d["tags"] = json.loads(d.get("tags") or "{}")
                    d["metadata"] = json.loads(d.get("metadata") or "{}")
                    d["fix_command"] = compute_fix_command(d)
                    enriched.append(d)
                self._cache = enriched
                self._cache_at = datetime.utcnow()
                return self._cache
        except Exception as e:
            logger.error(f"_get_cached_findings error: {e}")
            return self._cache or []

    def invalidate_cache(self):
        with self._lock:
            self._cache = None
            self._cache_at = None
