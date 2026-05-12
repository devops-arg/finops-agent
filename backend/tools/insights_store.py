"""
InsightsStore — SQLite-backed cache for pre-computed billing insights.
Same pattern as FindingsStore but simpler (no scan_runs table needed).
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from backend.models.insight import Insight

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DATA_DIR", "/app/data")) / "insights.db"


class InsightsStore:
    def __init__(self, db_path: Path = DB_PATH):
        self._db_path: Optional[Path] = db_path
        self._lock = threading.Lock()
        self._cache: Optional[list[dict]] = None
        self._cache_at: Optional[datetime] = None
        self._scanning = False
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        if self._db_path is None:
            return
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS insights (
                        id TEXT PRIMARY KEY,
                        category TEXT,
                        title TEXT,
                        value TEXT,
                        status TEXT,
                        detail TEXT,
                        recommendation TEXT,
                        savings_usd REAL DEFAULT 0,
                        affected_count INTEGER DEFAULT 0,
                        region TEXT,
                        detected_at TEXT,
                        context TEXT DEFAULT '{}'
                    );
                    CREATE INDEX IF NOT EXISTS idx_insights_category ON insights(category);
                    CREATE INDEX IF NOT EXISTS idx_insights_status ON insights(status);
                """)
        except Exception as e:
            logger.warning(f"InsightsStore DB init failed (will use memory): {e}")
            self._db_path = None

    def is_scanning(self) -> bool:
        return self._scanning

    def set_scanning(self, v: bool):
        self._scanning = v

    def save(self, insights: list[Insight]):
        """Replace all insights with fresh results."""
        rows = [{**i.to_dict(), "context": json.dumps(i.context)} for i in insights]
        with self._lock:
            self._cache = [{**r, "context": i.context} for r, i in zip(rows, insights, strict=False)]
            self._cache_at = datetime.utcnow()

        if self._db_path:
            try:
                with self._connect() as conn:
                    conn.execute("DELETE FROM insights")
                    conn.executemany(
                        "INSERT OR REPLACE INTO insights VALUES "
                        "(:id,:category,:title,:value,:status,:detail,:recommendation,"
                        ":savings_usd,:affected_count,:region,:detected_at,:context)",
                        rows,
                    )
            except Exception as e:
                logger.error(f"InsightsStore save error: {e}")

    def get_insights(self) -> list[dict]:
        with self._lock:
            if self._cache is not None:
                return list(self._cache)

        if not self._db_path:
            return []
        try:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM insights ORDER BY status, category, title").fetchall()
                result = []
                for r in rows:
                    d = dict(r)
                    d["context"] = json.loads(d.get("context") or "{}")
                    result.append(d)
                with self._lock:
                    self._cache = result
                    self._cache_at = datetime.utcnow()
                return result
        except Exception as e:
            logger.error(f"InsightsStore get error: {e}")
            return []

    def last_run_age_hours(self) -> Optional[float]:
        """Hours since last insights run, or None if never."""
        if not self._db_path:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT detected_at FROM insights ORDER BY detected_at DESC LIMIT 1"
                ).fetchone()
                if not row or not row["detected_at"]:
                    return None
                dt = datetime.fromisoformat(row["detected_at"])
                return round((datetime.utcnow() - dt).total_seconds() / 3600, 2)
        except Exception:
            return None

    def get_summary(self) -> dict[str, Any]:
        insights = self.get_insights()
        total_savings = sum(i.get("savings_usd", 0) for i in insights)
        by_status: dict[str, int] = {}
        for i in insights:
            s = i.get("status", "info")
            by_status[s] = by_status.get(s, 0) + 1
        return {
            "total": len(insights),
            "total_savings_usd": round(total_savings, 2),
            "by_status": by_status,
            "last_run_age_hours": self.last_run_age_hours(),
        }
