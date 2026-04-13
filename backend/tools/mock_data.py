"""Realistic AWS cost mock data generator for demo / mock mode.

Simulates a FICTIONAL Series B LatAm fintech called "Ribbon" running on
EKS + RDS + ElastiCache + OpenSearch, ~$28,000/month in AWS spend across
3 regions (us-east-1 primary, eu-west-1 DR, ap-southeast-1 edge).

ALL DATES ARE RELATIVE TO `datetime.utcnow()` — no hardcoded dates.
This means running the demo in June returns June dates automatically.

Deterministic (same seed → same output) so the narrative is stable across
page refreshes within a day.
"""

import math
from datetime import datetime, timedelta
from typing import Any, Dict, List


# ── Fictional client profile ─────────────────────────────────────────────────
CLIENT_NAME = "Ribbon (demo)"
CLIENT_SPEND_CLASS = "Series B LatAm fintech, ~$28K/mo AWS"

# ── Service cost profile (weekly baseline in USD) ────────────────────────────
# Totals to roughly $6,500/week = $28,145/month
SERVICES = [
    {"name": "Amazon EC2",                   "weekly": 2240,  "color": "#f59e0b"},
    {"name": "Amazon RDS",                   "weekly": 1180,  "color": "#06b6d4"},
    {"name": "Amazon ElastiCache",           "weekly": 620,   "color": "#22c55e"},
    {"name": "Amazon OpenSearch Service",    "weekly": 480,   "color": "#f97316"},
    {"name": "Amazon S3",                    "weekly": 410,   "color": "#8b5cf6"},
    {"name": "AWS Data Transfer",            "weekly": 380,   "color": "#94a3b8"},
    {"name": "Amazon EKS",                   "weekly": 180,   "color": "#ec4899"},
    {"name": "Amazon CloudWatch",            "weekly": 340,   "color": "#64748b"},
    {"name": "Amazon MSK (Kafka)",           "weekly": 280,   "color": "#a78bfa"},
    {"name": "AWS Lambda",                   "weekly": 190,   "color": "#3b82f6"},
    {"name": "Amazon Route 53",              "weekly": 28,    "color": "#334155"},
    {"name": "AWS Secrets Manager",          "weekly": 24,    "color": "#475569"},
    {"name": "AWS WAF",                      "weekly": 148,   "color": "#ef4444"},
]

ENVIRONMENTS = [
    {"name": "production", "pct": 0.72},
    {"name": "staging",    "pct": 0.18},
    {"name": "dev",        "pct": 0.10},
]

ACCOUNTS = [
    {"id": "111122223333", "name": "ribbon-prod",     "pct": 0.72},
    {"id": "444455556666", "name": "ribbon-staging",  "pct": 0.18},
    {"id": "777788889999", "name": "ribbon-sandbox",  "pct": 0.10},
]

REGIONS = [
    {"name": "us-east-1",      "pct": 0.62, "label": "N. Virginia"},
    {"name": "eu-west-1",      "pct": 0.22, "label": "Ireland (DR)"},
    {"name": "ap-southeast-1", "pct": 0.11, "label": "Singapore"},
    {"name": "us-west-2",      "pct": 0.04, "label": "Oregon"},
    {"name": "global",         "pct": 0.01, "label": "Route 53/CF"},
]

TEAMS = [
    {"name": "payments",   "pct": 0.38},
    {"name": "platform",   "pct": 0.26},
    {"name": "data",       "pct": 0.19},
    {"name": "growth",     "pct": 0.12},
    {"name": "shared",     "pct": 0.05},
]

