import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from backend.config.manager import AWSConfig, LocalStackConfig

logger = logging.getLogger(__name__)


class ReportGenerator:
    """Generates AWS cost reports. Uses mock data in LocalStack mode."""

    def __init__(self, aws_config: AWSConfig, localstack_config: LocalStackConfig = None, num_weeks: int = 4):
        self._config = aws_config
        self._localstack = localstack_config
        self._num_weeks = num_weeks
        self._ce_client = None
        self._report_path = Path("report_data.json")
        self.use_mock_data = True

    def _is_localstack(self) -> bool:
        return bool(self._localstack and self._localstack.enabled)

    def _should_mock(self) -> bool:
        return self.use_mock_data

    def _get_ce(self):
        if self._ce_client:
            return self._ce_client
        import boto3

        if self._config.profile:
            session = boto3.Session(
                profile_name=self._config.profile,
                region_name=self._config.region,
            )
        else:
            session = boto3.Session(
                aws_access_key_id=self._config.access_key_id,
                aws_secret_access_key=self._config.secret_access_key,
                region_name=self._config.region,
            )

        if self._config.assume_role_arn:
            sts = session.client("sts")
            creds = sts.assume_role(
                RoleArn=self._config.assume_role_arn,
                RoleSessionName="finops-report",
            )["Credentials"]
            session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=self._config.region,
            )

        self._ce_client = session.client("ce", region_name="us-east-1")
        return self._ce_client

    def generate(self) -> dict[str, Any]:
        if self._should_mock():
            return self._generate_mock()
        return self._generate_live()

    def _generate_mock(self) -> dict[str, Any]:
        from backend.tools.mock_data import generate_report

        logger.info("Generating mock cost report (LocalStack mode)")
        report = generate_report(num_weeks=self._num_weeks)
        self._save(report)
        logger.info(f"Mock report generated: ${report['summary']['lastWeekCost']:.2f} last week")
        return report

    def _generate_live(self) -> dict[str, Any]:
        logger.info("Generating live AWS cost report...")
        weeks = self.get_weeks()

        weekly_trend = []
        by_account_all = {}
        by_service_all = {}
        by_env_all = {}
        by_region_all = {}

        for week in weeks:
            start, end, label = week["start"], week["end"], week["label"]

            total = self._fetch_total_cost(start, end)
            weekly_trend.append({"week": label, "cost": round(total, 2)})

            accounts = self._fetch_costs_grouped(start, end, "LINKED_ACCOUNT")
            for acct, cost in accounts.items():
                if acct not in by_account_all:
                    by_account_all[acct] = {"id": acct, "name": acct, "costs": {}}
                by_account_all[acct]["costs"][label] = round(cost, 2)

            services = self._fetch_costs_grouped(start, end, "SERVICE")
            for svc, cost in services.items():
                if svc not in by_service_all:
                    by_service_all[svc] = {"name": svc, "costs": {}}
                by_service_all[svc]["costs"][label] = round(cost, 2)

            envs = self._fetch_costs_by_tag(start, end, "Environment")
            for env, cost in envs.items():
                if env not in by_env_all:
                    by_env_all[env] = {"name": env, "costs": {}}
                by_env_all[env]["costs"][label] = round(cost, 2)

            regions = self._fetch_costs_grouped(start, end, "REGION")
            for reg, cost in regions.items():
                if reg not in by_region_all:
                    by_region_all[reg] = {"name": reg, "costs": {}}
                by_region_all[reg]["costs"][label] = round(cost, 2)

        by_account = sorted(
            by_account_all.values(),
            key=lambda x: list(x["costs"].values())[-1] if x["costs"] else 0,
            reverse=True,
        )
        by_service = sorted(
            by_service_all.values(),
            key=lambda x: list(x["costs"].values())[-1] if x["costs"] else 0,
            reverse=True,
        )
        by_env = sorted(
            by_env_all.values(),
            key=lambda x: list(x["costs"].values())[-1] if x["costs"] else 0,
            reverse=True,
        )
        by_region = sorted(
            by_region_all.values(),
            key=lambda x: list(x["costs"].values())[-1] if x["costs"] else 0,
            reverse=True,
        )

        last_week_cost = weekly_trend[-1]["cost"] if weekly_trend else 0
        prev_week_cost = weekly_trend[-2]["cost"] if len(weekly_trend) >= 2 else 0
        weekly_change = round(
            ((last_week_cost - prev_week_cost) / prev_week_cost * 100) if prev_week_cost else 0, 1
        )
        four_week_total = sum(w["cost"] for w in weekly_trend)
        four_week_avg = round(four_week_total / len(weekly_trend), 2) if weekly_trend else 0
        monthly_projection = round(four_week_avg * 4.33, 2)

        report = {
            "generated": datetime.utcnow().isoformat() + "Z",
            "mode": "aws-live",
            "weeks": [w["label"] for w in weeks],
            "weeklyTrend": weekly_trend,
            "weeklyTrendFull": weekly_trend,
            "byAccount": by_account[:20],
            "byService": by_service[:20],
            "byEnvironment": by_env,
            "byRegion": by_region[:20],
            "anomalies": self._fetch_anomalies(),
            "summary": {
                "lastWeekCost": last_week_cost,
                "previousWeekCost": prev_week_cost,
                "weeklyChange": weekly_change,
                "fourWeekTotal": round(four_week_total, 2),
                "fourWeekAvg": four_week_avg,
                "monthlyProjection": monthly_projection,
                "mtdCost": 0,
                "topAccount": by_account[0]["name"] if by_account else "N/A",
                "topService": by_service[0]["name"] if by_service else "N/A",
                "topRegion": by_region[0]["name"] if by_region else "N/A",
                "activeAccounts": len(by_account),
                "activeServices": len(by_service),
                "activeRegions": len(by_region),
            },
        }

        self._save(report)
        logger.info(f"Live report generated: ${report['summary']['lastWeekCost']:.2f} USD last week")
        return report

    def _fetch_anomalies(self) -> list[dict]:
        """Fetch cost anomalies from AWS Cost Anomaly Detection (last 30 days)."""
        try:
            ce = self._get_ce()
            end = datetime.utcnow().date()
            start = end - timedelta(days=30)
            resp = ce.get_anomalies(
                DateInterval={"StartDate": str(start), "EndDate": str(end)},
                MaxResults=10,
            )
            anomalies = []
            for a in resp.get("Anomalies", []):
                impact = a.get("Impact", {})
                causes = a.get("RootCauses", [{}])
                svc = causes[0].get("Service", "Unknown") if causes else "Unknown"
                region = causes[0].get("Region", "") if causes else ""
                start_date = a.get("AnomalyStartDate", "")
                end_date = a.get("AnomalyEndDate", "")
                total_impact = float(impact.get("TotalImpact", 0))
                max_impact = float(impact.get("MaxImpact", 0))
                anomalies.append(
                    {
                        "id": a.get("AnomalyId", ""),
                        "service": svc,
                        "region": region,
                        "start_date": start_date,
                        "end_date": end_date,
                        "total_impact": round(total_impact, 2),
                        "max_impact": round(max_impact, 2),
                        "score": round(float(a.get("AnomalyScore", {}).get("MaxScore", 0)), 2),
                        "status": (a.get("AnomalyEndDate") and "closed") or "open",
                        "feedback": a.get("Feedback", ""),
                    }
                )
            anomalies.sort(key=lambda x: -x["total_impact"])
            return anomalies
        except Exception as e:
            logger.warning(f"Could not fetch anomalies: {e}")
            return []

    def get_weeks(self) -> list[dict[str, str]]:
        today = datetime.utcnow().date()
        days_since_sunday = (today.weekday() + 1) % 7
        if days_since_sunday == 0:
            days_since_sunday = 7
        last_sunday = today - timedelta(days=days_since_sunday)

        weeks = []
        for i in range(self._num_weeks):
            end = last_sunday - timedelta(weeks=i)
            start = end - timedelta(days=6)
            weeks.append(
                {
                    "start": start.isoformat(),
                    "end": (end + timedelta(days=1)).isoformat(),
                    "label": f"{start.strftime('%d/%m')} - {end.strftime('%d/%m')}",
                }
            )
        weeks.reverse()
        return weeks

    def _fetch_costs_grouped(self, start: str, end: str, group_by: str) -> dict[str, float]:
        ce = self._get_ce()
        response = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": group_by}],
        )
        totals = {}
        for period in response.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                name = group["Keys"][0] if group.get("Keys") else "Unknown"
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                totals[name] = totals.get(name, 0) + amount
        return totals

    def _fetch_costs_by_tag(self, start: str, end: str, tag_key: str) -> dict[str, float]:
        ce = self._get_ce()
        response = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "TAG", "Key": tag_key}],
        )
        totals = {}
        for period in response.get("ResultsByTime", []):
            for group in period.get("Groups", []):
                tag_val = group["Keys"][0] if group.get("Keys") else "No Tag"
                if tag_val.startswith(f"{tag_key}$"):
                    tag_val = tag_val[len(tag_key) + 1 :] or "No Tag"
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                totals[tag_val] = totals.get(tag_val, 0) + amount
        return totals

    def _fetch_total_cost(self, start: str, end: str) -> float:
        ce = self._get_ce()
        response = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )
        total = 0.0
        for period in response.get("ResultsByTime", []):
            total += float(period.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0))
        return total

    # ── Period trend (chart + service breakdown) ─────────────────────────────

    PERIOD_CONFIG: dict[str, dict] = {
        "3d": {"days": 3, "granularity": "DAILY", "label_fmt": "%a %d"},
        "1w": {"days": 7, "granularity": "DAILY", "label_fmt": "%a %d"},
        "1m": {"days": 30, "granularity": "DAILY", "label_fmt": "%d/%m"},
        "3m": {"days": 90, "granularity": "MONTHLY", "label_fmt": "%b"},
        "1y": {"days": 365, "granularity": "MONTHLY", "label_fmt": "%b %y"},
    }

    def get_trend_data(self, period: str = "1m") -> dict[str, Any]:
        if self._should_mock():
            return self._mock_trend_data(period)
        return self._live_trend_data(period)

    def _live_trend_data(self, period: str) -> dict[str, Any]:
        cfg = self.PERIOD_CONFIG.get(period, self.PERIOD_CONFIG["1m"])
        end = datetime.utcnow().date()
        start = end - timedelta(days=cfg["days"])
        gran = cfg["granularity"]
        fmt = cfg["label_fmt"]
        ce = self._get_ce()

        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity=gran,
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )

        trend_map: dict[str, float] = {}
        svc_totals: dict[str, float] = {}
        svc_timeline: dict[str, list[dict]] = {}  # service → [{label, cost}, ...]
        labels: list[str] = []

        for row in resp.get("ResultsByTime", []):
            dt = datetime.strptime(row["TimePeriod"]["Start"], "%Y-%m-%d")
            label = dt.strftime(fmt)
            if label not in labels:
                labels.append(label)
            day_total = 0.0
            for group in row.get("Groups", []):
                svc = group["Keys"][0]
                cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
                svc_totals[svc] = svc_totals.get(svc, 0) + cost
                day_total += cost
                if svc not in svc_timeline:
                    svc_timeline[svc] = []
                svc_timeline[svc].append({"label": label, "cost": round(cost, 2)})
            trend_map[label] = trend_map.get(label, 0) + day_total

        trend = [{"label": k, "cost": round(v, 2)} for k, v in trend_map.items()]
        total = sum(svc_totals.values())
        days = cfg["days"]
        by_service = sorted(
            [
                {
                    "name": k,
                    "cost": round(v, 2),
                    "pct": round(v / total * 100, 1) if total else 0,
                    "daily_avg": round(v / days, 2),
                    "timeline": svc_timeline.get(k, []),
                }
                for k, v in svc_totals.items()
                if v > 0.01
            ],
            key=lambda x: -x["cost"],
        )[:15]

        return {
            "period": period,
            "trend": trend,
            "labels": labels,
            "byService": by_service,
            "total": round(total, 2),
            "daily_avg": round(total / days, 2),
            "start": start.isoformat(),
            "end": end.isoformat(),
        }

    def _mock_trend_data(self, period: str) -> dict[str, Any]:
        import random

        cfg = self.PERIOD_CONFIG.get(period, self.PERIOD_CONFIG["1m"])
        end = datetime.utcnow().date()
        start = end - timedelta(days=cfg["days"])
        gran = cfg["granularity"]
        fmt = cfg["label_fmt"]
        rng = random.Random(42)
        base = 850.0

        SVC_BASES = [
            ("Amazon EC2", 0.35),
            ("Amazon RDS", 0.20),
            ("Amazon EKS", 0.15),
            ("Amazon S3", 0.10),
            ("AWS Lambda", 0.08),
            ("Amazon CloudFront", 0.05),
            ("Amazon ElastiCache", 0.04),
            ("Amazon CloudWatch", 0.03),
        ]

        # Build day-by-day labels and per-service timelines
        labels = []
        svc_timeline: dict[str, list[dict]] = {n: [] for n, _ in SVC_BASES}

        if gran == "DAILY":
            d = start
            while d <= end:
                lbl = d.strftime(fmt)
                labels.append(lbl)
                for svc, frac in SVC_BASES:
                    svc_timeline[svc].append(
                        {
                            "label": lbl,
                            "cost": round(base * frac * rng.uniform(0.75, 1.25), 2),
                        }
                    )
                d += timedelta(days=1)
        else:
            from datetime import date as _date

            d = _date(start.year, start.month, 1)
            while d <= end:
                lbl = d.strftime(fmt)
                labels.append(lbl)
                for svc, frac in SVC_BASES:
                    svc_timeline[svc].append(
                        {
                            "label": lbl,
                            "cost": round(base * 30 * frac * rng.uniform(0.9, 1.1), 2),
                        }
                    )
                d = _date(d.year + (d.month // 12), (d.month % 12) + 1, 1)

        trend = []
        for i, lbl in enumerate(labels):
            total_day = sum(svc_timeline[n][i]["cost"] for n, _ in SVC_BASES if i < len(svc_timeline[n]))
            trend.append({"label": lbl, "cost": round(total_day, 2)})

        days = cfg["days"]
        by_service = []
        for svc, frac in SVC_BASES:
            cost = sum(p["cost"] for p in svc_timeline[svc])
            by_service.append(
                {
                    "name": svc,
                    "cost": round(cost, 2),
                    "pct": round(frac * 100, 1),
                    "daily_avg": round(cost / max(days, 1), 2),
                    "timeline": svc_timeline[svc],
                }
            )

        total = sum(s["cost"] for s in by_service)
        return {
            "period": period,
            "trend": trend,
            "labels": labels,
            "byService": by_service,
            "total": round(total, 2),
            "daily_avg": round(total / max(days, 1), 2),
            "start": start.isoformat(),
            "end": end.isoformat(),
        }

    def _save(self, report: dict[str, Any]):
        with open(self._report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"Report saved to {self._report_path}")

    def invalidate_cache(self):
        if self._report_path.exists():
            self._report_path.unlink()
            logger.info("Report cache invalidated")

    def load_cached(self) -> Optional[dict[str, Any]]:
        if not self._report_path.exists():
            return None
        try:
            with open(self._report_path) as f:
                data = json.load(f)
            # Always regenerate in mock mode (dates stay fresh)
            if self._should_mock():
                return None
            # Don't serve a mock cache when we want live data
            if not self._should_mock() and data.get("mode") == "mock":
                return None
            return data
        except Exception as e:
            logger.warning(f"Failed to load cached report: {e}")
            return None
