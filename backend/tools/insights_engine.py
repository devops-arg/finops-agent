"""
InsightsEngine — 20 pre-computed billing checks that run against live AWS APIs.
No LLM required. Results are plain Python dicts, fast and free.

Checks are grouped into categories:
  cost         — spend analysis (Cost Explorer)
  networking   — NAT, data transfer, VPC endpoints
  commitments  — Savings Plans, RIs, on-demand ratio
  compute      — EC2 rightsizing, Spot, gp2→gp3
  storage      — S3 lifecycle, CloudWatch Logs, EBS snapshots
  database     — RDS multi-AZ in non-prod, idle, snapshot cost
  lambda       — oversized memory, deprecated runtimes
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config as BotocoreConfig

from backend.config.manager import AWSConfig, LocalStackConfig
from backend.models.insight import (
    Insight,
    CATEGORY_COST, CATEGORY_NETWORKING, CATEGORY_COMMITMENTS,
    CATEGORY_COMPUTE, CATEGORY_STORAGE, CATEGORY_OBSERVABILITY,
    STATUS_OK, STATUS_INFO, STATUS_WARNING, STATUS_CRITICAL,
)  # no security category — this engine is billing-only

logger = logging.getLogger(__name__)

_BOTO_CFG = BotocoreConfig(connect_timeout=10, read_timeout=20, retries={"max_attempts": 2})

# Lambda runtimes that are EOL or approaching EOL
_DEPRECATED_RUNTIMES = {
    "nodejs10.x", "nodejs12.x", "nodejs14.x",
    "python2.7", "python3.6", "python3.7", "python3.8",
    "ruby2.5", "ruby2.7",
    "java8", "dotnetcore2.1", "dotnetcore3.1",
    "go1.x",
}


def _client(service: str, aws: AWSConfig, ls: LocalStackConfig, region: str = None):
    region = region or aws.region or "us-east-1"
    if ls.enabled:
        return boto3.client(service, config=_BOTO_CFG,
                            endpoint_url=ls.url,
                            aws_access_key_id="test",
                            aws_secret_access_key="test",
                            region_name=region)
    kw: Dict[str, Any] = {"region_name": region}
    if aws.access_key_id:
        kw["aws_access_key_id"] = aws.access_key_id
    if aws.secret_access_key:
        kw["aws_secret_access_key"] = aws.secret_access_key
    return boto3.client(service, config=_BOTO_CFG, **kw)


def _fmt(n: float) -> str:
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n/1_000:.1f}K"
    return f"${n:.0f}"


# ── Cost Explorer helpers ───────────────────────────────────────────────────

def _ce_total(ce, start: str, end: str, filters: Dict = None) -> float:
    kw = dict(TimePeriod={"Start": start, "End": end},
              Granularity="MONTHLY", Metrics=["UnblendedCost"])
    if filters:
        kw["Filter"] = filters
    resp = ce.get_cost_and_usage(**kw)
    return sum(float(r["Total"]["UnblendedCost"]["Amount"])
               for r in resp.get("ResultsByTime", []))


def _ce_by_service(ce, start: str, end: str) -> List[Dict]:
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY", Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}]
    )
    out = []
    for r in resp.get("ResultsByTime", []):
        for g in r.get("Groups", []):
            out.append({"service": g["Keys"][0],
                        "cost": float(g["Metrics"]["UnblendedCost"]["Amount"])})
    return sorted(out, key=lambda x: -x["cost"])


# ════════════════════════════════════════════════════════════════════════════
# INDIVIDUAL CHECKS
# ════════════════════════════════════════════════════════════════════════════

def check_top_cost_drivers(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """Top 5 AWS services by cost this month."""
    try:
        ce = _client("ce", aws, ls, "us-east-1")
        end = datetime.utcnow().date()
        start = end.replace(day=1)
        services = _ce_by_service(ce, str(start), str(end))
        top5 = services[:5]
        total = sum(s["cost"] for s in services)
        top_name = top5[0]["service"] if top5 else "—"
        top_cost = top5[0]["cost"] if top5 else 0
        pct = round(top_cost / total * 100) if total else 0
        detail_lines = " · ".join(f"{s['service'].replace('Amazon ','').replace('AWS ','')} {_fmt(s['cost'])}"
                                  for s in top5)
        return Insight(
            category=CATEGORY_COST,
            title="Top cost drivers this month",
            value=f"{top_name.replace('Amazon ','').replace('AWS ','')} {_fmt(top_cost)} ({pct}%)",
            status=STATUS_INFO,
            detail=f"Top 5: {detail_lines}. Total month-to-date: {_fmt(total)}.",
            recommendation="Review top services for rightsizing or commitment opportunities.",
            context={"top_services": top5, "total_mtd": round(total, 2)},
        )
    except Exception as e:
        logger.warning(f"check_top_cost_drivers failed: {e}")
        return Insight(category=CATEGORY_COST, title="Top cost drivers", value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def check_cost_trend(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """This week vs last week cost change."""
    try:
        ce = _client("ce", aws, ls, "us-east-1")
        today = datetime.utcnow().date()
        this_start = str(today - timedelta(days=7))
        last_start = str(today - timedelta(days=14))
        this_week = _ce_total(ce, this_start, str(today))
        last_week = _ce_total(ce, last_start, this_start)
        if last_week > 0:
            change_pct = round((this_week - last_week) / last_week * 100, 1)
        else:
            change_pct = 0
        direction = "▲" if change_pct > 0 else "▼" if change_pct < 0 else "="
        status = STATUS_WARNING if change_pct > 20 else STATUS_OK if change_pct <= 5 else STATUS_INFO
        return Insight(
            category=CATEGORY_COST,
            title="Cost trend: this week vs last week",
            value=f"{direction} {abs(change_pct)}% ({_fmt(this_week)} vs {_fmt(last_week)})",
            status=status,
            detail=f"Last 7 days: {_fmt(this_week)}. Previous 7 days: {_fmt(last_week)}. Change: {change_pct:+.1f}%.",
            recommendation="Investigate spikes above 20% — usually caused by new deployments or scaling events.",
            context={"this_week_usd": round(this_week, 2), "last_week_usd": round(last_week, 2), "change_pct": change_pct},
        )
    except Exception as e:
        logger.warning(f"check_cost_trend failed: {e}")
        return Insight(category=CATEGORY_COST, title="Cost trend", value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def check_cost_by_region(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """Cost breakdown by AWS region this month."""
    try:
        ce = _client("ce", aws, ls, "us-east-1")
        end = datetime.utcnow().date()
        start = end.replace(day=1)
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": str(start), "End": str(end)},
            Granularity="MONTHLY", Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "REGION"}]
        )
        regions = {}
        for r in resp.get("ResultsByTime", []):
            for g in r.get("Groups", []):
                reg = g["Keys"][0]
                regions[reg] = regions.get(reg, 0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
        regions = {k: v for k, v in regions.items() if v > 0.5}
        top_region = max(regions, key=regions.get) if regions else "—"
        detail_parts = " · ".join(f"{r} {_fmt(c)}" for r, c in
                                   sorted(regions.items(), key=lambda x: -x[1])[:5])
        return Insight(
            category=CATEGORY_COST,
            title="Cost by region (month-to-date)",
            value=f"{top_region} leads at {_fmt(regions.get(top_region, 0))}",
            status=STATUS_INFO,
            detail=f"Region breakdown: {detail_parts}.",
            recommendation="Consolidate workloads into fewer regions to reduce cross-region data transfer.",
            context={"by_region": regions},
        )
    except Exception as e:
        logger.warning(f"check_cost_by_region failed: {e}")
        return Insight(category=CATEGORY_COST, title="Cost by region", value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def check_cost_anomalies(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """Anomalies detected by AWS Cost Anomaly Detection in last 30 days."""
    try:
        ce = _client("ce", aws, ls, "us-east-1")
        end = datetime.utcnow().date()
        start = end - timedelta(days=30)
        resp = ce.get_anomalies(
            DateInterval={"StartDate": str(start), "EndDate": str(end)},
            MaxResults=10
        )
        anomalies = resp.get("Anomalies", [])
        total_impact = sum(a.get("Impact", {}).get("TotalImpact", 0) for a in anomalies)
        if not anomalies:
            return Insight(category=CATEGORY_COST, title="Cost anomalies (30 days)",
                           value="None detected", status=STATUS_OK,
                           detail="No cost anomalies detected in the last 30 days.",
                           recommendation="Keep monitoring — set budget alerts for early detection.",
                           context={"anomalies": []})
        top = anomalies[0]
        svc = top.get("RootCauses", [{}])[0].get("Service", "Unknown")
        return Insight(
            category=CATEGORY_COST,
            title="Cost anomalies (30 days)",
            value=f"{len(anomalies)} anomalies, {_fmt(total_impact)} total impact",
            status=STATUS_WARNING if total_impact > 100 else STATUS_INFO,
            detail=f"{len(anomalies)} anomaly/anomalies detected. Largest: {svc}, {_fmt(top.get('Impact',{}).get('TotalImpact',0))} impact.",
            recommendation="Review anomalies in Cost Explorer — they often indicate runaway resources.",
            savings_usd=round(total_impact, 2),
            affected_count=len(anomalies),
            context={"anomaly_count": len(anomalies), "total_impact": round(total_impact, 2)},
        )
    except Exception as e:
        logger.warning(f"check_cost_anomalies failed: {e}")
        return Insight(category=CATEGORY_COST, title="Cost anomalies", value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def _cost_tag_keys() -> List[str]:
    """Tag keys to analyze, from COST_TAG_KEYS env var. Default: env."""
    raw = os.environ.get("COST_TAG_KEYS", "env")
    return [k.strip() for k in raw.split(",") if k.strip()]


def check_cost_by_env_tag(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """Cost split by configurable tag keys (from COST_TAG_KEYS env var)."""
    tag_keys = _cost_tag_keys()
    primary_key = tag_keys[0]
    try:
        ce = _client("ce", aws, ls, "us-east-1")
        end = datetime.utcnow().date()
        start = end.replace(day=1)

        all_breakdowns: Dict[str, Dict[str, float]] = {}

        for tag_key in tag_keys:
            resp = ce.get_cost_and_usage(
                TimePeriod={"Start": str(start), "End": str(end)},
                Granularity="MONTHLY", Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "TAG", "Key": tag_key}]
            )
            by_val: Dict[str, float] = {}
            for r in resp.get("ResultsByTime", []):
                for g in r.get("Groups", []):
                    # Cost Explorer returns values as "tagkey$tagvalue"
                    raw_val = g["Keys"][0]
                    tag_val = raw_val.split("$", 1)[-1] if "$" in raw_val else raw_val
                    tag_val = tag_val or "untagged"
                    by_val[tag_val] = by_val.get(tag_val, 0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
            all_breakdowns[tag_key] = by_val

        # Primary key for the main display
        by_env = all_breakdowns.get(primary_key, {})
        untagged = by_env.get("untagged", 0)
        total = sum(by_env.values())
        untagged_pct = round(untagged / total * 100) if total else 0

        detail_parts = " · ".join(
            f"{k} {_fmt(v)}" for k, v in sorted(by_env.items(), key=lambda x: -x[1]) if v > 0.5
        )

        extra_keys = tag_keys[1:]
        extra_note = ""
        if extra_keys:
            extra_lines = []
            for k in extra_keys:
                bv = all_breakdowns.get(k, {})
                unt = bv.get("untagged", 0)
                tot = sum(bv.values())
                unt_pct = round(unt / tot * 100) if tot else 0
                if unt_pct > 0:
                    extra_lines.append(f"{k}: {unt_pct}% untagged")
            if extra_lines:
                extra_note = " Also checked: " + ", ".join(extra_lines) + "."

        keys_str = ", ".join(tag_keys)
        status = STATUS_WARNING if untagged_pct > 30 else STATUS_INFO
        return Insight(
            category=CATEGORY_COST,
            title=f"Cost by tag: {keys_str}",
            value=f"{untagged_pct}% untagged ({primary_key})" if untagged_pct > 10 else detail_parts[:60] or "All tagged",
            status=status,
            detail=f"Tag `{primary_key}` breakdown: {detail_parts}. "
                   f"{untagged_pct}% of spend has no `{primary_key}` tag.{extra_note}",
            recommendation=f"Tag all resources with {keys_str} to enable cost chargebacks. "
                           f"Configure tag keys via COST_TAG_KEYS in .env.",
            context={
                "tag_keys": tag_keys,
                "breakdowns": {k: v for k, v in all_breakdowns.items()},
                "primary_untagged_pct": untagged_pct,
            },
        )
    except Exception as e:
        logger.warning(f"check_cost_by_env_tag failed: {e}")
        return Insight(category=CATEGORY_COST,
                       title=f"Cost by tag: {', '.join(tag_keys)}",
                       value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def check_nat_gateway_cost(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """NAT Gateway spend + estimate savings from VPC Endpoints."""
    try:
        ce = _client("ce", aws, ls, "us-east-1")
        end = datetime.utcnow().date()
        start = end - timedelta(days=30)
        nat_cost = _ce_total(ce, str(start), str(end), filters={
            "Dimensions": {"Key": "SERVICE", "Values": ["Amazon Virtual Private Cloud"]}
        })
        # VPC Endpoints save ~$0.01/GB for S3/DynamoDB/ECR traffic via NAT
        # Estimate: if NAT > $50/mo, VPC endpoints for S3+ECR can save 30-60%
        est_savings = round(nat_cost * 0.40, 2) if nat_cost > 50 else 0
        status = STATUS_WARNING if nat_cost > 200 else STATUS_INFO if nat_cost > 50 else STATUS_OK
        return Insight(
            category=CATEGORY_NETWORKING,
            title="NAT Gateway cost",
            value=_fmt(nat_cost) + "/mo",
            status=status,
            detail=f"VPC/NAT costs {_fmt(nat_cost)}/month. NAT charges $0.045/GB processed. "
                   f"VPC Endpoints for S3, ECR, DynamoDB, STS eliminate this traffic entirely (free endpoints).",
            recommendation="Create VPC Endpoints for S3, ECR, DynamoDB, STS, CloudWatch. Each eliminates NAT data charges for that service.",
            savings_usd=est_savings,
            context={"nat_cost_usd": round(nat_cost, 2), "estimated_savings": est_savings},
        )
    except Exception as e:
        logger.warning(f"check_nat_gateway_cost failed: {e}")
        return Insight(category=CATEGORY_NETWORKING, title="NAT Gateway cost", value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def check_vpc_endpoints_missing(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """Check if VPC Endpoints exist for the high-traffic services (S3, ECR, DynamoDB, STS)."""
    try:
        region = aws.region or "us-east-1"
        ec2 = _client("ec2", aws, ls, region)
        resp = ec2.describe_vpc_endpoints(
            Filters=[{"Name": "state", "Values": ["available", "pending"]}]
        )
        existing = set()
        for ep in resp.get("VpcEndpoints", []):
            svc = ep.get("ServiceName", "")
            for key in ("s3", "ecr.api", "ecr.dkr", "dynamodb", "sts", "logs", "monitoring"):
                if key in svc:
                    existing.add(key)
        important = {"s3", "ecr.dkr", "dynamodb", "sts"}
        missing = important - existing
        if not missing:
            return Insight(category=CATEGORY_NETWORKING, title="VPC Endpoints for key services",
                           value="All present", status=STATUS_OK,
                           detail="VPC Endpoints found for S3, ECR, DynamoDB, STS. NAT traffic for these services is minimized.",
                           context={"existing": list(existing), "missing": []})
        missing_str = ", ".join(sorted(missing)).upper().replace("ECR.DKR", "ECR").replace("DYNAMODB", "DynamoDB")
        return Insight(
            category=CATEGORY_NETWORKING,
            title="VPC Endpoints missing for key services",
            value=f"{len(missing)} missing: {missing_str}",
            status=STATUS_WARNING if len(missing) >= 2 else STATUS_INFO,
            detail=f"Missing VPC Endpoints: {missing_str}. Traffic to these services routes through NAT Gateway at $0.045/GB.",
            recommendation=f"Create Gateway/Interface VPC Endpoints for: {missing_str}. Gateway endpoints (S3, DynamoDB) are free.",
            affected_count=len(missing),
            context={"existing_endpoints": list(existing), "missing": list(missing)},
        )
    except Exception as e:
        logger.warning(f"check_vpc_endpoints_missing failed: {e}")
        return Insight(category=CATEGORY_NETWORKING, title="VPC Endpoints", value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def check_data_transfer_cost(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """Data transfer (egress) cost this month."""
    try:
        ce = _client("ce", aws, ls, "us-east-1")
        end = datetime.utcnow().date()
        start = end.replace(day=1)
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": str(start), "End": str(end)},
            Granularity="MONTHLY", Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "USAGE_TYPE_GROUP", "Values": ["EC2: Data Transfer - Internet (Out)"]}}
        )
        egress = sum(float(r["Total"]["UnblendedCost"]["Amount"]) for r in resp.get("ResultsByTime", []))
        status = STATUS_WARNING if egress > 200 else STATUS_INFO if egress > 50 else STATUS_OK
        return Insight(
            category=CATEGORY_NETWORKING,
            title="Data transfer egress cost (month-to-date)",
            value=_fmt(egress) + "/mo",
            status=status,
            detail=f"Internet egress traffic costs {_fmt(egress)} this month. AWS charges $0.09/GB out to internet.",
            recommendation="Use CloudFront as egress point ($0.0085/GB), use S3 Transfer Acceleration for large objects, or VPC Endpoints to eliminate intra-AWS traffic.",
            savings_usd=round(egress * 0.3, 2),
            context={"egress_usd": round(egress, 2)},
        )
    except Exception as e:
        logger.warning(f"check_data_transfer_cost failed: {e}")
        return Insight(category=CATEGORY_NETWORKING, title="Data transfer egress", value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def check_savings_plans_coverage(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """Savings Plans utilization and coverage."""
    try:
        ce = _client("ce", aws, ls, "us-east-1")
        end = datetime.utcnow().date()
        start = end - timedelta(days=30)
        resp = ce.get_savings_plans_utilization(
            TimePeriod={"Start": str(start), "End": str(end)}
        )
        totals = resp.get("Total", {})
        util_pct = float(totals.get("UtilizationPercentage", 0))
        net_savings = float(totals.get("NetSavings", 0))
        on_demand_cost = float(totals.get("OnDemandCostEquivalent", 0))

        # Get coverage
        cov_resp = ce.get_savings_plans_coverage(
            TimePeriod={"Start": str(start), "End": str(end)}
        )
        cov_totals = cov_resp.get("Total", {})
        cov_pct = float(cov_totals.get("CoveragePercentage", 0))
        uncovered_cost = float(cov_totals.get("OnDemandCost", 0))

        # Potential savings: if we committed to 80% coverage
        potential_savings = round(uncovered_cost * 0.30, 2) if uncovered_cost > 100 else 0

        status = STATUS_CRITICAL if cov_pct < 30 else STATUS_WARNING if cov_pct < 60 else STATUS_OK
        return Insight(
            category=CATEGORY_COMMITMENTS,
            title="Savings Plans coverage",
            value=f"{cov_pct:.0f}% covered, {_fmt(net_savings)} saved/mo",
            status=status,
            detail=f"Savings Plans cover {cov_pct:.0f}% of eligible compute. "
                   f"Utilization: {util_pct:.0f}%. Uncovered on-demand: {_fmt(uncovered_cost)}/mo.",
            recommendation=f"Purchase Compute Savings Plan to cover more on-demand. At 80% coverage, estimated additional savings: {_fmt(potential_savings)}/mo.",
            savings_usd=potential_savings,
            context={"coverage_pct": round(cov_pct, 1), "utilization_pct": round(util_pct, 1),
                     "net_savings": round(net_savings, 2), "uncovered_usd": round(uncovered_cost, 2)},
        )
    except Exception as e:
        logger.warning(f"check_savings_plans_coverage failed: {e}")
        return Insight(category=CATEGORY_COMMITMENTS, title="Savings Plans coverage", value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def check_ri_recommendations(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """Reserved Instance purchase recommendations for RDS and ElastiCache."""
    try:
        ce = _client("ce", aws, ls, "us-east-1")
        total_savings = 0.0
        rec_count = 0
        for svc in ["Amazon Relational Database Service", "Amazon ElastiCache"]:
            try:
                resp = ce.get_reservation_purchase_recommendation(
                    Service=svc, LookbackPeriodInDays="SIXTY_DAYS",
                    TermInYears="ONE_YEAR", PaymentOption="NO_UPFRONT"
                )
                for rec_group in resp.get("Recommendations", []):
                    for rec in rec_group.get("RecommendationDetails", []):
                        est = rec.get("EstimatedMonthlySavingsAmount")
                        if est:
                            total_savings += float(est)
                            rec_count += 1
            except Exception:
                pass
        if rec_count == 0:
            return Insight(category=CATEGORY_COMMITMENTS, title="RI recommendations (RDS + ElastiCache)",
                           value="No recommendations", status=STATUS_OK,
                           detail="No Reserved Instance purchase recommendations found. You may already have good RI coverage.",
                           context={"recommendations": 0})
        status = STATUS_WARNING if total_savings > 100 else STATUS_INFO
        return Insight(
            category=CATEGORY_COMMITMENTS,
            title="RI recommendations (RDS + ElastiCache)",
            value=f"{rec_count} RIs → {_fmt(total_savings)}/mo savings",
            status=status,
            detail=f"AWS recommends {rec_count} Reserved Instance purchase(s) for RDS/ElastiCache. "
                   f"1-year No-Upfront saves {_fmt(total_savings)}/mo vs on-demand.",
            recommendation="Purchase 1-year No-Upfront RIs for steady-state RDS and ElastiCache instances.",
            savings_usd=round(total_savings, 2),
            affected_count=rec_count,
            context={"rec_count": rec_count, "monthly_savings": round(total_savings, 2)},
        )
    except Exception as e:
        logger.warning(f"check_ri_recommendations failed: {e}")
        return Insight(category=CATEGORY_COMMITMENTS, title="RI recommendations", value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def check_ondemand_vs_committed(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """Percentage of compute running on-demand vs committed (RI + SP)."""
    try:
        ce = _client("ce", aws, ls, "us-east-1")
        end = datetime.utcnow().date()
        start = end - timedelta(days=30)
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": str(start), "End": str(end)},
            Granularity="MONTHLY", Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "PURCHASE_TYPE"}]
        )
        by_type: Dict[str, float] = {}
        for r in resp.get("ResultsByTime", []):
            for g in r.get("Groups", []):
                pt = g["Keys"][0]
                by_type[pt] = by_type.get(pt, 0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
        on_demand = by_type.get("On Demand", 0)
        reserved = by_type.get("Reserved", 0)
        spot = by_type.get("Spot", 0)
        sp = by_type.get("Savings Plans", 0)  # note: SPs show up in their own service
        committed = reserved + sp
        total = on_demand + committed + spot
        od_pct = round(on_demand / total * 100) if total else 100
        committed_pct = round(committed / total * 100) if total else 0
        status = STATUS_CRITICAL if od_pct > 80 else STATUS_WARNING if od_pct > 50 else STATUS_OK
        return Insight(
            category=CATEGORY_COMMITMENTS,
            title="On-demand vs committed compute",
            value=f"{od_pct}% on-demand, {committed_pct}% committed",
            status=status,
            detail=f"On-demand: {_fmt(on_demand)}/mo ({od_pct}%). Reserved/SP: {_fmt(committed)}/mo ({committed_pct}%). "
                   f"Spot: {_fmt(spot)}/mo. High on-demand % = paying full price with no discount.",
            recommendation="Target >60% commitment coverage. Purchase Compute Savings Plans for flexible EC2/Fargate coverage.",
            context={"on_demand_usd": round(on_demand, 2), "committed_usd": round(committed, 2),
                     "spot_usd": round(spot, 2), "od_pct": od_pct, "committed_pct": committed_pct},
        )
    except Exception as e:
        logger.warning(f"check_ondemand_vs_committed failed: {e}")
        return Insight(category=CATEGORY_COMMITMENTS, title="On-demand vs committed", value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def check_ec2_rightsizing(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """EC2 rightsizing recommendations from Cost Explorer."""
    try:
        ce = _client("ce", aws, ls, "us-east-1")
        resp = ce.get_rightsizing_recommendation(Service="AmazonEC2",
                                                  Configuration={"RecommendationTarget": "CROSS_INSTANCE_FAMILY",
                                                                  "BenefitsConsidered": True})
        recs = resp.get("RightsizingRecommendations", [])
        total_savings = sum(
            float(r.get("ModifyRecommendationDetail", r.get("TerminateRecommendationDetail", {}))
                  .get("EstimatedMonthlySavings", 0))
            for r in recs
        )
        if not recs:
            return Insight(category=CATEGORY_COMPUTE, title="EC2 rightsizing opportunities",
                           value="None found", status=STATUS_OK,
                           detail="No EC2 rightsizing recommendations. Your instances appear right-sized.",
                           context={"recommendations": 0})
        status = STATUS_WARNING if total_savings > 200 else STATUS_INFO
        return Insight(
            category=CATEGORY_COMPUTE,
            title="EC2 rightsizing opportunities",
            value=f"{len(recs)} instances → {_fmt(total_savings)}/mo savings",
            status=status,
            detail=f"Cost Explorer found {len(recs)} EC2 instance(s) to downsize or terminate. "
                   f"Estimated savings: {_fmt(total_savings)}/mo.",
            recommendation="Review and downsize instances running <20% CPU over 14 days. Test in staging first.",
            savings_usd=round(total_savings, 2),
            affected_count=len(recs),
            context={"rec_count": len(recs), "monthly_savings": round(total_savings, 2)},
        )
    except Exception as e:
        logger.warning(f"check_ec2_rightsizing failed: {e}")
        return Insight(category=CATEGORY_COMPUTE, title="EC2 rightsizing", value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def check_spot_coverage(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """Percentage of EC2 fleet using Spot instances."""
    try:
        region = aws.region or "us-east-1"
        ec2 = _client("ec2", aws, ls, region)
        resp = ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running"]}])
        total = 0
        spot_count = 0
        for r in resp.get("Reservations", []):
            for i in r.get("Instances", []):
                total += 1
                if i.get("InstanceLifecycle") == "spot":
                    spot_count += 1
        spot_pct = round(spot_count / total * 100) if total else 0
        status = STATUS_WARNING if spot_pct < 10 and total > 5 else STATUS_INFO
        return Insight(
            category=CATEGORY_COMPUTE,
            title="Spot instance coverage",
            value=f"{spot_pct}% Spot ({spot_count}/{total} instances)",
            status=status,
            detail=f"{spot_count} of {total} running EC2 instances use Spot pricing. "
                   f"Spot is 60-90% cheaper than on-demand for fault-tolerant workloads.",
            recommendation="Use Spot for batch jobs, CI/CD workers, and stateless services. Karpenter handles Spot gracefully in EKS.",
            affected_count=total - spot_count,
            context={"total_instances": total, "spot_count": spot_count, "spot_pct": spot_pct},
        )
    except Exception as e:
        logger.warning(f"check_spot_coverage failed: {e}")
        return Insight(category=CATEGORY_COMPUTE, title="Spot instance coverage", value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def check_gp2_to_gp3(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """EBS gp2 volumes that could be migrated to gp3 (20% cheaper, same IOPS)."""
    try:
        regions = aws.scan_regions or [aws.region or "us-east-1"]
        gp2_vols = []
        for reg in regions:
            ec2 = _client("ec2", aws, ls, reg)
            paginator = ec2.get_paginator("describe_volumes")
            for page in paginator.paginate(Filters=[{"Name": "volume-type", "Values": ["gp2"]}]):
                for v in page.get("Volumes", []):
                    gp2_vols.append({"id": v["VolumeId"], "size_gb": v["Size"], "region": reg})
        total_gb = sum(v["size_gb"] for v in gp2_vols)
        # gp2: $0.10/GB-month, gp3: $0.08/GB-month → $0.02/GB savings
        monthly_savings = round(total_gb * 0.02, 2)
        if not gp2_vols:
            return Insight(category=CATEGORY_COMPUTE, title="gp2 → gp3 EBS migration",
                           value="No gp2 volumes", status=STATUS_OK,
                           detail="All EBS volumes already use gp3 or other volume types.",
                           context={"gp2_count": 0})
        status = STATUS_WARNING if monthly_savings > 50 else STATUS_INFO
        return Insight(
            category=CATEGORY_COMPUTE,
            title="gp2 → gp3 EBS migration",
            value=f"{len(gp2_vols)} volumes, {total_gb:,} GB → {_fmt(monthly_savings)}/mo savings",
            status=status,
            detail=f"{len(gp2_vols)} EBS gp2 volumes totaling {total_gb:,} GB. "
                   f"gp3 costs $0.08/GB vs $0.10/GB for gp2, same baseline IOPS (3,000). No downtime required.",
            recommendation="Modify volumes from gp2 to gp3 via AWS Console or CLI. Zero downtime, instant savings.",
            savings_usd=monthly_savings,
            affected_count=len(gp2_vols),
            context={"gp2_count": len(gp2_vols), "total_gb": total_gb, "monthly_savings": monthly_savings},
        )
    except Exception as e:
        logger.warning(f"check_gp2_to_gp3 failed: {e}")
        return Insight(category=CATEGORY_COMPUTE, title="gp2 → gp3 EBS migration", value="unavailable",
                       status=STATUS_INFO, detail=str(e))


def check_cloudwatch_logs_no_retention(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """CloudWatch Log Groups with no retention policy (logs stored forever)."""
    try:
        regions = aws.scan_regions or [aws.region or "us-east-1"]
        no_retention = []
        for reg in regions:
            logs = _client("logs", aws, ls, reg)
            paginator = logs.get_paginator("describe_log_groups")
            for page in paginator.paginate():
                for lg in page.get("logGroups", []):
                    if lg.get("retentionInDays") is None:
                        size_bytes = lg.get("storedBytes", 0)
                        no_retention.append({
                            "name": lg["logGroupName"],
                            "size_gb": round(size_bytes / 1e9, 2),
                            "region": reg,
                        })
        total_gb = sum(lg["size_gb"] for lg in no_retention)
        # CloudWatch Logs storage: $0.03/GB-month
        monthly_cost = round(total_gb * 0.03, 2)
        if not no_retention:
            return Insight(category=CATEGORY_OBSERVABILITY, title="CloudWatch Logs without retention",
                           value="All have retention", status=STATUS_OK,
                           detail="All log groups have retention policies set.",
                           context={"count": 0})
        status = STATUS_WARNING if len(no_retention) > 10 or monthly_cost > 20 else STATUS_INFO
        return Insight(
            category=CATEGORY_OBSERVABILITY,
            title="CloudWatch Logs without retention policy",
            value=f"{len(no_retention)} groups, {total_gb:.1f} GB ({_fmt(monthly_cost)}/mo)",
            status=status,
            detail=f"{len(no_retention)} log group(s) have no retention policy — logs accumulate forever at $0.03/GB/month. "
                   f"Current accumulated storage: {total_gb:.1f} GB.",
            recommendation="Set retention to 30-90 days for most log groups. Use 1 year for audit/compliance logs.",
            savings_usd=monthly_cost,
            affected_count=len(no_retention),
            context={"groups_count": len(no_retention), "total_gb": round(total_gb, 2), "monthly_cost": monthly_cost},
        )
    except Exception as e:
        logger.warning(f"check_cloudwatch_logs_no_retention failed: {e}")
        return Insight(category=CATEGORY_OBSERVABILITY, title="CloudWatch Logs without retention",
                       value="unavailable", status=STATUS_INFO, detail=str(e))


def check_cloudwatch_logs_top_groups(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """Top 5 CloudWatch Log Groups by stored bytes (biggest cost drivers)."""
    try:
        region = aws.region or "us-east-1"
        logs = _client("logs", aws, ls, region)
        groups = []
        paginator = logs.get_paginator("describe_log_groups")
        for page in paginator.paginate():
            for lg in page.get("logGroups", []):
                size = lg.get("storedBytes", 0)
                if size > 0:
                    groups.append({"name": lg["logGroupName"], "size_gb": round(size / 1e9, 3)})
        groups.sort(key=lambda x: -x["size_gb"])
        top5 = groups[:5]
        total_gb = sum(g["size_gb"] for g in groups)
        top5_gb = sum(g["size_gb"] for g in top5)
        monthly_top5 = round(top5_gb * 0.03, 2)
        detail_lines = " · ".join(
            f"{g['name'].split('/')[-1]} {g['size_gb']:.1f}GB" for g in top5
        )
        return Insight(
            category=CATEGORY_OBSERVABILITY,
            title="Top 5 CloudWatch Log Groups by size",
            value=f"{top5_gb:.1f} GB in top 5, {_fmt(monthly_top5)}/mo",
            status=STATUS_WARNING if top5_gb > 50 else STATUS_INFO,
            detail=f"Top 5 log groups: {detail_lines}. Total log storage: {total_gb:.1f} GB.",
            recommendation="Review top groups — /aws/lambda/ and /aws/eks/ are often oversized. Set aggressive retention on debug logs.",
            context={"top_groups": top5, "total_gb": round(total_gb, 2)},
        )
    except Exception as e:
        logger.warning(f"check_cloudwatch_logs_top_groups failed: {e}")
        return Insight(category=CATEGORY_OBSERVABILITY, title="Top CloudWatch Log Groups",
                       value="unavailable", status=STATUS_INFO, detail=str(e))


def check_s3_no_lifecycle(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """S3 buckets without lifecycle rules — objects never transition to cheaper storage."""
    try:
        s3 = _client("s3", aws, ls)
        buckets = s3.list_buckets().get("Buckets", [])
        no_lifecycle = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            def _check(bucket_name):
                try:
                    s3.get_bucket_lifecycle_configuration(Bucket=bucket_name)
                    return None
                except Exception:
                    return bucket_name
            futs = {ex.submit(_check, b["Name"]): b["Name"] for b in buckets}
            for fut in as_completed(futs):
                r = fut.result()
                if r:
                    no_lifecycle.append(r)
        if not no_lifecycle:
            return Insight(category=CATEGORY_STORAGE, title="S3 buckets without lifecycle rules",
                           value="All have lifecycle rules", status=STATUS_OK,
                           detail="All S3 buckets have lifecycle rules configured.",
                           context={"count": 0})
        status = STATUS_WARNING if len(no_lifecycle) > 5 else STATUS_INFO
        return Insight(
            category=CATEGORY_STORAGE,
            title="S3 buckets without lifecycle rules",
            value=f"{len(no_lifecycle)} of {len(buckets)} buckets",
            status=status,
            detail=f"{len(no_lifecycle)} bucket(s) have no lifecycle rules. Objects never move to S3-IA or Glacier, "
                   f"staying at $0.023/GB vs $0.0125/GB for S3-IA (45% cheaper).",
            recommendation="Add lifecycle rules to transition objects: 30d → S3-IA, 90d → Glacier-IR. Saves 45-75% on storage.",
            affected_count=len(no_lifecycle),
            context={"buckets_without_lifecycle": no_lifecycle[:20]},
        )
    except Exception as e:
        logger.warning(f"check_s3_no_lifecycle failed: {e}")
        return Insight(category=CATEGORY_STORAGE, title="S3 without lifecycle rules",
                       value="unavailable", status=STATUS_INFO, detail=str(e))


def check_rds_multiaz_nonprod(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """RDS Multi-AZ instances in non-prod environments (doubles the cost)."""
    try:
        regions = aws.scan_regions or [aws.region or "us-east-1"]
        suspects = []
        for reg in regions:
            rds = _client("rds", aws, ls, reg)
            resp = rds.describe_db_instances()
            for inst in resp.get("DBInstances", []):
                if not inst.get("MultiAZ"):
                    continue
                tags = {t["Key"].lower(): t["Value"].lower()
                        for t in inst.get("TagList", [])}
                env = tags.get("env", tags.get("environment", ""))
                if any(x in env for x in ("staging", "stage", "dev", "test", "preprod", "qa")):
                    suspects.append({
                        "id": inst["DBInstanceIdentifier"],
                        "class": inst.get("DBInstanceClass", ""),
                        "env": env,
                        "region": reg,
                    })
        if not suspects:
            return Insight(category=CATEGORY_STORAGE, title="RDS Multi-AZ in non-prod",
                           value="None found", status=STATUS_OK,
                           detail="No RDS instances with Multi-AZ enabled in staging/dev/preprod environments.",
                           context={"count": 0})
        est_savings = len(suspects) * 80  # rough estimate: ~$80/mo per instance saved
        status = STATUS_WARNING if suspects else STATUS_OK
        detail_list = ", ".join(f"{s['id']} ({s['env']})" for s in suspects[:5])
        return Insight(
            category=CATEGORY_STORAGE,
            title="RDS Multi-AZ enabled in non-prod",
            value=f"{len(suspects)} instances → {_fmt(est_savings)}/mo wasted",
            status=status,
            detail=f"Multi-AZ doubles RDS cost by running a standby replica. Found in non-prod: {detail_list}.",
            recommendation="Disable Multi-AZ on staging/dev/preprod RDS instances. Keep it only in production.",
            savings_usd=est_savings,
            affected_count=len(suspects),
            context={"instances": suspects},
        )
    except Exception as e:
        logger.warning(f"check_rds_multiaz_nonprod failed: {e}")
        return Insight(category=CATEGORY_STORAGE, title="RDS Multi-AZ non-prod",
                       value="unavailable", status=STATUS_INFO, detail=str(e))


def check_lambda_deprecated_runtimes(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """Lambda functions running on deprecated/EOL runtimes."""
    try:
        regions = aws.scan_regions or [aws.region or "us-east-1"]
        deprecated = []
        for reg in regions:
            lmb = _client("lambda", aws, ls, reg)
            paginator = lmb.get_paginator("list_functions")
            for page in paginator.paginate():
                for fn in page.get("Functions", []):
                    rt = fn.get("Runtime", "")
                    if rt in _DEPRECATED_RUNTIMES:
                        deprecated.append({"name": fn["FunctionName"], "runtime": rt, "region": reg})
        if not deprecated:
            return Insight(category=CATEGORY_COMPUTE, title="Lambda deprecated runtimes",
                           value="All up-to-date", status=STATUS_OK,
                           detail="All Lambda functions use supported runtimes.",
                           context={"count": 0})
        runtime_summary = {}
        for fn in deprecated:
            runtime_summary[fn["runtime"]] = runtime_summary.get(fn["runtime"], 0) + 1
        summary_str = ", ".join(f"{rt} ({cnt})" for rt, cnt in runtime_summary.items())
        status = STATUS_WARNING if len(deprecated) > 3 else STATUS_INFO
        return Insight(
            category=CATEGORY_COMPUTE,
            title="Lambda functions on deprecated runtimes",
            value=f"{len(deprecated)} functions: {summary_str}",
            status=status,
            detail=f"{len(deprecated)} Lambda function(s) use deprecated runtimes: {summary_str}. "
                   f"AWS will block updates and eventually deprecate invocations.",
            recommendation="Migrate to current runtimes: nodejs20.x, python3.12, java21. AWS provides migration guides.",
            affected_count=len(deprecated),
            context={"deprecated_functions": deprecated[:20], "by_runtime": runtime_summary},
        )
    except Exception as e:
        logger.warning(f"check_lambda_deprecated_runtimes failed: {e}")
        return Insight(category=CATEGORY_COMPUTE, title="Lambda deprecated runtimes",
                       value="unavailable", status=STATUS_INFO, detail=str(e))


def check_ebs_snapshot_cost(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """Total EBS snapshot storage cost across regions."""
    try:
        regions = aws.scan_regions or [aws.region or "us-east-1"]
        total_gb = 0
        snap_count = 0
        old_snaps = 0  # >90 days
        cutoff = datetime.utcnow() - timedelta(days=90)
        for reg in regions:
            ec2 = _client("ec2", aws, ls, reg)
            resp = ec2.describe_snapshots(OwnerIds=["self"])
            for s in resp.get("Snapshots", []):
                gb = s.get("VolumeSize", 0)
                total_gb += gb
                snap_count += 1
                start = s.get("StartTime")
                if start and start.replace(tzinfo=None) < cutoff:
                    old_snaps += 1
        # EBS snapshot: $0.05/GB-month
        monthly_cost = round(total_gb * 0.05, 2)
        status = STATUS_WARNING if monthly_cost > 100 else STATUS_INFO if monthly_cost > 20 else STATUS_OK
        return Insight(
            category=CATEGORY_STORAGE,
            title="EBS snapshot storage cost",
            value=f"{snap_count} snapshots, {total_gb:,} GB → {_fmt(monthly_cost)}/mo",
            status=status,
            detail=f"{snap_count} EBS snapshots totaling {total_gb:,} GB at $0.05/GB/month. "
                   f"{old_snaps} snapshots are older than 90 days.",
            recommendation="Delete manual snapshots older than 90 days unless needed for compliance. Use Data Lifecycle Manager for automated retention.",
            affected_count=old_snaps,
            context={"snap_count": snap_count, "total_gb": total_gb,
                     "monthly_cost": monthly_cost, "old_snaps_90d": old_snaps},
        )
    except Exception as e:
        logger.warning(f"check_ebs_snapshot_cost failed: {e}")
        return Insight(category=CATEGORY_STORAGE, title="EBS snapshot cost",
                       value="unavailable", status=STATUS_INFO, detail=str(e))


def check_ebs_provisioned_iops_detached(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """io1/io2 volumes that are detached — paying full IOPS cost with zero utilization."""
    try:
        regions = aws.scan_regions or [aws.region or "us-east-1"]
        detached: List[Dict] = []
        for reg in regions:
            ec2 = _client("ec2", aws, ls, reg)
            paginator = ec2.get_paginator("describe_volumes")
            for page in paginator.paginate(
                Filters=[
                    {"Name": "volume-type", "Values": ["io1", "io2"]},
                    {"Name": "status",      "Values": ["available"]},  # available = not attached
                ]
            ):
                for v in page.get("Volumes", []):
                    iops = v.get("Iops", 0)
                    size_gb = v.get("Size", 0)
                    vtype = v.get("VolumeType", "io1")
                    # io1/io2: $0.065/provisioned-IOPS-month + $0.125/GB-month
                    iops_cost = round(iops * 0.065, 2)
                    storage_cost = round(size_gb * 0.125, 2)
                    total_cost = iops_cost + storage_cost
                    tags = {t["Key"]: t["Value"] for t in v.get("Tags", [])}
                    detached.append({
                        "id": v["VolumeId"],
                        "type": vtype,
                        "size_gb": size_gb,
                        "iops": iops,
                        "monthly_cost": total_cost,
                        "region": reg,
                        "name": tags.get("Name", v["VolumeId"]),
                    })

        if not detached:
            return Insight(
                category=CATEGORY_STORAGE,
                title="Detached io1/io2 volumes (provisioned IOPS waste)",
                value="None found", status=STATUS_OK,
                detail="No detached io1/io2 volumes found. Good — provisioned IOPS are only paid when needed.",
                context={"count": 0},
            )

        total_cost = sum(v["monthly_cost"] for v in detached)
        total_iops = sum(v["iops"] for v in detached)
        status = STATUS_CRITICAL if total_cost > 100 else STATUS_WARNING
        top = sorted(detached, key=lambda x: -x["monthly_cost"])
        detail_lines = " · ".join(
            f"{v['name']} ({v['type']}, {v['iops']:,} IOPS) {_fmt(v['monthly_cost'])}/mo"
            for v in top[:5]
        )
        return Insight(
            category=CATEGORY_STORAGE,
            title="Detached io1/io2 volumes — paying IOPS for nothing",
            value=f"{len(detached)} volumes, {total_iops:,} IOPS → {_fmt(total_cost)}/mo wasted",
            status=status,
            detail=f"{len(detached)} io1/io2 volume(s) are detached but still billing at $0.065/IOPS/month. "
                   f"Top offenders: {detail_lines}.",
            recommendation="Delete unused io1/io2 volumes (take a final snapshot first). "
                           "Or downgrade to gp3 while detached — gp3 costs $0.08/GB with 3,000 free IOPS.",
            savings_usd=round(total_cost, 2),
            affected_count=len(detached),
            context={"volumes": top[:20], "total_monthly_cost": round(total_cost, 2), "total_iops": total_iops},
        )
    except Exception as e:
        logger.warning(f"check_ebs_provisioned_iops_detached failed: {e}")
        return Insight(category=CATEGORY_STORAGE,
                       title="Detached io1/io2 volumes (provisioned IOPS waste)",
                       value="unavailable", status=STATUS_INFO, detail=str(e))


def check_ebs_overprovisioned_iops(aws: AWSConfig, ls: LocalStackConfig) -> Insight:
    """io1/io2 volumes where actual CloudWatch IOPS usage < 30% of provisioned (attached but idle)."""
    try:
        regions = aws.scan_regions or [aws.region or "us-east-1"]
        overprovisioned: List[Dict] = []
        cw_end = datetime.utcnow()
        cw_start = cw_end - timedelta(days=7)

        for reg in regions:
            ec2 = _client("ec2", aws, ls, reg)
            cw = _client("cloudwatch", aws, ls, reg)

            paginator = ec2.get_paginator("describe_volumes")
            for page in paginator.paginate(
                Filters=[
                    {"Name": "volume-type", "Values": ["io1", "io2"]},
                    {"Name": "status",      "Values": ["in-use"]},
                ]
            ):
                for v in page.get("Volumes", []):
                    vol_id = v["VolumeId"]
                    provisioned_iops = v.get("Iops", 0)
                    if provisioned_iops < 100:
                        continue  # skip tiny volumes

                    try:
                        # Get average IOPS = (ReadOps + WriteOps) / period_seconds
                        read_resp = cw.get_metric_statistics(
                            Namespace="AWS/EBS", MetricName="VolumeReadOps",
                            Dimensions=[{"Name": "VolumeId", "Value": vol_id}],
                            StartTime=cw_start, EndTime=cw_end,
                            Period=int((cw_end - cw_start).total_seconds()),
                            Statistics=["Sum"],
                        )
                        write_resp = cw.get_metric_statistics(
                            Namespace="AWS/EBS", MetricName="VolumeWriteOps",
                            Dimensions=[{"Name": "VolumeId", "Value": vol_id}],
                            StartTime=cw_start, EndTime=cw_end,
                            Period=int((cw_end - cw_start).total_seconds()),
                            Statistics=["Sum"],
                        )
                        period_secs = (cw_end - cw_start).total_seconds()
                        read_ops  = read_resp["Datapoints"][0]["Sum"]  if read_resp["Datapoints"]  else 0
                        write_ops = write_resp["Datapoints"][0]["Sum"] if write_resp["Datapoints"] else 0
                        avg_iops = (read_ops + write_ops) / period_secs

                        utilization_pct = round(avg_iops / provisioned_iops * 100, 1) if provisioned_iops else 0
                        if utilization_pct < 30:
                            iops_cost = round(provisioned_iops * 0.065, 2)
                            size_gb = v.get("Size", 0)
                            storage_cost = round(size_gb * 0.125, 2)
                            total_cost = iops_cost + storage_cost
                            # Savings: downgrade to gp3 (3,000 free IOPS, $0.005/extra IOPS)
                            needed_iops = max(int(avg_iops * 2), 3000)  # 2x headroom, min gp3 baseline
                            gp3_iops_cost = max(0, needed_iops - 3000) * 0.005
                            gp3_storage_cost = size_gb * 0.08
                            gp3_total = gp3_iops_cost + gp3_storage_cost
                            saving = round(total_cost - gp3_total, 2)

                            tags = {t["Key"]: t["Value"] for t in v.get("Tags", [])}
                            overprovisioned.append({
                                "id": vol_id,
                                "type": v.get("VolumeType", "io1"),
                                "size_gb": size_gb,
                                "provisioned_iops": provisioned_iops,
                                "avg_iops_7d": round(avg_iops, 1),
                                "utilization_pct": utilization_pct,
                                "monthly_cost": total_cost,
                                "saving": saving,
                                "region": reg,
                                "name": tags.get("Name", vol_id),
                                "instance": v.get("Attachments", [{}])[0].get("InstanceId", "—"),
                            })
                    except Exception:
                        continue  # CW data unavailable, skip

        if not overprovisioned:
            return Insight(
                category=CATEGORY_STORAGE,
                title="io1/io2 IOPS over-provisioning",
                value="None detected", status=STATUS_OK,
                detail="All attached io1/io2 volumes are utilizing >30% of provisioned IOPS, or CloudWatch data is unavailable.",
                context={"count": 0},
            )

        total_saving = sum(v["saving"] for v in overprovisioned if v["saving"] > 0)
        total_iops_wasted = sum(v["provisioned_iops"] - int(v["avg_iops_7d"]) for v in overprovisioned)
        status = STATUS_CRITICAL if total_saving > 200 else STATUS_WARNING
        top = sorted(overprovisioned, key=lambda x: -x["saving"])
        detail_lines = " · ".join(
            f"{v['name']} {v['provisioned_iops']:,} IOPS provisioned / {v['avg_iops_7d']:.0f} used ({v['utilization_pct']}%)"
            for v in top[:4]
        )
        return Insight(
            category=CATEGORY_STORAGE,
            title="io1/io2 IOPS over-provisioned (actual usage < 30%)",
            value=f"{len(overprovisioned)} volumes, {total_iops_wasted:,} IOPS unused → {_fmt(total_saving)}/mo savings",
            status=status,
            detail=f"{len(overprovisioned)} io1/io2 volume(s) using <30% of provisioned IOPS over the last 7 days. "
                   f"Top: {detail_lines}.",
            recommendation="Downgrade to gp3: modify volume type in-place (zero downtime). "
                           "gp3 includes 3,000 IOPS free and charges only $0.005/extra IOPS vs $0.065 for io1/io2.",
            savings_usd=round(total_saving, 2),
            affected_count=len(overprovisioned),
            context={"volumes": top[:20], "total_saving": round(total_saving, 2), "total_iops_wasted": total_iops_wasted},
        )
    except Exception as e:
        logger.warning(f"check_ebs_overprovisioned_iops failed: {e}")
        return Insight(category=CATEGORY_STORAGE,
                       title="io1/io2 IOPS over-provisioning",
                       value="unavailable", status=STATUS_INFO, detail=str(e))


# ════════════════════════════════════════════════════════════════════════════
# RUNNER
# ════════════════════════════════════════════════════════════════════════════

ALL_CHECKS = [
    # Cost
    check_top_cost_drivers,
    check_cost_trend,
    check_cost_by_region,
    check_cost_anomalies,
    check_cost_by_env_tag,
    # Networking
    check_nat_gateway_cost,
    check_vpc_endpoints_missing,
    check_data_transfer_cost,
    # Commitments
    check_savings_plans_coverage,
    check_ri_recommendations,
    check_ondemand_vs_committed,
    # Compute
    check_ec2_rightsizing,
    check_spot_coverage,
    check_gp2_to_gp3,
    check_lambda_deprecated_runtimes,
    # Storage / DB
    check_s3_no_lifecycle,
    check_rds_multiaz_nonprod,
    check_ebs_snapshot_cost,
    check_ebs_provisioned_iops_detached,
    check_ebs_overprovisioned_iops,
    # Observability
    check_cloudwatch_logs_no_retention,
    check_cloudwatch_logs_top_groups,
]


def run_all_insights(aws: AWSConfig, ls: LocalStackConfig,
                     progress_cb=None) -> List[Insight]:
    """Run all checks in parallel and return results."""
    results: List[Insight] = []
    total = len(ALL_CHECKS)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fn, aws, ls): fn.__name__ for fn in ALL_CHECKS}
        done = 0
        for fut in as_completed(futs):
            name = futs[fut]
            done += 1
            try:
                insight = fut.result()
                results.append(insight)
                if progress_cb:
                    progress_cb(name, done, total)
            except Exception as e:
                logger.warning(f"Insight check {name} failed: {e}")

    # Sort: critical first, then warning, then by category
    order = {STATUS_CRITICAL: 0, STATUS_WARNING: 1, STATUS_INFO: 2, STATUS_OK: 3}
    results.sort(key=lambda x: (order.get(x.status, 9), x.category, x.title))
    return results