# Usage types (for NAT, data transfer, EBS questions)
USAGE_TYPES_WEEKLY = [
    # NAT Gateway (critical for questions about cross-AZ + NAT)
    {"type": "NatGateway-Bytes",                         "weekly": 612, "category": "networking", "service": "VPC"},
    {"type": "NatGateway-Hours",                         "weekly": 72,  "category": "networking", "service": "VPC"},
    # Data transfer
    {"type": "DataTransfer-Out-Bytes (Internet)",        "weekly": 184, "category": "transfer",   "service": "AWS Data Transfer"},
    {"type": "DataTransfer-Regional-Bytes (cross-AZ)",   "weekly": 142, "category": "transfer",   "service": "AWS Data Transfer"},
    {"type": "USW2-DataTransfer-Regional-Bytes",         "weekly": 54,  "category": "transfer",   "service": "AWS Data Transfer"},  # inter-region
    # EBS
    {"type": "EBS:VolumeUsage.gp2",                      "weekly": 287, "category": "storage",    "service": "Amazon EC2"},  # migration candidate
    {"type": "EBS:VolumeUsage.gp3",                      "weekly": 412, "category": "storage",    "service": "Amazon EC2"},
    {"type": "EBS:SnapshotUsage",                        "weekly": 156, "category": "storage",    "service": "Amazon EC2"},
    {"type": "EBS:VolumeUsage.io1",                      "weekly": 92,  "category": "storage",    "service": "Amazon EC2"},
    # Compute breakdown
    {"type": "BoxUsage:m5.xlarge (on-demand)",           "weekly": 682, "category": "compute",    "service": "Amazon EC2"},
    {"type": "SpotUsage:m5.xlarge",                      "weekly": 198, "category": "compute",    "service": "Amazon EC2"},
    {"type": "BoxUsage:c6i.2xlarge (on-demand)",         "weekly": 412, "category": "compute",    "service": "Amazon EC2"},
    {"type": "SpotUsage:c6i.2xlarge",                    "weekly": 145, "category": "compute",    "service": "Amazon EC2"},
    # RDS
    {"type": "InstanceUsage:db.r5.2xlarge",              "weekly": 520, "category": "database",   "service": "Amazon RDS"},
    {"type": "InstanceUsage:db.r5.xlarge (replica)",     "weekly": 260, "category": "database",   "service": "Amazon RDS"},
    {"type": "RDS:StorageUsage",                         "weekly": 240, "category": "database",   "service": "Amazon RDS"},
    {"type": "RDS:BackupUsage",                          "weekly": 160, "category": "database",   "service": "Amazon RDS"},
    # S3 & CloudWatch
    {"type": "S3:StandardStorage",                       "weekly": 240, "category": "storage",    "service": "Amazon S3"},
    {"type": "S3:Requests-Tier1",                        "weekly": 62,  "category": "storage",    "service": "Amazon S3"},
    {"type": "CW:DataProcessing-Bytes (Logs ingestion)", "weekly": 218, "category": "observability", "service": "Amazon CloudWatch"},
    {"type": "CW:LogsStorage",                           "weekly": 72,  "category": "observability", "service": "Amazon CloudWatch"},
    {"type": "CW:Metrics",                               "weekly": 50,  "category": "observability", "service": "Amazon CloudWatch"},
]

# Commitment coverage profile
COMMITMENT_COVERAGE = {
    "savings_plans_pct": 18,          # 18% of eligible compute covered
    "reserved_instances_pct": 22,     # 22% of RDS/ElastiCache
    "spot_pct": 28,                   # 28% of EC2 on Spot
    "on_demand_pct": 32,              # 32% pure on-demand (optimization target)
    "savings_plans_monthly_potential": 1850,
    "ri_monthly_potential": 720,
}

WEEKLY_TOTAL_BASE = sum(s["weekly"] for s in SERVICES)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _week_multiplier(weeks_ago: int) -> float:
    """Growth trend: ~0.8%/week, with a spike 3 weeks ago."""
    base = 1.0 - (weeks_ago * 0.008)
    if weeks_ago == 3:
        return base * 1.24  # medium anomaly week
    return base


def _service_multiplier(service_name: str, weeks_ago: int) -> float:
    """Per-service variance for realism."""
    if service_name == "Amazon RDS" and weeks_ago == 3:
        return 2.1  # RDS storage anomaly
    if service_name == "AWS Data Transfer" and weeks_ago == 3:
        return 1.6
    if service_name == "Amazon CloudWatch" and weeks_ago == 2:
        return 1.35  # log ingestion spike from a deploy
    if service_name == "AWS Lambda" and weeks_ago == 1:
        return 1.22  # cold start burst
    return 1.0


def _iso_date(d) -> str:
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


