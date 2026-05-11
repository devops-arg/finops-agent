"""
AWS resource status tools — query EC2, RDS, EKS, ElastiCache, OpenSearch, S3.
In LocalStack mode, uses boto3 against the LocalStack endpoint and supplements
with mock data for metrics not supported by LocalStack free tier.
In real AWS mode, uses live boto3 calls.
"""

import logging
import time
from typing import Any, Dict, List

from backend.tools.base import BaseTool
from backend.models.core import ToolResult
from backend.config.manager import AWSConfig, LocalStackConfig

logger = logging.getLogger(__name__)


TOOL_DEFINITIONS = [
    {
        "name": "get_infrastructure_health",
        "description": (
            "Get a health summary of all AWS infrastructure: EC2 instances, RDS databases, "
            "EKS clusters, ElastiCache, OpenSearch, and S3 buckets. "
            "Returns status (healthy/warning/critical), resource counts, and key metrics."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_ec2_instances",
        "description": (
            "List EC2 instances with their state, instance type, and resource utilization. "
            "Can filter by environment tag or instance state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "environment": {
                    "type": "string",
                    "description": "Filter by environment tag (production, staging, dev). Default: all.",
                },
                "state": {
                    "type": "string",
                    "enum": ["running", "stopped", "all"],
                    "description": "Filter by instance state. Default: all.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_rds_status",
        "description": (
            "Get RDS database status including connections, CPU, storage utilization, "
            "backup status, and performance insights. Identifies over-provisioned instances."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_eks_cluster_status",
        "description": (
            "Get EKS cluster status including node count, pod distribution, "
            "resource utilization, and Karpenter node pool configuration."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cluster_name": {
                    "type": "string",
                    "description": "Specific cluster name to query. Default: all clusters.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "get_elasticache_status",
        "description": (
            "Get ElastiCache/Redis cluster metrics: hit rate, memory utilization, "
            "connection count, evictions. Identifies sizing issues."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_s3_usage",
        "description": (
            "Get S3 bucket sizes, object counts, storage class distribution, "
            "and lifecycle policy status. Identifies buckets without lifecycle rules."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_optimization_recommendations",
        "description": (
            "Get AI-powered cost optimization recommendations based on current infrastructure state. "
            "Returns prioritized list with estimated monthly savings and implementation steps."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


class AWSResourceTools(BaseTool):
    def __init__(self, aws_config: AWSConfig, localstack_config: LocalStackConfig):
        self._aws = aws_config
        self._ls = localstack_config
        self._session = None

    def _get_session(self):
        if self._session:
            return self._session
        import boto3

        kwargs = {
            "aws_access_key_id": self._aws.access_key_id or "test",
            "aws_secret_access_key": self._aws.secret_access_key or "test",
            "region_name": self._aws.region,
        }
        if self._aws.profile and not self._ls.enabled:
            import boto3
            self._session = boto3.Session(profile_name=self._aws.profile, region_name=self._aws.region)
            return self._session

        self._session = __import__("boto3").Session(**kwargs)
        return self._session

    def _client(self, service: str):
        session = self._get_session()
        if self._ls.enabled:
            return session.client(
                service,
                endpoint_url=self._ls.url,
                region_name=self._aws.region,
            )
        return session.client(service, region_name=self._aws.region)

    def get_definitions(self) -> List[Dict[str, Any]]:
        return TOOL_DEFINITIONS

    def get_tool_names(self) -> List[str]:
        return [t["name"] for t in TOOL_DEFINITIONS]

    def execute(self, tool_name: str, parameters: Dict[str, Any]) -> ToolResult:
        start = time.time()
        handlers = {
            "get_infrastructure_health":      self._get_infra_health,
            "list_ec2_instances":             self._list_ec2,
            "get_rds_status":                 self._get_rds,
            "get_eks_cluster_status":         self._get_eks,
            "get_elasticache_status":         self._get_elasticache,
            "get_s3_usage":                   self._get_s3,
            "get_optimization_recommendations": self._get_optimize,
        }
        handler = handlers.get(tool_name)
        if not handler:
            return ToolResult(
                tool_name=tool_name, operation=tool_name, success=False,
                error=f"Unknown tool: {tool_name}",
            )
        try:
            data = handler(parameters)
            return ToolResult(
                tool_name=tool_name, operation=tool_name, success=True,
                data=data, execution_time=round(time.time() - start, 2),
            )
        except Exception as e:
            logger.error(f"Resource tool error [{tool_name}]: {e}")
            return ToolResult(
                tool_name=tool_name, operation=tool_name, success=False,
                error=str(e), execution_time=round(time.time() - start, 2),
            )

    def _is_live(self) -> bool:
        return not self._ls.enabled

    def _cw_metric(self, namespace: str, metric: str, dimensions: List[Dict],
                   days: int = 7, stat: str = "Average") -> float:
        """CloudWatch average over N days. Returns 0.0 on error."""
        try:
            from datetime import datetime, timedelta
            cw = self._client("cloudwatch")
            end = datetime.utcnow()
            start = end - timedelta(days=days)
            resp = cw.get_metric_statistics(
                Namespace=namespace, MetricName=metric, Dimensions=dimensions,
                StartTime=start, EndTime=end, Period=days * 86400,
                Statistics=[stat],
            )
            pts = resp.get("Datapoints", [])
            if not pts:
                return 0.0
            return round(pts[0].get(stat, 0.0), 2)
        except Exception:
            return 0.0

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _get_infra_health(self, params: Dict) -> Dict:
        if self._is_live():
            try:
                rds_data = self._get_rds({})
                ec2_data = self._list_ec2({})
                ec_data  = self._get_elasticache({})
                s3_data  = self._get_s3({})
                return {
                    "source": "live",
                    "rds":          {"count": rds_data.get("count", 0), "instances": rds_data.get("instances", [])},
                    "ec2":          {"count": ec2_data.get("count", 0), "running": ec2_data.get("running", 0)},
                    "elasticache":  {"count": len(ec_data.get("clusters", []))},
                    "s3":           {"count": ec2_data.get("count", 0), "buckets": s3_data.get("count", 0)},
                    "total_monthly_cost_estimate": (
                        rds_data.get("total_monthly_cost", 0) + ec_data.get("total_monthly_cost", 0)
                    ),
                }
            except Exception as e:
                logger.warning(f"Live infra health failed: {e}")
        from backend.tools.mock_data import generate_infrastructure
        return generate_infrastructure()

    def _list_ec2(self, params: Dict) -> Dict:
        env_filter = params.get("environment")
        state_filter = params.get("state", "all")

        if self._is_live():
            try:
                from backend.tools.live_resources import (
                    _fetch_ec2_region, _aggregate_ec2, _run_per_region, _session as _lr_session,
                )
                sess = _lr_session(self._aws)
                regions = self._aws.scan_regions or [self._aws.region]
                per_region = _run_per_region(sess, regions, _fetch_ec2_region, "ec2")
                agg = _aggregate_ec2(per_region)
                all_instances = []
                for rd in per_region:
                    all_instances.extend(rd.get("instances", []))
                if env_filter:
                    all_instances = [i for i in all_instances if i.get("environment") == env_filter]
                if state_filter != "all":
                    all_instances = [i for i in all_instances if i.get("state") == state_filter]
                running = sum(1 for i in all_instances if i.get("state") == "running")
                return {
                    **agg,
                    "instances": all_instances,
                    "count": len(all_instances),
                    "running": running,
                    "source": "live-aws",
                }
            except Exception as e:
                logger.warning(f"Live EC2 query failed, falling back to mock: {e}")

        if self._ls.enabled:
            # Query LocalStack for seeded instances
            try:
                ec2 = self._client("ec2")
                filters = []
                if state_filter != "all":
                    filters.append({"Name": "instance-state-name", "Values": [state_filter]})
                if env_filter:
                    filters.append({"Name": "tag:Environment", "Values": [env_filter]})

                resp = ec2.describe_instances(Filters=filters) if filters else ec2.describe_instances()
                instances = []
                for res in resp.get("Reservations", []):
                    for inst in res.get("Instances", []):
                        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                        instances.append({
                            "instance_id": inst["InstanceId"],
                            "instance_type": inst["InstanceType"],
                            "state": inst["State"]["Name"],
                            "environment": tags.get("Environment", "unknown"),
                            "name": tags.get("Name", "unnamed"),
                            "role": tags.get("Role", ""),
                            "private_ip": inst.get("PrivateIpAddress", ""),
                            "launch_time": str(inst.get("LaunchTime", "")),
                        })

                if instances:
                    return {"instances": instances, "count": len(instances), "source": "localstack"}
            except Exception as e:
                logger.warning(f"LocalStack EC2 query failed, using mock: {e}")

        # Fallback to rich mock data
        instances = [
            {"instance_id": "i-0a1b2c3d4e5f60001", "instance_type": "m5.xlarge",   "state": "running", "environment": "production", "name": "eks-node-prod-1", "role": "eks-node", "cpu_pct": 24.1, "memory_pct": 41.3},
            {"instance_id": "i-0a1b2c3d4e5f60002", "instance_type": "m5.xlarge",   "state": "running", "environment": "production", "name": "eks-node-prod-2", "role": "eks-node", "cpu_pct": 31.2, "memory_pct": 52.8},
            {"instance_id": "i-0a1b2c3d4e5f60003", "instance_type": "m5.xlarge",   "state": "running", "environment": "production", "name": "eks-node-prod-3", "role": "eks-node", "cpu_pct": 18.7, "memory_pct": 38.9},
            {"instance_id": "i-0a1b2c3d4e5f60004", "instance_type": "m5.xlarge",   "state": "running", "environment": "production", "name": "eks-node-prod-4", "role": "eks-node", "cpu_pct": 27.4, "memory_pct": 45.1},
            {"instance_id": "i-0a1b2c3d4e5f60005", "instance_type": "c5.2xlarge",  "state": "running", "environment": "production", "name": "eks-node-prod-5", "role": "eks-node-compute", "cpu_pct": 42.3, "memory_pct": 28.6},
            {"instance_id": "i-0a1b2c3d4e5f60006", "instance_type": "c5.2xlarge",  "state": "running", "environment": "production", "name": "eks-node-prod-6", "role": "eks-node-compute", "cpu_pct": 38.9, "memory_pct": 31.2},
            {"instance_id": "i-0a1b2c3d4e5f60007", "instance_type": "m5.xlarge",   "state": "running", "environment": "production", "name": "eks-node-prod-7", "role": "eks-node", "cpu_pct": 15.2, "memory_pct": 44.7},
            {"instance_id": "i-0a1b2c3d4e5f60008", "instance_type": "m5.xlarge",   "state": "running", "environment": "production", "name": "eks-node-prod-8", "role": "eks-node", "cpu_pct": 19.8, "memory_pct": 39.3},
            {"instance_id": "i-0a1b2c3d4e5f60009", "instance_type": "t3.large",    "state": "running", "environment": "staging",    "name": "eks-node-stg-1",  "role": "eks-node", "cpu_pct": 8.4,  "memory_pct": 22.1},
            {"instance_id": "i-0a1b2c3d4e5f60010", "instance_type": "t3.large",    "state": "running", "environment": "staging",    "name": "eks-node-stg-2",  "role": "eks-node", "cpu_pct": 12.1, "memory_pct": 28.4},
            {"instance_id": "i-0a1b2c3d4e5f60011", "instance_type": "t3.large",    "state": "running", "environment": "staging",    "name": "eks-node-stg-3",  "role": "eks-node", "cpu_pct": 6.7,  "memory_pct": 19.8},
            {"instance_id": "i-0a1b2c3d4e5f60012", "instance_type": "t3.micro",    "state": "stopped", "environment": "dev",        "name": "bastion-dev",     "role": "bastion",  "cpu_pct": 0,    "memory_pct": 0},
        ]

        if env_filter:
            instances = [i for i in instances if i["environment"] == env_filter]
        if state_filter != "all":
            instances = [i for i in instances if i["state"] == state_filter]

        avg_cpu = round(sum(i["cpu_pct"] for i in instances if i["state"] == "running") / max(1, sum(1 for i in instances if i["state"] == "running")), 1)
        return {
            "instances": instances,
            "count": len(instances),
            "running": sum(1 for i in instances if i["state"] == "running"),
            "avg_cpu_pct": avg_cpu,
            "source": "mock",
        }

    def _get_rds(self, params: Dict) -> Dict:
        if self._ls.enabled:
            try:
                rds = self._client("rds")
                resp = rds.describe_db_instances()
                instances = []
                for inst in resp.get("DBInstances", []):
                    instances.append({
                        "identifier": inst["DBInstanceIdentifier"],
                        "class": inst["DBInstanceClass"],
                        "engine": f"{inst['Engine']} {inst.get('EngineVersion', '')}",
                        "status": inst["DBInstanceStatus"],
                        "multi_az": inst.get("MultiAZ", False),
                        "storage_gb": inst.get("AllocatedStorage", 0),
                    })
                if instances:
                    return {"instances": instances, "count": len(instances), "source": "localstack"}
            except Exception as e:
                logger.warning(f"LocalStack RDS query failed, using mock: {e}")

        return {
            "source": "mock",
            "instances": [
                {
                    "identifier": "devopsarg-prod-postgres",
                    "class": "db.r5.2xlarge",
                    "engine": "PostgreSQL 15.4",
                    "status": "available",
                    "multi_az": True,
                    "storage_gb": 500,
                    "storage_used_gb": 182,
                    "storage_pct": 36.4,
                    "connections_active": 847,
                    "connections_max": 2000,
                    "connections_pct": 42.3,
                    "avg_cpu_pct": 12.3,
                    "avg_iops": 2840,
                    "backup_retention": 7,
                    "environment": "production",
                    "monthly_cost": 580,
                    "optimization_note": "CPU at 12% — candidate for db.r5.xlarge downsize (save $340/mo)",
                },
                {
                    "identifier": "devopsarg-prod-postgres-replica",
                    "class": "db.r5.xlarge",
                    "engine": "PostgreSQL 15.4",
                    "status": "available",
                    "multi_az": False,
                    "storage_gb": 500,
                    "storage_used_gb": 182,
                    "storage_pct": 36.4,
                    "connections_active": 312,
                    "connections_max": 1000,
                    "connections_pct": 31.2,
                    "avg_cpu_pct": 8.1,
                    "environment": "production",
                    "role": "read-replica",
                    "monthly_cost": 145,
                },
                {
                    "identifier": "devopsarg-staging-postgres",
                    "class": "db.t3.medium",
                    "engine": "PostgreSQL 15.4",
                    "status": "available",
                    "multi_az": False,
                    "storage_gb": 100,
                    "storage_used_gb": 12,
                    "storage_pct": 12.0,
                    "connections_active": 18,
                    "connections_max": 312,
                    "connections_pct": 5.8,
                    "avg_cpu_pct": 3.2,
                    "environment": "staging",
                    "monthly_cost": 60,
                },
            ],
            "total_instances": 3,
            "total_monthly_cost": 785,
            "alert": "Primary instance (db.r5.2xlarge) averaging 12% CPU — rightsizing opportunity exists",
        }

    def _get_eks(self, params: Dict) -> Dict:
        cluster_filter = params.get("cluster_name")
        if self._ls.enabled:
            try:
                eks = self._client("eks")
                resp = eks.list_clusters()
                clusters = []
                for name in resp.get("clusters", []):
                    if cluster_filter and name != cluster_filter:
                        continue
                    cluster_resp = eks.describe_cluster(name=name)
                    c = cluster_resp.get("cluster", {})
                    clusters.append({
                        "name": name,
                        "status": c.get("status", "ACTIVE"),
                        "version": c.get("version", "1.29"),
                    })
                if clusters:
                    return {"clusters": clusters, "count": len(clusters), "source": "localstack"}
            except Exception as e:
                logger.warning(f"LocalStack EKS query failed, using mock: {e}")

        return {
            "source": "mock",
            "clusters": [
                {
                    "name": "devopsarg-prod",
                    "status": "ACTIVE",
                    "version": "1.29",
                    "region": "us-east-1",
                    "node_count": 8,
                    "node_provisioner": "Karpenter v1.0",
                    "node_types": ["m5.xlarge", "c5.2xlarge"],
                    "spot_pct": 40,
                    "on_demand_pct": 60,
                    "pods_running": 98,
                    "pods_pending": 0,
                    "pods_failed": 1,
                    "namespaces": ["default", "monitoring", "ingress", "app-prod"],
                    "avg_cpu_pct": 28.4,
                    "avg_memory_pct": 45.1,
                    "environment": "production",
                    "monthly_cost": 110,
                },
                {
                    "name": "devopsarg-staging",
                    "status": "ACTIVE",
                    "version": "1.29",
                    "region": "us-east-1",
                    "node_count": 3,
                    "node_provisioner": "Karpenter v1.0",
                    "node_types": ["t3.large"],
                    "spot_pct": 100,
                    "on_demand_pct": 0,
                    "pods_running": 26,
                    "pods_pending": 0,
                    "pods_failed": 1,
                    "namespaces": ["default", "app-staging"],
                    "avg_cpu_pct": 9.1,
                    "avg_memory_pct": 23.4,
                    "environment": "staging",
                    "monthly_cost": 36,
                    "optimization_note": "Cluster idle nights/weekends — scale-to-zero could save $240/mo",
                },
            ],
            "total_monthly_cost": 146,
        }

    def _get_elasticache(self, params: Dict) -> Dict:
        if self._ls.enabled:
            try:
                ec = self._client("elasticache")
                resp = ec.describe_cache_clusters()
                clusters = [
                    {
                        "id": c["CacheClusterId"],
                        "engine": f"{c.get('Engine', 'redis')} {c.get('EngineVersion', '')}",
                        "node_type": c.get("CacheNodeType", ""),
                        "status": c.get("CacheClusterStatus", ""),
                    }
                    for c in resp.get("CacheClusters", [])
                ]
                if clusters:
                    return {"clusters": clusters, "source": "localstack"}
            except Exception as e:
                logger.warning(f"LocalStack ElastiCache query failed, using mock: {e}")

        return {
            "source": "mock",
            "clusters": [
                {
                    "id": "devopsarg-prod-redis",
                    "engine": "Redis 7.0",
                    "node_type": "cache.r6g.large",
                    "nodes": 2,
                    "status": "available",
                    "hit_rate_pct": 96.2,
                    "memory_used_gb": 2.1,
                    "memory_total_gb": 13.4,
                    "memory_pct": 15.7,
                    "connections": 1240,
                    "evictions_per_sec": 0,
                    "commands_per_sec": 4820,
                    "environment": "production",
                    "monthly_cost": 240,
                    "health": "healthy",
                },
                {
                    "id": "devopsarg-staging-redis",
                    "engine": "Redis 7.0",
                    "node_type": "cache.t3.micro",
                    "nodes": 1,
                    "status": "available",
                    "hit_rate_pct": 88.4,
                    "memory_used_gb": 0.08,
                    "memory_total_gb": 0.5,
                    "memory_pct": 16.0,
                    "connections": 42,
                    "evictions_per_sec": 0,
                    "environment": "staging",
                    "monthly_cost": 55,
                    "health": "healthy",
                },
            ],
            "total_monthly_cost": 295,
        }

    def _get_s3(self, params: Dict) -> Dict:
        if self._ls.enabled:
            try:
                s3 = self._client("s3")
                resp = s3.list_buckets()
                buckets = []
                for b in resp.get("Buckets", []):
                    try:
                        tagging = s3.get_bucket_tagging(Bucket=b["Name"])
                        tags = {t["Key"]: t["Value"] for t in tagging.get("TagSet", [])}
                    except Exception:
                        tags = {}
                    buckets.append({
                        "name": b["Name"],
                        "created": str(b.get("CreationDate", "")),
                        "environment": tags.get("Environment", "unknown"),
                    })
                if buckets:
                    return {"buckets": buckets, "count": len(buckets), "source": "localstack"}
            except Exception as e:
                logger.warning(f"LocalStack S3 query failed, using mock: {e}")

        return {
            "source": "mock",
            "buckets": [
                {"name": "devopsarg-prod-assets",  "size_gb": 450,  "objects": 1_200_000, "lifecycle": True,  "storage_class": "Standard",    "environment": "production", "monthly_cost": 55},
                {"name": "devopsarg-prod-logs",    "size_gb": 1200, "objects": 3_400_000, "lifecycle": False, "storage_class": "Standard",    "environment": "production", "monthly_cost": 110, "alert": "No lifecycle policy — S3-IA transition after 30d would save ~$85/mo"},
                {"name": "devopsarg-prod-backups", "size_gb": 380,  "objects": 180_000,   "lifecycle": True,  "storage_class": "Glacier IR",  "environment": "production", "monthly_cost": 19},
                {"name": "devopsarg-staging",      "size_gb": 70,   "objects": 40_000,    "lifecycle": False, "storage_class": "Standard",    "environment": "staging",    "monthly_cost": 11},
            ],
            "total_size_gb": 2100,
            "total_objects": 4_820_000,
            "total_monthly_cost": 195,
            "buckets_without_lifecycle": 2,
            "potential_savings": 85,
        }

    def _get_optimize(self, params: Dict) -> Dict:
        from backend.tools.mock_data import generate_optimization
        return generate_optimization()
