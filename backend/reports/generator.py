import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
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

    def _is_localstack(self) -> bool:
        return bool(self._localstack and self._localstack.enabled)

    def _should_mock(self) -> bool:
        import os
        if self._is_localstack():
            return True
        flag = os.environ.get("USE_MOCK_DATA", "").lower()
        return flag in ("true", "1", "yes")

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

    def generate(self) -> Dict[str, Any]:
        if self._should_mock():
            return self._generate_mock()
        return self._generate_live()

    def _generate_mock(self) -> Dict[str, Any]:
        from backend.tools.mock_data import generate_report
        logger.info("Generating mock cost report (LocalStack mode)")
        report = generate_report(num_weeks=self._num_weeks)
        self._save(report)
        logger.info(f"Mock report generated: ${report['summary']['lastWeekCost']:.2f} last week")
        return report

    def _generate_live(self) -> Dict[str, Any]:
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

        by_account = sorted(by_account_all.values(), key=lambda x: list(x["costs"].values())[-1] if x["costs"] else 0, reverse=True)
        by_service = sorted(by_service_all.values(), key=lambda x: list(x["costs"].values())[-1] if x["costs"] else 0, reverse=True)
        by_env = sorted(by_env_all.values(), key=lambda x: list(x["costs"].values())[-1] if x["costs"] else 0, reverse=True)
        by_region = sorted(by_region_all.values(), key=lambda x: list(x["costs"].values())[-1] if x["costs"] else 0, reverse=True)

        last_week_cost = weekly_trend[-1]["cost"] if weekly_trend else 0
        prev_week_cost = weekly_trend[-2]["cost"] if len(weekly_trend) >= 2 else 0
        weekly_change = round(((last_week_cost - prev_week_cost) / prev_week_cost * 100) if prev_week_cost else 0, 1)
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
            "anomalies": [],
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

    def get_weeks(self) -> List[Dict[str, str]]:
        today = datetime.utcnow().date()
        days_since_sunday = (today.weekday() + 1) % 7
        if days_since_sunday == 0:
            days_since_sunday = 7
        last_sunday = today - timedelta(days=days_since_sunday)

        weeks = []
        for i in range(self._num_weeks):
            end = last_sunday - timedelta(weeks=i)
            start = end - timedelta(days=6)
            weeks.append({
                "start": start.isoformat(),
                "end": (end + timedelta(days=1)).isoformat(),
                "label": f"{start.strftime('%d/%m')} - {end.strftime('%d/%m')}",
            })
        weeks.reverse()
        return weeks

    def _fetch_costs_grouped(self, start: str, end: str, group_by: str) -> Dict[str, float]:
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

    def _fetch_costs_by_tag(self, start: str, end: str, tag_key: str) -> Dict[str, float]:
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
                    tag_val = tag_val[len(tag_key) + 1:] or "No Tag"
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

    def _save(self, report: Dict[str, Any]):
        with open(self._report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        logger.info(f"Report saved to {self._report_path}")

    def load_cached(self) -> Optional[Dict[str, Any]]:
        if not self._report_path.exists():
            return None
        try:
            with open(self._report_path) as f:
                data = json.load(f)
            # In mock mode (localstack OR USE_MOCK_DATA=true), always regenerate
            # so dates stay fresh relative to `today` — mock data is instant anyway.
            if self._should_mock():
                return None
            return data
        except Exception as e:
            logger.warning(f"Failed to load cached report: {e}")
            return None
