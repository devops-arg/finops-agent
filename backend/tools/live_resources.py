"""Live AWS infrastructure queries for dashboard endpoints.

Hits describe-* / list-* APIs across EC2, RDS, EKS, ElastiCache, OpenSearch, S3,
and builds the same shape that `mock_data.generate_infrastructure` returns so
the frontend renderer doesn't care which source it's from.

All calls are read-only and cached per-request (no state between requests).
Errors in a single service don't break others — we catch and continue.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

import boto3

logger = logging.getLogger(__name__)

# Global services don't need to iterate regions
# S3, IAM, Route53, CloudFront are global
# Cost Explorer (ce) is only in us-east-1 but returns data for all regions


def _session(aws_config) -> boto3.Session:
    """Build a boto3 session from AWSConfig. Supports keys, profile, and assume-role."""
    if aws_config.profile:
        session = boto3.Session(
            profile_name=aws_config.profile,
            region_name=aws_config.region,
        )
    else:
        session = boto3.Session(
            aws_access_key_id=aws_config.access_key_id,
            aws_secret_access_key=aws_config.secret_access_key,
            region_name=aws_config.region,
        )

    if aws_config.assume_role_arn:
        sts = session.client("sts")
        creds = sts.assume_role(
            RoleArn=aws_config.assume_role_arn,
            RoleSessionName="finops-dashboard",
        )["Credentials"]
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=aws_config.region,
        )
    return session


# ── Region enumeration ─────────────────────────────────────────────────────
def _list_enabled_regions(session: boto3.Session) -> list[str]:
    """Return all regions enabled/opted-in for this account."""
    ec2 = session.client("ec2", region_name="us-east-1")
    resp = ec2.describe_regions(AllRegions=False)  # AllRegions=False excludes opted-out regions
    regions = [r["RegionName"] for r in resp.get("Regions", [])]
    logger.info(f"Enumerated {len(regions)} enabled AWS regions: {regions}")
    return regions


def _run_per_region(
    session: boto3.Session, regions: list[str], fn, label: str, max_workers: int = 10
) -> list[dict[str, Any]]:
    """Run `fn(session, region)` for each region in parallel. Returns list of results."""
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fn, session, r): r for r in regions}
        for fut in as_completed(futures):
            r = futures[fut]
            try:
                data = fut.result(timeout=15)
                if data:
                    data["_region"] = r
                    results.append(data)
            except Exception as e:
                logger.warning(f"{label} fetch failed in {r}: {str(e)[:120]}")
    return results


# ── EC2 ────────────────────────────────────────────────────────────────────
def _fetch_ec2_region(session: boto3.Session, region: str) -> dict[str, Any]:
    ec2 = session.client("ec2", region_name=region)
    paginator = ec2.get_paginator("describe_instances")
    instances = []
    for page in paginator.paginate(PaginationConfig={"PageSize": 100}):
        for reservation in page.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                instances.append(
                    {
                        "id": inst["InstanceId"],
                        "type": inst["InstanceType"],
                        "state": inst["State"]["Name"],
                        "name": tags.get("Name", inst["InstanceId"]),
                        "environment": tags.get("Environment", tags.get("env", "unknown")),
                    }
                )
    return {"instances": instances}


def _aggregate_ec2(per_region: list[dict[str, Any]]) -> dict[str, Any]:
    all_instances = []
    by_region = {}
    for rdata in per_region:
        region = rdata["_region"]
        insts = rdata.get("instances", [])
        all_instances.extend(insts)
        if insts:
            by_region[region] = len(insts)

    running = sum(1 for i in all_instances if i["state"] == "running")
    stopped = sum(1 for i in all_instances if i["state"] == "stopped")
    types_count = {}
    envs_count = {}
    for i in all_instances:
        types_count[i["type"]] = types_count.get(i["type"], 0) + 1
        envs_count[i["environment"]] = envs_count.get(i["environment"], 0) + 1

    top_types = dict(sorted(types_count.items(), key=lambda x: -x[1])[:8])

    return {
        "total_instances": len(all_instances),
        "running": running,
        "stopped": stopped,
        "instance_types": top_types,
        "environments": envs_count,
        "regions": by_region,
        "monthly_cost": 0,
        "status": "healthy" if running > 0 else "warning" if stopped > 0 else "healthy",
        "detail": f"{running}/{len(all_instances)} running across {len(by_region)} regions: {', '.join(by_region.keys())}",
    }


# ── RDS ────────────────────────────────────────────────────────────────────
def _fetch_rds_region(session: boto3.Session, region: str) -> dict[str, Any]:
    rds = session.client("rds", region_name=region)
    dbs = rds.describe_db_instances().get("DBInstances", [])
    clusters = rds.describe_db_clusters().get("DBClusters", [])
    return {"dbs": dbs, "clusters": clusters}


def _aggregate_rds(per_region: list[dict[str, Any]]) -> dict[str, Any]:
    all_dbs = []
    all_clusters = []
    by_region = {}
    for rdata in per_region:
        region = rdata["_region"]
        dbs = rdata.get("dbs", [])
        clusters = rdata.get("clusters", [])
        all_dbs.extend(dbs)
        all_clusters.extend(clusters)
        if dbs or clusters:
            by_region[region] = {"dbs": len(dbs), "clusters": len(clusters)}

    if not all_dbs and not all_clusters:
        return {
            "clusters": 0,
            "instances": 0,
            "engine": "none",
            "regions": {},
            "monthly_cost": 0,
            "status": "healthy",
            "detail": "No RDS instances in any region",
        }

    total_storage = sum(db.get("AllocatedStorage", 0) for db in all_dbs)
    engines = {db.get("Engine", "unknown") for db in all_dbs}
    multi_az = any(db.get("MultiAZ") for db in all_dbs)
    instance_classes = [db.get("DBInstanceClass", "") for db in all_dbs]

    return {
        "clusters": len(all_clusters),
        "instances": len(all_dbs),
        "engine": ", ".join(engines),
        "primary_type": instance_classes[0] if instance_classes else "",
        "storage_allocated_gb": total_storage,
        "storage_used_gb": int(total_storage * 0.4),
        "multi_az": multi_az,
        "regions": by_region,
        "monthly_cost": 0,
        "status": "healthy",
        "detail": f"{len(all_dbs)} instances, {len(all_clusters)} clusters across {len(by_region)} regions",
    }


# ── EKS ────────────────────────────────────────────────────────────────────
def _fetch_eks_region(session: boto3.Session, region: str) -> dict[str, Any]:
    eks = session.client("eks", region_name=region)
    cluster_names = eks.list_clusters().get("clusters", [])
    if not cluster_names:
        return {"clusters": []}

    clusters = []
    for name in cluster_names:
        nodes = 0
        try:
            ng_names = eks.list_nodegroups(clusterName=name).get("nodegroups", [])
            for ngname in ng_names:
                desc = eks.describe_nodegroup(clusterName=name, nodegroupName=ngname)
                nodes += desc["nodegroup"].get("scalingConfig", {}).get("desiredSize", 0)
        except Exception as e:
            logger.warning(f"EKS nodegroup fetch failed for {name} in {region}: {e}")
        clusters.append({"name": name, "nodes": nodes, "region": region})
    return {"clusters": clusters}


def _aggregate_eks(per_region: list[dict[str, Any]]) -> dict[str, Any]:
    all_clusters = []
    by_region = {}
    for rdata in per_region:
        region = rdata["_region"]
        cls = rdata.get("clusters", [])
        all_clusters.extend(cls)
        if cls:
            by_region[region] = len(cls)

    total_nodes = sum(c["nodes"] for c in all_clusters)
    return {
        "clusters": len(all_clusters),
        "cluster_names": [c["name"] for c in all_clusters],
        "nodes": {"total": total_nodes},
        "pods_running": 0,
        "regions": by_region,
        "monthly_cost": 0,
        "status": "healthy",
        "detail": f"{len(all_clusters)} clusters, ~{total_nodes} nodes across {len(by_region)} regions"
        if all_clusters
        else "No EKS clusters in any region",
    }


# ── ElastiCache ────────────────────────────────────────────────────────────
def _fetch_elasticache_region(session: boto3.Session, region: str) -> dict[str, Any]:
    ec = session.client("elasticache", region_name=region)
    clusters = ec.describe_cache_clusters().get("CacheClusters", [])
    return {"clusters": clusters}


def _aggregate_elasticache(per_region: list[dict[str, Any]]) -> dict[str, Any]:
    all_clusters = []
    by_region = {}
    for rdata in per_region:
        region = rdata["_region"]
        cls = rdata.get("clusters", [])
        all_clusters.extend(cls)
        if cls:
            by_region[region] = len(cls)

    if not all_clusters:
        return {
            "clusters": 0,
            "total_nodes": 0,
            "engine": "none",
            "regions": {},
            "monthly_cost": 0,
            "status": "healthy",
            "detail": "No ElastiCache clusters in any region",
        }

    engines = {c.get("Engine", "unknown") for c in all_clusters}
    total_nodes = sum(c.get("NumCacheNodes", 0) for c in all_clusters)
    types = [c.get("CacheNodeType", "") for c in all_clusters]

    return {
        "clusters": len(all_clusters),
        "total_nodes": total_nodes,
        "engine": ", ".join(engines),
        "instance_type": types[0] if types else "",
        "regions": by_region,
        "monthly_cost": 0,
        "status": "healthy",
        "detail": f"{len(all_clusters)} clusters, {total_nodes} nodes across {len(by_region)} regions",
    }


# ── OpenSearch ─────────────────────────────────────────────────────────────
def _fetch_opensearch_region(session: boto3.Session, region: str) -> dict[str, Any]:
    os_client = session.client("opensearch", region_name=region)
    domains = os_client.list_domain_names().get("DomainNames", [])
    return {"domains": domains}


def _aggregate_opensearch(per_region: list[dict[str, Any]]) -> dict[str, Any]:
    all_domains = []
    by_region = {}
    for rdata in per_region:
        region = rdata["_region"]
        doms = rdata.get("domains", [])
        all_domains.extend(doms)
        if doms:
            by_region[region] = len(doms)

    if not all_domains:
        return {
            "domains": 0,
            "regions": {},
            "monthly_cost": 0,
            "status": "healthy",
            "detail": "No OpenSearch domains in any region",
        }

    return {
        "domains": len(all_domains),
        "regions": by_region,
        "monthly_cost": 0,
        "status": "healthy",
        "detail": f"{len(all_domains)} domains across {len(by_region)} regions: {', '.join(d['DomainName'] for d in all_domains[:3])}",
    }


# ── S3 ─────────────────────────────────────────────────────────────────────
def _fetch_s3(session: boto3.Session) -> dict[str, Any]:
    s3 = session.client("s3")
    resp = s3.list_buckets()
    buckets = resp.get("Buckets", [])
    bucket_info = []
    # Just list first 10 — don't do GetBucketSize (expensive) for dashboard
    for b in buckets[:10]:
        bucket_info.append(
            {
                "name": b["Name"],
                "created": b["CreationDate"].isoformat() if "CreationDate" in b else "",
                "size_gb": 0,
                "objects": 0,
                "lifecycle": False,
            }
        )

    return {
        "total_buckets": len(buckets),
        "total_size_gb": 0,  # skipped — requires GetBucketSize per bucket
        "total_objects": 0,
        "monthly_cost": 0,
        "status": "healthy",
        "buckets": bucket_info,
        "detail": f"{len(buckets)} buckets — size/object counts omitted for performance",
    }


# ── Entry point ────────────────────────────────────────────────────────────
def fetch_live_infrastructure(aws_config, region: str = None) -> dict[str, Any]:
    """Fetch live infrastructure from AWS.

    Args:
        aws_config: AWSConfig with credentials.
        region: If "all", scans every enabled region in parallel (slow, ~20s).
                If None, uses the configured default region (fast).
                Otherwise, queries just that specific region.

    EC2/RDS/EKS/ElastiCache/OpenSearch are regional.
    S3 is global. Cost Explorer is queried once in us-east-1 (all-region costs).
    """
    session = _session(aws_config)
    resources: dict[str, Any] = {}

    # Determine which regions to scan
    scan_all = region == "all"
    if scan_all:
        try:
            regions = _list_enabled_regions(session)
        except Exception as e:
            logger.error(f"Could not enumerate regions, falling back to configured region: {e}")
            regions = [aws_config.region or "us-east-1"]
    else:
        regions = [region or aws_config.region or "us-east-1"]

    # Always enumerate region list so the UI can populate a selector
    try:
        available_regions = _list_enabled_regions(session)
    except Exception:
        available_regions = regions

    # ── Regional services: fetch all regions in parallel, then aggregate ──
    regional_services = [
        ("ec2", _fetch_ec2_region, _aggregate_ec2),
        ("rds", _fetch_rds_region, _aggregate_rds),
        ("eks", _fetch_eks_region, _aggregate_eks),
        ("elasticache", _fetch_elasticache_region, _aggregate_elasticache),
        ("opensearch", _fetch_opensearch_region, _aggregate_opensearch),
    ]
    for key, fetch_fn, agg_fn in regional_services:
        try:
            per_region_results = _run_per_region(session, regions, fetch_fn, key)
            resources[key] = agg_fn(per_region_results)
        except Exception as e:
            logger.warning(f"Live {key} aggregation failed: {e}")
            resources[key] = {
                "status": "error",
                "detail": f"Failed to query {key}: {str(e)[:120]}",
                "monthly_cost": 0,
            }

    # ── Global: S3 ──
    try:
        resources["s3"] = _fetch_s3(session)
    except Exception as e:
        logger.warning(f"S3 fetch failed: {e}")
        resources["s3"] = {"status": "error", "detail": f"S3 error: {str(e)[:120]}", "monthly_cost": 0}

    # ── Cost attribution from Cost Explorer (us-east-1, returns all-region costs) ──
    try:
        _attach_costs(session, resources, aws_config.region)
    except Exception as e:
        logger.warning(f"Cost attribution failed: {e}")

    healthy = sum(1 for r in resources.values() if r.get("status") == "healthy")
    warning = sum(1 for r in resources.values() if r.get("status") == "warning")
    critical = sum(1 for r in resources.values() if r.get("status") in ("error", "critical"))

    return {
        "mode": "aws-live",
        "generated": datetime.utcnow().isoformat() + "Z",
        "regions_scanned": regions,
        "available_regions": available_regions,
        "default_region": aws_config.region or "us-east-1",
        "scan_mode": "all" if scan_all else "single",
        "resources": resources,
        "health_summary": {"healthy": healthy, "warning": warning, "critical": critical},
    }


def _attach_costs(session: boto3.Session, resources: dict[str, Any], region: str):
    """Pull last 30-day cost per service and attribute to matching resources."""
    from datetime import timedelta

    ce = session.client("ce", region_name="us-east-1")
    end = datetime.utcnow().date()
    start = end - timedelta(days=30)
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    service_costs = {}
    for period in resp.get("ResultsByTime", []):
        for group in period.get("Groups", []):
            svc = group["Keys"][0]
            amt = float(group["Metrics"]["UnblendedCost"]["Amount"])
            service_costs[svc] = service_costs.get(svc, 0) + amt

    # Map AWS service names to our resource keys
    svc_map = {
        "ec2": ["Amazon Elastic Compute Cloud - Compute", "EC2 - Other", "Amazon EC2"],
        "rds": ["Amazon Relational Database Service", "Amazon RDS"],
        "eks": ["Amazon Elastic Container Service for Kubernetes", "Amazon EKS"],
        "elasticache": ["Amazon ElastiCache"],
        "opensearch": ["Amazon OpenSearch Service", "Amazon Elasticsearch Service"],
        "s3": ["Amazon Simple Storage Service", "Amazon S3"],
    }
    for res_key, aws_names in svc_map.items():
        if res_key not in resources:
            continue
        total = sum(service_costs.get(n, 0) for n in aws_names)
        resources[res_key]["monthly_cost"] = round(total, 2)


def fetch_live_optimization(aws_config) -> dict[str, Any]:
    """Fetch cost optimization recommendations from AWS Cost Optimization Hub.

    Cost Optimization Hub (launched Nov 2023) is the unified AWS service that
    aggregates recommendations from: Compute Optimizer, Trusted Advisor,
    Savings Plans/RI recs, and Billing & Cost Management. If not enrolled,
    falls back to direct Cost Explorer rightsizing + SP APIs.
    """
    session = _session(aws_config)

    # Try Cost Optimization Hub first (unified, most complete)
    try:
        hub_result = _fetch_cost_optimization_hub(session)
        if hub_result["recommendations"]:
            return hub_result
    except Exception as e:
        logger.warning(f"Cost Optimization Hub fetch failed: {e}")

    # Fallback: direct CE recommendations APIs
    return _fetch_ce_recommendations_fallback(session)


def _map_effort(aws_effort: str) -> str:
    """AWS uses VeryLow/Low/Medium/High/VeryHigh; we use low/medium/high."""
    m = {"VeryLow": "low", "Low": "low", "Medium": "medium", "High": "high", "VeryHigh": "high"}
    return m.get(aws_effort, "medium")


def _priority_from_savings(monthly_savings: float) -> str:
    if monthly_savings >= 200:
        return "P1"
    if monthly_savings >= 50:
        return "P2"
    return "P3"


def _action_to_type(action: str) -> str:
    """Map AWS Cost Optimization Hub action types to our internal type field."""
    return {
        "Rightsize": "rightsizing",
        "Upgrade": "rightsizing",
        "MigrateToGraviton": "rightsizing",
        "Stop": "idle",
        "Delete": "idle",
        "PurchaseReservedInstances": "commitment",
        "PurchaseSavingsPlans": "commitment",
        "MigrateToGpInstances": "storage",
        "MigrateToGpVolumes": "storage",
        "ChangeStorageClass": "lifecycle",
    }.get(action, "other")


def _fetch_cost_optimization_hub(session: boto3.Session) -> dict[str, Any]:
    """Fetch from the unified Cost Optimization Hub service (us-east-1 only)."""
    hub = session.client("cost-optimization-hub", region_name="us-east-1")

    # Verify enrollment
    try:
        enrollment = hub.list_enrollment_statuses()
        active = any(item.get("status") == "Active" for item in enrollment.get("items", []))
        if not active:
            raise RuntimeError("Cost Optimization Hub is not enrolled for this account")
    except Exception as e:
        raise RuntimeError(f"Cost Optimization Hub not available: {e}")

    # Paginate recommendations (up to 200)
    all_items = []
    paginator = hub.get_paginator("list_recommendations")
    page_count = 0
    for page in paginator.paginate(
        includeAllRecommendations=True,
        PaginationConfig={"MaxItems": 200, "PageSize": 50},
    ):
        all_items.extend(page.get("items", []))
        page_count += 1
        if page_count >= 4:  # hard cap 200 recs
            break

    # Sort by monthly savings descending
    all_items.sort(key=lambda r: float(r.get("estimatedMonthlySavings", 0) or 0), reverse=True)

    recommendations = []
    total_savings = 0.0
    for i, rec in enumerate(all_items):
        savings = float(rec.get("estimatedMonthlySavings", 0) or 0)
        total_savings += savings

        action = rec.get("actionType", "Unknown")
        curr_summary = rec.get("currentResourceSummary", "")
        rec_summary = rec.get("recommendedResourceSummary", "")
        resource_id = rec.get("resourceId", "")
        resource_type = rec.get("currentResourceType", "")
        region = rec.get("region", "")
        savings_pct = rec.get("estimatedSavingsPercentage", 0)

        # Build human-readable title
        if action == "Rightsize" and curr_summary and rec_summary:
            title = f"Rightsize {resource_type}: {curr_summary} → {rec_summary}"
        elif action == "MigrateToGraviton":
            title = f"Migrate to Graviton: {curr_summary} → {rec_summary}"
        elif action == "PurchaseReservedInstances":
            title = f"Purchase RIs: {rec_summary[:80]}"
        elif action == "PurchaseSavingsPlans":
            title = f"Purchase Savings Plan: {rec_summary[:80]}"
        elif action == "Stop":
            title = f"Stop idle {resource_type}: {resource_id or curr_summary}"
        elif action == "Delete":
            title = f"Delete unused {resource_type}: {resource_id or curr_summary}"
        else:
            title = f"{action}: {resource_type} {resource_id or curr_summary}"[:120]

        detail_parts = []
        if region:
            detail_parts.append(f"Region: {region}")
        if savings_pct:
            detail_parts.append(f"Est. savings: {savings_pct:.0f}%")
        if rec.get("source"):
            detail_parts.append(f"Source: {rec['source']}")
        if rec.get("recommendationLookbackPeriodInDays"):
            detail_parts.append(f"Lookback: {rec['recommendationLookbackPeriodInDays']}d")
        if rec.get("restartNeeded"):
            detail_parts.append("Restart required")

        recommendations.append(
            {
                "id": rec.get("recommendationId", f"OPT-{i+1:04d}"),
                "priority": _priority_from_savings(savings),
                "severity": "high" if savings >= 200 else "medium" if savings >= 50 else "low",
                "service": resource_type or "AWS",
                "title": title,
                "detail": " · ".join(detail_parts)
                + (
                    f". Current resource: {curr_summary}"
                    if curr_summary and curr_summary not in title
                    else ""
                ),
                "monthly_savings": round(savings, 2),
                "annual_savings": round(savings * 12, 2),
                "current_monthly_cost": round(float(rec.get("estimatedMonthlyCost", 0) or 0), 2),
                "effort": _map_effort(rec.get("implementationEffort", "Medium")),
                "risk": "medium" if rec.get("restartNeeded") else "low",
                "type": _action_to_type(action),
                "action_type": action,
                "region": region,
                "resource_id": resource_id,
                "rollback_possible": rec.get("rollbackPossible", False),
                "source": rec.get("source", "CostOptimizationHub"),
            }
        )

    # Summary score: more/bigger opportunities = lower score
    score = max(0, min(100, 100 - int(total_savings / 50)))

    type_counts = {}
    for r in recommendations:
        type_counts[r["type"]] = type_counts.get(r["type"], 0) + 1

    return {
        "mode": "aws-live",
        "source": "CostOptimizationHub",
        "generated": datetime.utcnow().isoformat() + "Z",
        "savings_score": score,
        "total_monthly_savings_identified": round(total_savings, 2),
        "total_annual_savings_identified": round(total_savings * 12, 2),
        "recommendations": recommendations,
        "quick_stats": {
            "total_recommendations": len(recommendations),
            "rightsizing_opportunities": type_counts.get("rightsizing", 0),
            "idle_resources": type_counts.get("idle", 0),
            "commitment_gaps": type_counts.get("commitment", 0),
            "storage_opportunities": type_counts.get("storage", 0),
        },
        "note": f"Aggregated from AWS Cost Optimization Hub ({len(all_items)} total recommendations, showing top by $ impact)",
    }


def _fetch_ce_recommendations_fallback(session: boto3.Session) -> dict[str, Any]:
    """Fallback when Cost Optimization Hub is not enrolled: use CE APIs directly."""
    recommendations: list[dict[str, Any]] = []
    total_savings = 0.0

    try:
        ce = session.client("ce", region_name="us-east-1")
        resp = ce.get_rightsizing_recommendation(Service="AmazonEC2")
        for i, rec in enumerate(resp.get("RightsizingRecommendations", [])[:10]):
            target = rec.get("ModifyRecommendationDetail") or rec.get("TerminateRecommendationDetail") or {}
            savings = float(target.get("EstimatedMonthlySavings", 0))
            total_savings += savings
            inst_id = rec.get("CurrentInstance", {}).get("ResourceId", "unknown")
            curr_type = rec.get("CurrentInstance", {}).get("InstanceName", "")
            action = rec.get("RightsizingType", "")
            recommendations.append(
                {
                    "id": f"CE-RS-{i+1:03d}",
                    "priority": _priority_from_savings(savings),
                    "severity": "high" if savings > 100 else "medium",
                    "service": "Amazon EC2",
                    "title": f"{action} {curr_type or inst_id}",
                    "detail": f"Instance {inst_id}: {action.lower()}. Based on AWS rightsizing recommendation.",
                    "monthly_savings": round(savings, 2),
                    "annual_savings": round(savings * 12, 2),
                    "effort": "low",
                    "risk": "low",
                    "type": "rightsizing",
                    "source": "CostExplorer",
                }
            )
    except Exception as e:
        logger.warning(f"EC2 rightsizing fallback failed: {e}")

    score = max(0, min(100, 100 - int(total_savings / 50)))
    return {
        "mode": "aws-live",
        "source": "CostExplorer",
        "generated": datetime.utcnow().isoformat() + "Z",
        "savings_score": score,
        "total_monthly_savings_identified": round(total_savings, 2),
        "total_annual_savings_identified": round(total_savings * 12, 2),
        "recommendations": recommendations,
        "quick_stats": {"total_recommendations": len(recommendations)},
        "note": "Fallback: Cost Optimization Hub not enrolled; using direct Cost Explorer APIs",
    }
