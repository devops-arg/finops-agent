import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from backend.tools.base import BaseTool
from backend.models.core import ToolResult
from backend.config.manager import AWSConfig, LocalStackConfig

logger = logging.getLogger(__name__)

TOOL_DEFINITIONS = [
    {
        "name": "query_aws_costs",
        "description": (
            "Query AWS Cost Explorer for cost and usage data. "
            "Can group by SERVICE, LINKED_ACCOUNT, REGION, USAGE_TYPE, INSTANCE_TYPE, "
            "or by tags like TAG:Environment, TAG:Project, TAG:Owner. "
            "Supports filters to narrow down results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD format (exclusive)",
                },
                "granularity": {
                    "type": "string",
                    "enum": ["DAILY", "MONTHLY"],
                    "description": "Time granularity. Default DAILY.",
                },
                "group_by": {
                    "type": "string",
                    "description": (
                        "Dimension or tag to group costs by. "
                        "Options: SERVICE, LINKED_ACCOUNT, REGION, USAGE_TYPE, INSTANCE_TYPE, "
                        "or a tag like TAG:Environment."
                    ),
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "Optional filters. Keys: SERVICE, LINKED_ACCOUNT, REGION, TAG:<name>. "
                        "Values: list of strings to match."
                    ),
                },
                "metrics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Cost metrics. Default ['UnblendedCost']. Options: UnblendedCost, BlendedCost, UsageQuantity, AmortizedCost, NetUnblendedCost.",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_cost_forecast",
        "description": (
            "Get AWS cost forecast for a future period. "
            "Uses Cost Explorer's ML-based forecasting to predict upcoming costs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Forecast start date (YYYY-MM-DD). Must be today or in the future.",
                },
                "end_date": {
                    "type": "string",
                    "description": "Forecast end date (YYYY-MM-DD, exclusive).",
                },
                "granularity": {
                    "type": "string",
                    "enum": ["DAILY", "MONTHLY"],
                    "description": "Granularity. Default MONTHLY.",
                },
                "metric": {
                    "type": "string",
                    "description": "Cost metric to forecast. Default UnblendedCost.",
                },
            },
            "required": ["start_date", "end_date"],
        },
    },
    {
        "name": "get_cost_anomalies",
        "description": (
            "Detect cost anomalies using AWS Cost Anomaly Detection. "
            "Returns unusual spending patterns over the specified period."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Start date (YYYY-MM-DD). Default: 30 days ago.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date (YYYY-MM-DD). Default: today.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "list_available_dimensions",
        "description": (
            "List available values for a Cost Explorer dimension "
            "(e.g., all service names, account IDs, regions in use). "
            "Useful for discovering what to filter or group by."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dimension": {
                    "type": "string",
                    "enum": ["SERVICE", "LINKED_ACCOUNT", "REGION", "USAGE_TYPE", "INSTANCE_TYPE"],
                    "description": "The dimension to list values for.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date (YYYY-MM-DD). Default: 30 days ago.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date (YYYY-MM-DD). Default: today.",
                },
            },
            "required": ["dimension"],
        },
    },
    {
        "name": "get_savings_plan_utilization",
        "description": (
            "Get Savings Plans or Reserved Instance utilization and coverage. "
            "Shows how well commitments are being used."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": "Start date (YYYY-MM-DD). Default: 30 days ago.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date (YYYY-MM-DD). Default: today.",
                },
                "report_type": {
                    "type": "string",
                    "enum": ["savings_plans", "reserved_instances"],
                    "description": "Type of commitment to report on. Default savings_plans.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_rightsizing_recommendations",
        "description": (
            "Get EC2 rightsizing recommendations from AWS Cost Explorer. "
            "Identifies instances that are over-provisioned or idle."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Service to get recommendations for. Default AmazonEC2.",
                },
                "lookback_days": {
                    "type": "integer",
                    "description": "Number of days to analyze. Options: 7, 14, 30, 60. Default 14.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_current_date",
        "description": (
            "Get the current date and useful date helpers for building cost queries: "
            "today, start of current/previous month, last 7/30/90 days, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


class AWSCostTools(BaseTool):
    def __init__(self, config: AWSConfig, localstack_config: LocalStackConfig = None):
        self._config = config
        self._localstack = localstack_config
        self._ce_client = None
        self._session = None

    def _get_session(self):
        if self._session:
            return self._session
        import boto3

        if self._config.profile and not (self._localstack and self._localstack.enabled):
            self._session = boto3.Session(
                profile_name=self._config.profile,
                region_name=self._config.region,
            )
        else:
            self._session = boto3.Session(
                aws_access_key_id=self._config.access_key_id or "test",
                aws_secret_access_key=self._config.secret_access_key or "test",
                region_name=self._config.region,
            )

        if self._config.assume_role_arn and not (self._localstack and self._localstack.enabled):
            sts = self._session.client("sts")
            creds = sts.assume_role(
                RoleArn=self._config.assume_role_arn,
                RoleSessionName="finops-agent",
            )["Credentials"]
            self._session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=self._config.region,
            )
        return self._session

    def _get_ce(self):
        if not self._ce_client:
            session = self._get_session()
            if self._localstack and self._localstack.enabled:
                # LocalStack free tier doesn't support Cost Explorer — use mock
                return None
            self._ce_client = session.client("ce", region_name="us-east-1")
        return self._ce_client

    def _is_localstack(self) -> bool:
        return bool(self._localstack and self._localstack.enabled)

    def _should_mock(self) -> bool:
        """Return True if tools should return mock data.

        Sources: localstack mode (no real AWS), or the USE_MOCK_DATA env flag.
        """
        import os
        if self._is_localstack():
            return True
        flag = os.environ.get("USE_MOCK_DATA", "").lower()
        return flag in ("true", "1", "yes")

    def get_definitions(self) -> List[Dict[str, Any]]:
        return TOOL_DEFINITIONS

    def get_tool_names(self) -> List[str]:
        return [t["name"] for t in TOOL_DEFINITIONS]

    def execute(self, tool_name: str, parameters: Dict[str, Any]) -> ToolResult:
        start = time.time()

        # Mock mode: either LocalStack or USE_MOCK_DATA=true flag
        if self._should_mock() and tool_name not in ("get_current_date",):
            try:
                data = self._mock_dispatch(tool_name, parameters)
                return ToolResult(
                    tool_name=tool_name, operation=tool_name, success=True,
                    data=data, execution_time=round(time.time() - start, 3),
                )
            except Exception as e:
                return ToolResult(
                    tool_name=tool_name, operation=tool_name, success=False,
                    error=str(e), execution_time=round(time.time() - start, 3),
                )

        handlers = {
            "query_aws_costs": self._query_costs,
            "get_cost_forecast": self._get_forecast,
            "get_cost_anomalies": self._get_anomalies,
            "list_available_dimensions": self._list_dimensions,
            "get_savings_plan_utilization": self._get_savings_utilization,
            "get_rightsizing_recommendations": self._get_rightsizing,
            "get_current_date": self._get_current_date,
        }
        handler = handlers.get(tool_name)
        if not handler:
            return ToolResult(tool_name=tool_name, operation=tool_name, success=False, error=f"Unknown tool: {tool_name}")

        try:
            data = handler(parameters)
            return ToolResult(
                tool_name=tool_name,
                operation=tool_name,
                success=True,
                data=data,
                execution_time=round(time.time() - start, 2),
            )
        except Exception as e:
            logger.error(f"AWS tool error [{tool_name}]: {e}")
            return ToolResult(
                tool_name=tool_name,
                operation=tool_name,
                success=False,
                error=str(e),
                execution_time=round(time.time() - start, 2),
            )

    def _mock_dispatch(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Route Cost Explorer queries to mock data in LocalStack mode."""
        from backend.tools.mock_data import generate_report, generate_daily_trend, generate_optimization

        report = generate_report()

        if tool_name == "query_aws_costs":
            group_by = params.get("group_by", "SERVICE")
            granularity = params.get("granularity", "MONTHLY")
            start_date = params.get("start_date", "")
            end_date = params.get("end_date", "")

            if group_by == "SERVICE":
                # Build per-service cost data matching the requested period
                service_costs = []
                for svc in report["byService"]:
                    # Use most recent week's cost as the period cost
                    latest_cost = list(svc["costs"].values())[-1]
                    service_costs.append({
                        "key": svc["name"],
                        "amount": round(latest_cost, 2),
                        "unit": "USD",
                        "monthly_estimate": svc["monthly_estimate"],
                    })
                service_costs.sort(key=lambda x: x["amount"], reverse=True)
                total = sum(s["amount"] for s in service_costs)

                if granularity == "DAILY":
                    # Return daily breakdown from mock daily trend filtered to date range
                    daily = [d for d in report["dailyTrend"] if start_date <= d["date"] <= end_date]
                    if not daily:
                        daily = report["dailyTrend"][-7:]
                    return {
                        "result_count": len(daily),
                        "time_period": {"start": start_date, "end": end_date},
                        "granularity": "DAILY",
                        "daily_costs": [{"date": d["date"], "total": d["cost"]} for d in daily],
                        "total": round(sum(d["cost"] for d in daily), 2),
                        "by_service": service_costs,
                        "currency": "USD",
                    }

                return {
                    "result_count": len(service_costs),
                    "time_period": {"start": start_date, "end": end_date},
                    "granularity": granularity,
                    "by_service": service_costs,
                    "total": round(total, 2),
                    "currency": "USD",
                }

            # Non-SERVICE group_by (ACCOUNT, REGION, USAGE_TYPE, TAG:xxx, INSTANCE_TYPE)
            from backend.tools.mock_data import ACCOUNTS, REGIONS, TEAMS, USAGE_TYPES_WEEKLY, ENVIRONMENTS
            total = report["summary"]["lastWeekCost"]
            gb_upper = group_by.upper() if isinstance(group_by, str) else ""

            if gb_upper == "LINKED_ACCOUNT":
                groups = [{"key": a["name"], "amount": round(total * a["pct"], 2)} for a in ACCOUNTS]
            elif gb_upper == "REGION":
                groups = [{"key": r["name"], "amount": round(total * r["pct"], 2), "label": r["label"]} for r in REGIONS]
            elif gb_upper == "USAGE_TYPE":
                groups = [{"key": ut["type"], "amount": round(ut["weekly"], 2), "category": ut["category"], "service": ut["service"]}
                          for ut in USAGE_TYPES_WEEKLY]
                groups.sort(key=lambda x: x["amount"], reverse=True)
            elif gb_upper.startswith("TAG:") or gb_upper == "TAG" or params.get("tag_key"):
                # Accept both TAG:Environment (AWS format) and tag_key=Environment
                tag_key = params.get("tag_key") or (group_by.split(":", 1)[1] if ":" in str(group_by) else "Team")
                tk_lower = tag_key.lower()
                if tk_lower in ("environment", "env"):
                    src = ENVIRONMENTS
                elif tk_lower in ("team", "owner", "department"):
                    src = TEAMS
                elif tk_lower in ("service", "application", "app"):
                    src = [
                        {"name": "api",      "pct": 0.40},
                        {"name": "workers",  "pct": 0.22},
                        {"name": "database", "pct": 0.18},
                        {"name": "frontend", "pct": 0.12},
                        {"name": "analytics","pct": 0.08},
                    ]
                else:
                    src = TEAMS
                groups = [{"key": f"{tag_key}${x['name']}", "amount": round(total * x["pct"], 2)} for x in src]
            elif gb_upper == "INSTANCE_TYPE":
                groups = [
                    {"key": "m5.xlarge",   "amount": round(total * 0.28, 2)},
                    {"key": "c6i.2xlarge", "amount": round(total * 0.18, 2)},
                    {"key": "m5.large",    "amount": round(total * 0.12, 2)},
                    {"key": "r5.xlarge",   "amount": round(total * 0.09, 2)},
                    {"key": "t3.large",    "amount": round(total * 0.06, 2)},
                ]
            else:
                groups = []

            return {
                "time_period": {"start": start_date, "end": end_date},
                "granularity": granularity,
                "group_by": group_by,
                "groups": groups,
                "total": round(total, 2),
                "currency": "USD",
            }
        elif tool_name == "get_cost_forecast":
            monthly = report["summary"]["monthlyProjection"]
            return {
                "total_forecast": round(monthly, 2),
                "periods": [{"mean": round(monthly, 2), "low": round(monthly * 0.92, 2), "high": round(monthly * 1.09, 2)}],
                "metric": "UNBLENDED_COST",
                "currency": "USD",
                "note": "LocalStack mode — trend-based projection",
            }
        elif tool_name == "get_cost_anomalies":
            return {
                "anomalies": report["anomalies"],
                "count": len(report["anomalies"]),
                "note": "LocalStack mode — simulated anomalies",
            }
        elif tool_name == "get_savings_plan_utilization":
            opt = generate_optimization()
            return {
                "type": "savings_plans",
                "utilization_percentage": 0,
                "savings_plans_active": 0,
                "recommendation": "No Savings Plans active. Potential savings: $380/mo with Compute Savings Plans.",
                "note": "LocalStack mode — simulated",
            }
        elif tool_name == "get_rightsizing_recommendations":
            opt = generate_optimization()
            recs = [r for r in opt["recommendations"] if r["type"] == "rightsizing"]
            return {
                "total_recommendations": len(recs),
                "estimated_total_monthly_savings": sum(r["monthly_savings"] for r in recs),
                "recommendations": recs,
                "note": "LocalStack mode — based on simulated utilization metrics",
            }
        elif tool_name == "list_available_dimensions":
            dimension = params.get("dimension", "SERVICE")
            if dimension == "SERVICE":
                return {"dimension": "SERVICE", "values": [{"value": s["name"]} for s in report["byService"]], "count": len(report["byService"])}
            return {"dimension": dimension, "values": [], "note": "LocalStack mode"}
        return {"note": f"LocalStack mode — {tool_name} not fully supported", "data": {}}

    def _query_costs(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ce = self._get_ce()
        start_date = params["start_date"]
        end_date = params["end_date"]
        granularity = params.get("granularity", "DAILY")
        group_by_key = params.get("group_by")
        filters = params.get("filters")
        metrics = params.get("metrics", ["UnblendedCost"])

        api_params = {
            "TimePeriod": {"Start": start_date, "End": end_date},
            "Granularity": granularity,
            "Metrics": metrics,
        }

        if group_by_key:
            if group_by_key.startswith("TAG:"):
                tag_key = group_by_key[4:]
                api_params["GroupBy"] = [{"Type": "TAG", "Key": tag_key}]
            else:
                api_params["GroupBy"] = [{"Type": "DIMENSION", "Key": group_by_key}]

        if filters:
            filter_expressions = []
            for key, values in filters.items():
                if not isinstance(values, list):
                    values = [values]
                if key.startswith("TAG:"):
                    tag_key = key[4:]
                    filter_expressions.append({"Tags": {"Key": tag_key, "Values": values, "MatchOptions": ["EQUALS"]}})
                else:
                    filter_expressions.append({"Dimensions": {"Key": key, "Values": values, "MatchOptions": ["EQUALS"]}})
            if len(filter_expressions) == 1:
                api_params["Filter"] = filter_expressions[0]
            elif len(filter_expressions) > 1:
                api_params["Filter"] = {"And": filter_expressions}

        results = ce.get_cost_and_usage(**api_params)
        return self._format_cost_results(results, group_by_key, metrics)

    def _format_cost_results(self, results: Dict, group_by: Optional[str], metrics: List[str]) -> Dict:
        periods = []
        total = 0.0

        for period in results.get("ResultsByTime", []):
            period_data = {
                "start": period["TimePeriod"]["Start"],
                "end": period["TimePeriod"]["End"],
            }
            if group_by and period.get("Groups"):
                groups = []
                for group in period["Groups"]:
                    keys = group.get("Keys", [])
                    name = keys[0] if keys else "Unknown"
                    amount = float(group["Metrics"].get(metrics[0], {}).get("Amount", 0))
                    total += amount
                    groups.append({"name": name, "cost": round(amount, 2)})
                groups.sort(key=lambda x: x["cost"], reverse=True)
                period_data["groups"] = groups[:50]
            else:
                amount = float(period.get("Total", {}).get(metrics[0], {}).get("Amount", 0))
                total += amount
                period_data["cost"] = round(amount, 2)
            periods.append(period_data)

        return {
            "periods": periods,
            "total": round(total, 2),
            "currency": "USD",
            "query": {"group_by": group_by, "metrics": metrics},
        }

    def _get_forecast(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ce = self._get_ce()
        start_date = params["start_date"]
        end_date = params["end_date"]
        granularity = params.get("granularity", "MONTHLY")
        metric = params.get("metric", "UNBLENDED_COST")

        metric_map = {
            "UnblendedCost": "UNBLENDED_COST",
            "BlendedCost": "BLENDED_COST",
            "AmortizedCost": "AMORTIZED_COST",
            "NetUnblendedCost": "NET_UNBLENDED_COST",
        }
        metric = metric_map.get(metric, metric)

        try:
            response = ce.get_cost_forecast(
                TimePeriod={"Start": start_date, "End": end_date},
                Granularity=granularity,
                Metric=metric,
            )
            total = float(response.get("Total", {}).get("Amount", 0))
            forecast_periods = []
            for item in response.get("ForecastResultsByTime", []):
                forecast_periods.append({
                    "start": item["TimePeriod"]["Start"],
                    "end": item["TimePeriod"]["End"],
                    "mean": round(float(item.get("MeanValue", 0)), 2),
                    "low": round(float(item.get("PredictionIntervalLowerBound", 0)), 2),
                    "high": round(float(item.get("PredictionIntervalUpperBound", 0)), 2),
                })
            return {
                "total_forecast": round(total, 2),
                "periods": forecast_periods,
                "metric": metric,
                "currency": "USD",
            }
        except ce.exceptions.DataUnavailableException:
            return {"error": "Not enough historical data for forecasting. Need at least 30 days of cost data."}

    def _get_anomalies(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ce = self._get_ce()
        today = datetime.utcnow().date()
        start_date = params.get("start_date", (today - timedelta(days=30)).isoformat())
        end_date = params.get("end_date", today.isoformat())

        try:
            response = ce.get_anomalies(
                DateInterval={"StartDate": start_date, "EndDate": end_date},
                MaxResults=50,
            )
            anomalies = []
            for a in response.get("Anomalies", []):
                impact = a.get("Impact", {})
                anomalies.append({
                    "id": a.get("AnomalyId"),
                    "start": a.get("AnomalyStartDate"),
                    "end": a.get("AnomalyEndDate"),
                    "score": a.get("AnomalyScore", {}).get("CurrentScore", 0),
                    "total_impact": float(impact.get("TotalImpact", 0)),
                    "total_actual_spend": float(impact.get("TotalActualSpend", 0)),
                    "total_expected_spend": float(impact.get("TotalExpectedSpend", 0)),
                    "root_causes": [
                        {
                            "service": rc.get("Service", ""),
                            "region": rc.get("Region", ""),
                            "account": rc.get("LinkedAccount", ""),
                            "usage_type": rc.get("UsageType", ""),
                        }
                        for rc in a.get("RootCauses", [])
                    ],
                })
            return {
                "anomalies": anomalies,
                "count": len(anomalies),
                "period": {"start": start_date, "end": end_date},
            }
        except Exception as e:
            if "Anomaly" in str(e) and ("not" in str(e).lower() or "enable" in str(e).lower()):
                return {"error": "Cost Anomaly Detection is not enabled. Enable it in AWS Cost Management console first.", "anomalies": []}
            raise

    def _list_dimensions(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ce = self._get_ce()
        dimension = params["dimension"]
        today = datetime.utcnow().date()
        start_date = params.get("start_date", (today - timedelta(days=30)).isoformat())
        end_date = params.get("end_date", today.isoformat())

        response = ce.get_dimension_values(
            TimePeriod={"Start": start_date, "End": end_date},
            Dimension=dimension,
        )
        values = []
        for v in response.get("DimensionValues", []):
            values.append({
                "value": v.get("Value", ""),
                "attributes": v.get("Attributes", {}),
            })
        return {
            "dimension": dimension,
            "values": values,
            "count": len(values),
        }

    def _get_savings_utilization(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ce = self._get_ce()
        today = datetime.utcnow().date()
        start_date = params.get("start_date", (today - timedelta(days=30)).isoformat())
        end_date = params.get("end_date", today.isoformat())
        report_type = params.get("report_type", "savings_plans")

        if report_type == "savings_plans":
            try:
                response = ce.get_savings_plans_utilization(
                    TimePeriod={"Start": start_date, "End": end_date},
                    Granularity="MONTHLY",
                )
                total = response.get("Total", {})
                return {
                    "type": "savings_plans",
                    "utilization_percentage": float(total.get("Utilization", {}).get("UtilizationPercentage", 0)),
                    "total_commitment": float(total.get("Utilization", {}).get("TotalCommitment", 0)),
                    "used_commitment": float(total.get("Utilization", {}).get("UsedCommitment", 0)),
                    "unused_commitment": float(total.get("Utilization", {}).get("UnusedCommitment", 0)),
                    "savings": float(total.get("Savings", {}).get("NetSavings", 0)),
                    "on_demand_equivalent": float(total.get("Savings", {}).get("OnDemandCostEquivalent", 0)),
                    "period": {"start": start_date, "end": end_date},
                }
            except Exception as e:
                return {"error": f"Could not retrieve Savings Plans data: {e}", "type": "savings_plans"}
        else:
            try:
                response = ce.get_reservation_utilization(
                    TimePeriod={"Start": start_date, "End": end_date},
                    Granularity="MONTHLY",
                )
                total = response.get("Total", {})
                util = total.get("UtilizationPercentage", "0")
                return {
                    "type": "reserved_instances",
                    "utilization_percentage": float(util),
                    "total_running_hours": float(total.get("TotalRunningHours", 0)),
                    "total_actual_hours": float(total.get("TotalActualHours", 0)),
                    "unused_hours": float(total.get("UnusedHours", 0)),
                    "net_savings": float(total.get("NetRISavings", 0)),
                    "on_demand_equivalent": float(total.get("OnDemandCostOfRIHoursUsed", 0)),
                    "period": {"start": start_date, "end": end_date},
                }
            except Exception as e:
                return {"error": f"Could not retrieve RI data: {e}", "type": "reserved_instances"}

    def _get_rightsizing(self, params: Dict[str, Any]) -> Dict[str, Any]:
        ce = self._get_ce()
        service = params.get("service", "AmazonEC2")
        lookback = params.get("lookback_days", 14)

        lookback_map = {7: "SEVEN_DAYS", 14: "FOURTEEN_DAYS", 30: "THIRTY_DAYS", 60: "SIXTY_DAYS"}
        lookback_period = lookback_map.get(lookback, "FOURTEEN_DAYS")

        try:
            response = ce.get_rightsizing_recommendation(
                Service=service,
                Configuration={
                    "RecommendationTarget": "SAME_INSTANCE_FAMILY",
                    "BenefitsConsidered": True,
                },
                LookbackPeriodInDays=lookback_period,
            )
            summary = response.get("Summary", {})
            recommendations = []
            for rec in response.get("RightsizingRecommendations", [])[:20]:
                current = rec.get("CurrentInstance", {})
                actions = rec.get("ModifyRecommendationDetail", {}).get("TargetInstances", [])
                target = actions[0] if actions else {}
                recommendations.append({
                    "account_id": rec.get("AccountId", ""),
                    "instance_id": current.get("ResourceId", ""),
                    "current_type": current.get("ResourceDetails", {}).get("EC2ResourceDetails", {}).get("InstanceType", ""),
                    "action": rec.get("RightsizingType", ""),
                    "target_type": target.get("ResourceDetails", {}).get("EC2ResourceDetails", {}).get("InstanceType", ""),
                    "estimated_monthly_savings": float(target.get("EstimatedMonthlySavings", "0") or "0"),
                    "current_monthly_cost": float(current.get("MonthlyCost", "0") or "0"),
                })
            return {
                "total_recommendations": int(summary.get("TotalRecommendationCount", 0)),
                "estimated_total_monthly_savings": float(summary.get("EstimatedTotalMonthlySavingsAmount", 0)),
                "savings_currency": summary.get("SavingsCurrencyCode", "USD"),
                "recommendations": recommendations,
            }
        except Exception as e:
            return {"error": f"Could not get rightsizing recommendations: {e}"}

    def _get_current_date(self, params: Dict[str, Any]) -> Dict[str, Any]:
        today = datetime.utcnow().date()
        return {
            "today": today.isoformat(),
            "yesterday": (today - timedelta(days=1)).isoformat(),
            "current_month_start": today.replace(day=1).isoformat(),
            "previous_month_start": (today.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat(),
            "previous_month_end": (today.replace(day=1) - timedelta(days=1)).isoformat(),
            "last_7_days_start": (today - timedelta(days=7)).isoformat(),
            "last_30_days_start": (today - timedelta(days=30)).isoformat(),
            "last_90_days_start": (today - timedelta(days=90)).isoformat(),
            "current_quarter_start": today.replace(month=((today.month - 1) // 3) * 3 + 1, day=1).isoformat(),
            "day_of_month": today.day,
            "days_remaining_in_month": (today.replace(month=today.month % 12 + 1, day=1) - timedelta(days=1)).day - today.day if today.month < 12 else (today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)).day - today.day,
            "hint": "Use these dates to build queries. Cost Explorer end_date is exclusive (does not include that day).",
        }