# ── Time-series generators (all date-relative) ───────────────────────────────
def generate_weekly_trend(today=None, num_weeks=12) -> List[Dict]:
    today = today or datetime.utcnow().date()
    anchor = today - timedelta(days=today.weekday())  # current Monday
    weeks = []
    for i in range(num_weeks - 1, -1, -1):
        start = anchor - timedelta(weeks=i)
        end = start + timedelta(days=6)
        cost = WEEKLY_TOTAL_BASE * _week_multiplier(i)
        weeks.append({
            "start": _iso_date(start),
            "end": _iso_date(end),
            "label": start.strftime("%d/%m"),
            "cost": round(cost, 2),
            "weeks_ago": i,
        })
    return weeks


def generate_by_service(today=None, num_weeks=4) -> List[Dict]:
    today = today or datetime.utcnow().date()
    anchor = today - timedelta(days=today.weekday())
    result = []
    for svc in SERVICES:
        costs = {}
        for i in range(num_weeks - 1, -1, -1):
            start = anchor - timedelta(weeks=i)
            label = start.strftime("%d/%m")
            costs[label] = round(svc["weekly"] * _week_multiplier(i) * _service_multiplier(svc["name"], i), 2)
        result.append({
            "name": svc["name"],
            "color": svc["color"],
            "costs": costs,
            "monthly_estimate": round(svc["weekly"] * 4.33, 2),
        })
    return result


def generate_by_environment(today=None, num_weeks=4) -> List[Dict]:
    today = today or datetime.utcnow().date()
    anchor = today - timedelta(days=today.weekday())
    result = []
    for env in ENVIRONMENTS:
        costs = {}
        for i in range(num_weeks - 1, -1, -1):
            start = anchor - timedelta(weeks=i)
            label = start.strftime("%d/%m")
            costs[label] = round(WEEKLY_TOTAL_BASE * env["pct"] * _week_multiplier(i), 2)
        result.append({"name": env["name"], "costs": costs})
    return result


def generate_by_account(today=None, num_weeks=4) -> List[Dict]:
    today = today or datetime.utcnow().date()
    anchor = today - timedelta(days=today.weekday())
    result = []
    for acct in ACCOUNTS:
        costs = {}
        for i in range(num_weeks - 1, -1, -1):
            start = anchor - timedelta(weeks=i)
            label = start.strftime("%d/%m")
            costs[label] = round(WEEKLY_TOTAL_BASE * acct["pct"] * _week_multiplier(i), 2)
        result.append({"id": acct["id"], "name": acct["name"], "costs": costs})
    return result


def generate_by_region(today=None, num_weeks=4) -> List[Dict]:
    today = today or datetime.utcnow().date()
    anchor = today - timedelta(days=today.weekday())
    result = []
    for r in REGIONS:
        costs = {}
        for i in range(num_weeks - 1, -1, -1):
            start = anchor - timedelta(weeks=i)
            label = start.strftime("%d/%m")
            costs[label] = round(WEEKLY_TOTAL_BASE * r["pct"] * _week_multiplier(i), 2)
        result.append({"name": r["name"], "label": r["label"], "costs": costs})
    return result


def generate_by_team(today=None, num_weeks=4) -> List[Dict]:
    today = today or datetime.utcnow().date()
    anchor = today - timedelta(days=today.weekday())
    result = []
    for t in TEAMS:
        costs = {}
        for i in range(num_weeks - 1, -1, -1):
            start = anchor - timedelta(weeks=i)
            label = start.strftime("%d/%m")
            costs[label] = round(WEEKLY_TOTAL_BASE * t["pct"] * _week_multiplier(i), 2)
        result.append({"name": t["name"], "costs": costs})
    return result


def generate_usage_types(today=None) -> List[Dict]:
    """Detailed usage-type breakdown — covers NAT, data transfer, EBS, etc."""
    today = today or datetime.utcnow().date()
    return [
        {
            "type": ut["type"],
            "service": ut["service"],
            "category": ut["category"],
            "weekly_cost": round(ut["weekly"], 2),
            "monthly_cost": round(ut["weekly"] * 4.33, 2),
        }
        for ut in USAGE_TYPES_WEEKLY
    ]


def generate_anomalies(today=None) -> List[Dict]:
    """Multiple anomalies across different services — date-relative."""
    today = today or datetime.utcnow().date()
    anchor = today - timedelta(days=today.weekday())

    def week_bounds(weeks_ago):
        start = anchor - timedelta(weeks=weeks_ago)
        end = start + timedelta(days=6)
        return _iso_date(start), _iso_date(end)

    s3, e3 = week_bounds(3)
    s2, e2 = week_bounds(2)
    s1, e1 = week_bounds(1)

    return [
        {
            "id": "ANO-0001",
            "service": "Amazon RDS",
            "start": s3,
            "end": e3,
            "expected_cost": 1180.0,
            "actual_cost": 2478.0,
            "impact": 1298.0,
            "impact_pct": 110,
            "root_cause": "Automated snapshots of a retired read-replica were not cleaned up (42 snapshots × 180GB) combined with cross-region backup accidentally enabled.",
            "status": "resolved",
            "resolution": "Deleted orphaned snapshots, disabled cross-region backup, added lifecycle policy. Fixed in +5 days.",
        },
        {
            "id": "ANO-0002",
            "service": "AWS Data Transfer",
            "start": s3,
            "end": e3,
            "expected_cost": 380.0,
            "actual_cost": 612.0,
            "impact": 232.0,
            "impact_pct": 61,
            "root_cause": "A new microservice was deployed that calls S3 across regions (us-east-1 → eu-west-1) on every request instead of using the local replica.",
            "status": "investigating",
            "resolution": "PR #4421 open to route requests to local region S3 bucket.",
        },
        {
            "id": "ANO-0003",
            "service": "Amazon CloudWatch",
            "start": s2,
            "end": e2,
            "expected_cost": 340.0,
            "actual_cost": 498.0,
            "impact": 158.0,
            "impact_pct": 46,
            "root_cause": "A staging deploy accidentally enabled DEBUG log level in prod payment-service. 3.2 TB of extra logs ingested.",
            "status": "resolved",
            "resolution": "Reverted log level, added pre-commit hook to block DEBUG in production configs.",
        },
        {
            "id": "ANO-0004",
            "service": "AWS Lambda",
            "start": s1,
            "end": e1,
            "expected_cost": 190.0,
            "actual_cost": 248.0,
            "impact": 58.0,
            "impact_pct": 31,
            "root_cause": "Cold starts increased after a Python 3.11 → 3.12 upgrade on the webhook handler. Init duration doubled.",
            "status": "monitoring",
            "resolution": "Rollback considered; evaluating provisioned concurrency for peak hours.",
        },
    ]


def generate_daily_trend(today=None, days=30) -> List[Dict]:
    today = today or datetime.utcnow().date()
    result = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        is_weekend = day.weekday() >= 5
        day_factor = 0.78 if is_weekend else 1.06
        wave = 1 + 0.03 * math.sin(i * 0.7)
        daily_cost = (WEEKLY_TOTAL_BASE / 7) * day_factor * wave
        # Add extra RDS snapshot cost during anomaly window (~21-25 days ago)
        if 21 <= i <= 25:
            daily_cost += 185
        result.append({
            "date": _iso_date(day),
            "cost": round(daily_cost, 2),
            "is_weekend": is_weekend,
        })
    return result


# ── Master report (aggregates everything) ────────────────────────────────────
def generate_report(today=None, num_weeks=4) -> Dict[str, Any]:
    today = today or datetime.utcnow().date()

    weekly_trend_full = generate_weekly_trend(today, 12)
    weekly_trend_report = weekly_trend_full[-num_weeks:]
    by_service = generate_by_service(today, num_weeks)
    by_env = generate_by_environment(today, num_weeks)
    by_account = generate_by_account(today, num_weeks)
    by_region = generate_by_region(today, num_weeks)
    by_team = generate_by_team(today, num_weeks)

    last_week_cost = weekly_trend_report[-1]["cost"]
    prev_week_cost = weekly_trend_report[-2]["cost"] if len(weekly_trend_report) >= 2 else 0
    weekly_change = round(((last_week_cost - prev_week_cost) / prev_week_cost * 100) if prev_week_cost else 0, 1)
    four_week_total = sum(w["cost"] for w in weekly_trend_report)
    four_week_avg = round(four_week_total / len(weekly_trend_report), 2)
    monthly_projection = round(four_week_avg * 4.33, 2)

    month_start = today.replace(day=1)
    days_in_month = (today - month_start).days + 1
    mtd_cost = round((WEEKLY_TOTAL_BASE / 7) * days_in_month * 1.02, 2)

    return {
        "generated": datetime.utcnow().isoformat() + "Z",
        "mode": "mock",
        "account_name": CLIENT_NAME,
        "client_profile": CLIENT_SPEND_CLASS,
        "weeks": [w["label"] for w in weekly_trend_report],
        "weeklyTrend": [{"week": w["label"], "cost": w["cost"]} for w in weekly_trend_report],
        "weeklyTrendFull": [{"week": w["label"], "cost": w["cost"]} for w in weekly_trend_full],
        "dailyTrend": generate_daily_trend(today),
        "byService": by_service,
        "byEnvironment": by_env,
        "byAccount": by_account,
        "byRegion": by_region,
        "byTeam": by_team,
        "usageTypes": generate_usage_types(today),
        "anomalies": generate_anomalies(today),
        "commitmentCoverage": COMMITMENT_COVERAGE,
        "summary": {
            "lastWeekCost": last_week_cost,
            "previousWeekCost": prev_week_cost,
            "weeklyChange": weekly_change,
            "fourWeekTotal": round(four_week_total, 2),
            "fourWeekAvg": four_week_avg,
            "monthlyProjection": monthly_projection,
            "mtdCost": mtd_cost,
            "topService": by_service[0]["name"] if by_service else "N/A",
            "topAccount": by_account[0]["name"] if by_account else "N/A",
            "topRegion": by_region[0]["name"] if by_region else "N/A",
            "topTeam": by_team[0]["name"] if by_team else "N/A",
            "activeAccounts": len(by_account),
            "activeServices": len([s for s in by_service if s["monthly_estimate"] > 5]),
            "activeRegions": len(by_region),
            "anomalyCount": len(generate_anomalies(today)),
        },
    }


# ── Infrastructure snapshot ──────────────────────────────────────────────────
def generate_infrastructure() -> Dict[str, Any]:
    return {
        "mode": "mock",
        "generated": datetime.utcnow().isoformat() + "Z",
        "account_name": CLIENT_NAME,
        "resources": {
            "ec2": {
                "total_instances": 48,
                "running": 42,
                "stopped": 6,
                "instance_types": {"m5.xlarge": 14, "c6i.2xlarge": 10, "m5.large": 8, "t3.large": 6, "r5.xlarge": 4},
                "environments": {"production": 32, "staging": 10, "dev": 6},
                "regions": {"us-east-1": 32, "eu-west-1": 12, "ap-southeast-1": 4},
                "avg_cpu_pct": 22.8,
                "avg_memory_pct": 48.4,
                "spot_coverage_pct": 28,
                "graviton_coverage_pct": 12,
                "monthly_cost": 9700,
                "status": "warning",
                "warning": "22.8% avg CPU — oversizing. Compute Optimizer flagged 14 rightsizing candidates.",
                "detail": "42 running across 3 regions. Karpenter managing 18 Spot nodes.",
            },
            "rds": {
                "clusters": 3,
                "instances": 8,
                "engine": "PostgreSQL 15.4, Aurora PG 15.3",
                "primary_type": "db.r5.2xlarge",
                "replica_type": "db.r5.xlarge",
                "connections_active": 1247,
                "connections_max": 2500,
                "storage_used_gb": 842,
                "storage_allocated_gb": 1200,
                "avg_cpu_pct": 18.2,
                "iops_used": 4200,
                "backup_retention_days": 14,
                "multi_az": True,
                "monthly_cost": 5110,
                "status": "warning",
                "warning": "Primary at 18% CPU — downsize candidate. Replica at 8% CPU.",
                "detail": "3 clusters across us-east-1 + eu-west-1. Multi-AZ enabled.",
            },
            "eks": {
                "clusters": 3,
                "cluster_names": ["ribbon-prod", "ribbon-staging", "ribbon-data"],
                "nodes": {"production": 18, "staging": 6, "data": 8},
                "pods_running": 284,
                "pods_pending": 0,
                "pods_failed": 2,
                "node_provisioner": "Karpenter v1.0",
                "avg_cpu_pct": 32.4,
                "avg_memory_pct": 52.1,
                "monthly_cost": 780,
                "status": "healthy",
                "detail": "Karpenter managing spot/on-demand mix, 28% spot coverage.",
            },
            "elasticache": {
                "clusters": 4,
                "total_nodes": 8,
                "engine": "Redis 7.0",
                "instance_type": "cache.r6g.large",
                "hit_rate_pct": 97.1,
                "memory_used_gb": 18.2,
                "memory_total_gb": 52.8,
                "connections": 3240,
                "evictions_per_sec": 0,
                "monthly_cost": 2680,
                "status": "healthy",
                "detail": "4 Redis clusters, 1 primary + 1 replica per env. Graviton (r6g).",
            },
            "opensearch": {
                "domains": 2,
                "nodes": 6,
                "instance_type": "r6g.xlarge.search",
                "engine_version": "OpenSearch 2.11",
                "shards": 124,
                "indices": 42,
                "storage_used_gb": 420,
                "storage_total_gb": 800,
                "avg_cpu_pct": 19.4,
                "monthly_cost": 2080,
                "status": "healthy",
                "detail": "2 domains: logs + application search. 6 nodes total, Graviton.",
            },
            "s3": {
                "total_buckets": 32,
                "total_size_gb": 18400,
                "total_objects": 24800000,
                "monthly_cost": 1780,
                "status": "warning",
                "warning": "6 buckets missing lifecycle policies. ~$340/mo savings from Intelligent-Tiering.",
                "buckets": [
                    {"name": "ribbon-prod-data",     "size_gb": 6800, "objects": 12000000, "lifecycle": True,  "storage_class": "Standard"},
                    {"name": "ribbon-prod-logs",     "size_gb": 4200, "objects": 8400000,  "lifecycle": False, "storage_class": "Standard"},
                    {"name": "ribbon-prod-backups",  "size_gb": 3800, "objects": 2400000,  "lifecycle": True,  "storage_class": "Glacier IR"},
                    {"name": "ribbon-prod-ml-models","size_gb": 1800, "objects": 120000,   "lifecycle": True,  "storage_class": "Intelligent-Tiering"},
                    {"name": "ribbon-staging-data",  "size_gb": 1200, "objects": 1600000,  "lifecycle": False, "storage_class": "Standard"},
                    {"name": "ribbon-cloudtrail",    "size_gb": 600,  "objects": 280000,   "lifecycle": True,  "storage_class": "Glacier"},
                ],
                "detail": "32 buckets, 18.4 TB total across 3 regions.",
            },
            "nat_gateway": {
                "total_gateways": 6,
                "monthly_cost": 1350,
                "data_processed_tb_month": 13.6,
                "hourly_charges_usd": 128,
                "data_processing_usd": 612,
                "status": "warning",
                "warning": "High NAT data charges — VPC endpoints for S3/ECR/STS could save ~$450/mo.",
                "detail": "2 NAT gateways per region × 3 regions. $0.045/GB data processing charges.",
            },
        },
        "health_summary": {"healthy": 3, "warning": 4, "critical": 0},
    }


# ── Optimization recommendations ─────────────────────────────────────────────
def generate_optimization() -> Dict[str, Any]:
    """Comprehensive recommendations covering all the main FinOps angles."""
    recommendations = [
        {
            "id": "OPT-001",
            "priority": "P1",
            "severity": "high",
            "service": "Amazon RDS",
            "title": "Downsize RDS primary: db.r5.2xlarge → db.r5.xlarge",
            "detail": "Primary RDS averaging 18% CPU and 42% memory over 30 days. db.r5.xlarge keeps memory/vCPU ratio but halves capacity. ~3 min downtime.",
            "monthly_savings": 520,
            "annual_savings": 6240,
            "effort": "low",
            "risk": "low",
            "type": "rightsizing",
            "implementation": "`aws rds modify-db-instance --db-instance-identifier ribbon-prod-primary --db-instance-class db.r5.xlarge --apply-immediately`",
        },
        {
            "id": "OPT-002",
            "priority": "P1",
            "severity": "high",
            "service": "AWS VPC",
            "title": "Add VPC endpoints for S3, ECR, STS, CloudWatch",
            "detail": "NAT Gateway processing $612/mo of traffic, much of which is AWS-internal (ECR pulls, S3 gets, STS). VPC endpoints eliminate $0.045/GB charges.",
            "monthly_savings": 450,
            "annual_savings": 5400,
            "effort": "low",
            "risk": "low",
            "type": "networking",
            "implementation": "Create Gateway endpoints for S3, DynamoDB (free). Interface endpoints for ECR, STS, CloudWatch ($7.30/mo each × 3 regions).",
        },
        {
            "id": "OPT-003",
            "priority": "P1",
            "severity": "high",
            "service": "Savings Plans",
            "title": "1-Year Compute Savings Plan for EKS baseline",
            "detail": "18 prod nodes running 24/7 at on-demand. 1-year Compute SP (No Upfront) saves ~37% vs on-demand for the baseline.",
            "monthly_savings": 820,
            "annual_savings": 9840,
            "effort": "low",
            "risk": "low",
            "type": "commitment",
            "implementation": "Commit $4.20/hr Compute SP in AWS Cost Explorer → Savings Plans.",
        },
        {
            "id": "OPT-004",
            "priority": "P2",
            "severity": "medium",
            "service": "Amazon EC2",
            "title": "Migrate c6i/m5 workloads to Graviton (c7g/m7g)",
            "detail": "14 EC2 instances run Go/Rust/Java workloads that are ARM-safe. Graviton r7g/m7g/c7g = 20% cheaper + better perf.",
            "monthly_savings": 320,
            "annual_savings": 3840,
            "effort": "medium",
            "risk": "low",
            "type": "rightsizing",
            "implementation": "Rebuild multi-arch Docker images. Test under load. Rollout per-NodePool in Karpenter.",
        },
        {
            "id": "OPT-005",
            "priority": "P2",
            "severity": "medium",
            "service": "Amazon EKS",
            "title": "Scale-to-zero staging cluster nights & weekends",
            "detail": "Staging cluster runs 6 nodes 24/7 but receives zero traffic ~65% of the time (nights + weekends). KEDA cron trigger can scale to zero.",
            "monthly_savings": 280,
            "annual_savings": 3360,
            "effort": "medium",
            "risk": "low",
            "type": "scheduling",
            "implementation": "Deploy KEDA ScaledObject with cron trigger (20:00 → 08:00 Mon-Fri scale to 0).",
        },
        {
            "id": "OPT-006",
            "priority": "P2",
            "severity": "medium",
            "service": "Amazon S3",
            "title": "S3 Intelligent-Tiering + lifecycle on 6 buckets",
            "detail": "6 buckets missing lifecycle policies (ribbon-prod-logs 4.2TB, ribbon-staging-data, etc.). Intelligent-Tiering auto-moves cold data → saves ~$340/mo.",
            "monthly_savings": 340,
            "annual_savings": 4080,
            "effort": "low",
            "risk": "low",
            "type": "lifecycle",
            "implementation": "Add S3 lifecycle rule: Intelligent-Tiering after 0 days, Glacier IR after 90, Deep Archive after 365.",
        },
        {
            "id": "OPT-007",
            "priority": "P2",
            "severity": "medium",
            "service": "Amazon EC2",
            "title": "Increase Spot usage in Karpenter NodePool (28% → 60%)",
            "detail": "Current Karpenter NodePool: 28% Spot, 72% On-Demand. Workers + batch workloads tolerate interruption. Increasing Spot to 60% saves ~$260/mo.",
            "monthly_savings": 260,
            "annual_savings": 3120,
            "effort": "low",
            "risk": "medium",
            "type": "spot",
            "implementation": "Update Karpenter NodePool spec: weight Spot higher, add m5a/m5n/m6i/m6a fallbacks.",
        },
        {
            "id": "OPT-008",
            "priority": "P2",
            "severity": "medium",
            "service": "Amazon EC2",
            "title": "Migrate EBS gp2 → gp3 volumes",
            "detail": "$287/wk on gp2 volumes. gp3 is 20% cheaper with better baseline perf (3000 IOPS included). ~$230/mo savings.",
            "monthly_savings": 230,
            "annual_savings": 2760,
            "effort": "low",
            "risk": "low",
            "type": "storage",
            "implementation": "`aws ec2 modify-volume --volume-id <id> --volume-type gp3`. Online migration, no downtime.",
        },
        {
            "id": "OPT-009",
            "priority": "P2",
            "severity": "medium",
            "service": "Amazon CloudWatch",
            "title": "CloudWatch Logs → Loki/S3 pipeline for non-critical logs",
            "detail": "$340/wk on CloudWatch ingestion. DEBUG + access logs don't need CW features. Loki + S3 → 80% cheaper for same volume.",
            "monthly_savings": 820,
            "annual_savings": 9840,
            "effort": "medium",
            "risk": "low",
            "type": "observability",
            "implementation": "Deploy Promtail on EKS pushing to Loki. Keep CW for ALB/WAF logs only.",
        },
        {
            "id": "OPT-010",
            "priority": "P3",
            "severity": "low",
            "service": "Amazon EC2",
            "title": "Delete 18 orphaned EBS volumes + old snapshots",
            "detail": "18 volumes unattached > 30 days. 340 snapshots older than retention policy.",
            "monthly_savings": 140,
            "annual_savings": 1680,
            "effort": "low",
            "risk": "low",
            "type": "cleanup",
            "implementation": "Script: `aws ec2 describe-volumes --filters Name=status,Values=available`. Snapshot list via DLM.",
        },
        {
            "id": "OPT-011",
            "priority": "P3",
            "severity": "low",
            "service": "Amazon ElastiCache",
            "title": "RI for ElastiCache: cache.r6g.large × 4 (1-year)",
            "detail": "4 ElastiCache nodes running 24/7 as baseline cache. 1-year RI saves 32% vs on-demand.",
            "monthly_savings": 180,
            "annual_savings": 2160,
            "effort": "low",
            "risk": "low",
            "type": "commitment",
            "implementation": "Purchase 4x cache.r6g.large 1-year Reserved Nodes in us-east-1.",
        },
        {
            "id": "OPT-012",
            "priority": "P3",
            "severity": "low",
            "service": "AWS Data Transfer",
            "title": "Reduce cross-AZ traffic with topology-aware routing",
            "detail": "$142/wk on cross-AZ regional transfer. K8s topology-aware routing keeps traffic in-AZ when possible.",
            "monthly_savings": 110,
            "annual_savings": 1320,
            "effort": "medium",
            "risk": "low",
            "type": "networking",
            "implementation": "Set `service.kubernetes.io/topology-aware-hints: auto` on services. Deploy multiple replicas per AZ.",
        },
    ]
    total_savings = sum(r["monthly_savings"] for r in recommendations)
    return {
        "mode": "mock",
        "generated": datetime.utcnow().isoformat() + "Z",
        "account_name": CLIENT_NAME,
        "savings_score": 62,
        "total_monthly_savings_identified": total_savings,
        "total_annual_savings_identified": total_savings * 12,
        "commitment_coverage": COMMITMENT_COVERAGE,
        "recommendations": recommendations,
        "quick_stats": {
            "total_recommendations": len(recommendations),
            "rightsizing_opportunities": sum(1 for r in recommendations if r["type"] == "rightsizing"),
            "idle_resources": sum(1 for r in recommendations if r["type"] == "cleanup"),
            "commitment_gaps": sum(1 for r in recommendations if r["type"] == "commitment"),
            "scheduling_opportunities": sum(1 for r in recommendations if r["type"] == "scheduling"),
            "lifecycle_opportunities": sum(1 for r in recommendations if r["type"] == "lifecycle"),
            "networking_opportunities": sum(1 for r in recommendations if r["type"] == "networking"),
            "storage_opportunities": sum(1 for r in recommendations if r["type"] == "storage"),
            "observability_opportunities": sum(1 for r in recommendations if r["type"] == "observability"),
        },
    }
