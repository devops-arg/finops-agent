"""
Waste Analyzers — Cloudkeeper/Fix Inventory inspired checks for FinOps Agent.

55+ checks across EC2, EBS, RDS, ELB, NAT, ElastiCache, Lambda,
DynamoDB, S3, CloudWatch Logs, and misc services.

Two categories:
  - cleanup:   resources that should be deleted (zombies, orphans)
  - rightsize: resources that should be resized / reconfigured

Each analyzer:
  - Implements _live(client_factory) using boto3 (read-only, no writes)
  - Implements _mock() for LocalStack/USE_MOCK_DATA=true demo mode
  - Produces List[Finding] with realistic cost estimates

All live() calls are READ-ONLY — no AWS resource modifications.
"""

import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from backend.config.manager import AWSConfig, LocalStackConfig
from backend.models.finding import (
    CATEGORY_CLEANUP,
    CATEGORY_RIGHTSIZE,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    Finding,
    _severity_from_savings,
)
from backend.tools.base import BaseTool
from backend.models.core import ToolResult

logger = logging.getLogger(__name__)


# ── On-demand price table (USD/month estimates) ───────────────────────────────
EC2_MONTHLY_COST = {
    "t3.micro": 8, "t3.small": 15, "t3.medium": 30, "t3.large": 60,
    "t3.xlarge": 120, "t3.2xlarge": 240,
    "m5.large": 70, "m5.xlarge": 140, "m5.2xlarge": 280, "m5.4xlarge": 560,
    "m6i.large": 68, "m6i.xlarge": 136, "m6i.2xlarge": 272,
    "m6g.large": 55, "m6g.xlarge": 110, "m6g.2xlarge": 220,
    "c5.large": 62, "c5.xlarge": 124, "c5.2xlarge": 248, "c5.4xlarge": 496,
    "c6i.large": 60, "c6i.xlarge": 120, "c6i.2xlarge": 240,
    "c6g.large": 48, "c6g.xlarge": 96, "c6g.2xlarge": 192,
    "r5.large": 91, "r5.xlarge": 182, "r5.2xlarge": 364, "r5.4xlarge": 728,
    "r6i.large": 88, "r6i.xlarge": 176, "r6i.2xlarge": 352,
    "r6g.large": 73, "r6g.xlarge": 146, "r6g.2xlarge": 292,
}
RDS_MONTHLY_COST = {
    "db.t3.micro": 14, "db.t3.small": 28, "db.t3.medium": 56,
    "db.t3.large": 112, "db.t3.xlarge": 224,
    "db.m5.large": 138, "db.m5.xlarge": 276, "db.m5.2xlarge": 552,
    "db.r5.large": 182, "db.r5.xlarge": 364, "db.r5.2xlarge": 728,
    "db.r5.4xlarge": 1456, "db.r6g.large": 155, "db.r6g.xlarge": 310,
}
CACHE_MONTHLY_COST = {
    "cache.t3.micro": 12, "cache.t3.small": 24, "cache.t3.medium": 48,
    "cache.m5.large": 96, "cache.m5.xlarge": 192,
    "cache.m6g.large": 82, "cache.m6g.xlarge": 164,
    "cache.r5.large": 148, "cache.r5.xlarge": 296,
    "cache.r6g.large": 125, "cache.r6g.xlarge": 250,
}
EBS_GB_COST = {"gp2": 0.10, "gp3": 0.08, "io1": 0.125, "st1": 0.045, "sc1": 0.025}
EIP_MONTHLY = 3.60
ELB_MONTHLY = 18.0
NAT_HOUR = 0.045


def _ebs_monthly(volume_type: str, size_gb: int) -> float:
    return round(EBS_GB_COST.get(volume_type, 0.10) * size_gb, 2)


def _ec2_monthly(instance_type: str) -> float:
    return EC2_MONTHLY_COST.get(instance_type, 80)


def _rds_monthly(instance_class: str) -> float:
    return RDS_MONTHLY_COST.get(instance_class, 200)


def _cache_monthly(node_type: str) -> float:
    return CACHE_MONTHLY_COST.get(node_type, 100)


# ── Base analyzer ─────────────────────────────────────────────────────────────

class BaseAnalyzer(ABC):
    service: str = ""
    category: str = CATEGORY_CLEANUP
    # Set IS_GLOBAL = True for analyzers that call global AWS APIs (S3, Route53, CloudFront).
    # These run only once regardless of how many regions are configured.
    IS_GLOBAL: bool = False

    def __init__(self, aws_config: AWSConfig, localstack_config: LocalStackConfig):
        self._aws = aws_config
        self._ls = localstack_config

    def _should_mock(self) -> bool:
        if self._ls and self._ls.enabled:
            return True
        return os.environ.get("USE_MOCK_DATA", "").lower() in ("true", "1", "yes")

    def _boto_kwargs(self, region: str = None) -> Dict:
        target_region = region or self._aws.region
        if self._ls and self._ls.enabled:
            return {
                "endpoint_url": self._ls.url,
                "aws_access_key_id": "test",
                "aws_secret_access_key": "test",
                "region_name": target_region,
            }
        kw: Dict[str, Any] = {"region_name": target_region}
        if self._aws.access_key_id:
            kw["aws_access_key_id"] = self._aws.access_key_id
        if self._aws.secret_access_key:
            kw["aws_secret_access_key"] = self._aws.secret_access_key
        return kw

    def _client(self, service_name: str, region: str = None):
        import boto3
        from botocore.config import Config as BotocoreConfig
        cfg = BotocoreConfig(connect_timeout=10, read_timeout=20, retries={"max_attempts": 1})
        return boto3.client(service_name, config=cfg, **self._boto_kwargs(region))

    def _iter_buckets_concurrent(self, check_fn, max_workers: int = 20) -> List[Finding]:
        """Call check_fn(s3_client, bucket_name) for every bucket concurrently.

        check_fn should return a Finding, list[Finding], or None.
        Dramatically speeds up S3 analyzers that make one API call per bucket.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        s3 = self._client("s3")
        buckets = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
        if not buckets:
            return []
        findings: List[Finding] = []
        with ThreadPoolExecutor(max_workers=min(max_workers, len(buckets))) as ex:
            futs = {ex.submit(check_fn, s3, name): name for name in buckets}
            for fut in as_completed(futs):
                try:
                    result = fut.result()
                    if result is None:
                        continue
                    elif isinstance(result, list):
                        findings.extend(result)
                    else:
                        findings.append(result)
                except Exception:
                    pass
        return findings

    def _cw_avg(self, namespace: str, metric: str, dimensions: List[Dict],
                days: int = 7, stat: str = "Average") -> float:
        """Helper: CloudWatch average over N days. Returns 0.0 on any error."""
        try:
            cw = self._client("cloudwatch")
            end = datetime.utcnow()
            start = end - timedelta(days=days)
            resp = cw.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric,
                Dimensions=dimensions,
                StartTime=start,
                EndTime=end,
                Period=days * 86400,
                Statistics=[stat],
            )
            points = resp.get("Datapoints", [])
            if not points:
                return 0.0
            return points[0].get(stat, 0.0)
        except Exception:
            return 0.0

    def run(self) -> List[Finding]:
        if self._should_mock():
            try:
                return self._mock()
            except Exception as e:
                logger.warning(f"{self.__class__.__name__} mock error: {e}")
                return []
        try:
            return self._live()
        except Exception as e:
            logger.warning(f"{self.__class__.__name__} live error: {e}")
            return []

    @abstractmethod
    def _live(self) -> List[Finding]:
        pass

    @abstractmethod
    def _mock(self) -> List[Finding]:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# TIER 1 — describe_* only, no CloudWatch
# ═══════════════════════════════════════════════════════════════════════════════

class EBSUnattachedAnalyzer(BaseAnalyzer):
    service = "EBS"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        resp = ec2.describe_volumes(Filters=[{"Name": "status", "Values": ["available"]}])
        cutoff = datetime.utcnow() - timedelta(days=7)
        for vol in resp.get("Volumes", []):
            created = vol.get("CreateTime")
            if hasattr(created, "replace"):
                created = created.replace(tzinfo=None)
            if created and created > cutoff:
                continue
            idle_days = (datetime.utcnow() - created).days if created else 0
            size = vol.get("Size", 0)
            vtype = vol.get("VolumeType", "gp2")
            cost = _ebs_monthly(vtype, size)
            tags = {t["Key"]: t["Value"] for t in vol.get("Tags", [])}
            findings.append(Finding(
                resource_id=vol["VolumeId"],
                resource_type="ebs_volume",
                service=self.service,
                category=self.category,
                title=f"EBS volume unattached for {idle_days} days",
                description=f"{size}GB {vtype} volume not attached to any instance. Cost: ${cost}/mo.",
                severity=_severity_from_savings(cost),
                monthly_cost_usd=cost,
                estimated_savings_usd=cost,
                region=self._aws.region,
                idle_days=idle_days,
                tags=tags,
                metadata={"size_gb": size, "volume_type": vtype, "az": vol.get("AvailabilityZone")},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("vol-0abc1234567890001", "ebs_volume", self.service, self.category,
                    "EBS volume unattached for 45 days", "200GB gp2 volume not attached. $20/mo.",
                    SEVERITY_INFO, 20.0, 20.0, "us-east-1a", idle_days=45,
                    tags={"env": "staging", "team": "data"}, metadata={"size_gb": 200, "volume_type": "gp2"}),
            Finding("vol-0abc1234567890002", "ebs_volume", self.service, self.category,
                    "EBS volume unattached for 90 days", "500GB gp2 volume not attached. $50/mo.",
                    SEVERITY_WARNING, 50.0, 50.0, "us-east-1b", idle_days=90,
                    tags={"env": "dev"}, metadata={"size_gb": 500, "volume_type": "gp2"}),
            Finding("vol-0abc1234567890003", "ebs_volume", self.service, self.category,
                    "EBS volume unattached for 120 days", "2000GB gp2 volume. $200/mo.",
                    SEVERITY_CRITICAL, 200.0, 200.0, "us-east-1a", idle_days=120,
                    tags={"env": "prod", "team": "platform"}, metadata={"size_gb": 2000, "volume_type": "gp2"}),
        ]


class EC2StoppedWithEBSAnalyzer(BaseAnalyzer):
    service = "EC2"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        resp = ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}])
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                state_reason = inst.get("StateTransitionReason", "")
                stopped_time = None
                if "(" in state_reason and ")" in state_reason:
                    try:
                        ts_str = state_reason.split("(")[1].split(")")[0]
                        stopped_time = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        pass
                idle_days = (datetime.utcnow() - stopped_time).days if stopped_time else 0
                if idle_days < 7:
                    continue
                ebs_vols = [b for b in inst.get("BlockDeviceMappings", []) if b.get("Ebs")]
                vol_count = len(ebs_vols)
                itype = inst.get("InstanceType", "unknown")
                savings = 40 * vol_count
                findings.append(Finding(
                    resource_id=inst["InstanceId"],
                    resource_type="ec2_instance",
                    service=self.service,
                    category=self.category,
                    title=f"EC2 stopped {idle_days}d with {vol_count} EBS volume(s) attached",
                    description=f"{itype} instance stopped but paying for {vol_count} EBS volume(s). ~${savings}/mo.",
                    severity=_severity_from_savings(savings),
                    monthly_cost_usd=savings,
                    estimated_savings_usd=savings,
                    region=self._aws.region,
                    idle_days=idle_days,
                    tags=tags,
                    metadata={"instance_type": itype, "ebs_count": vol_count},
                ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("i-0stopped001", "ec2_instance", self.service, self.category,
                    "EC2 stopped 30d with 2 EBS volumes", "t3.large stopped, 2 EBS volumes attached. $80/mo.",
                    SEVERITY_WARNING, 80.0, 80.0, "us-east-1", idle_days=30,
                    tags={"env": "dev", "team": "data", "Name": "data-exploration-old"},
                    metadata={"instance_type": "t3.large", "ebs_count": 2}),
            Finding("i-0stopped002", "ec2_instance", self.service, self.category,
                    "EC2 stopped 60d with 3 EBS volumes", "m5.xlarge dev machine stopped 60d. 3 EBS volumes, $120/mo.",
                    SEVERITY_WARNING, 120.0, 120.0, "us-east-1", idle_days=60,
                    tags={"env": "dev", "team": "platform", "Name": "platform-devbox-old"},
                    metadata={"instance_type": "m5.xlarge", "ebs_count": 3}),
            Finding("i-0stopped003", "ec2_instance", self.service, self.category,
                    "EC2 stopped 90d with 1 EBS volume", "c5.2xlarge staging load gen stopped 90d. $248/mo waste.",
                    SEVERITY_CRITICAL, 248.0, 248.0, "us-east-1", idle_days=90,
                    tags={"env": "staging", "team": "growth", "Name": "loadgen-staging"},
                    metadata={"instance_type": "c5.2xlarge", "ebs_count": 1}),
        ]


class ElasticIPUnusedAnalyzer(BaseAnalyzer):
    service = "EC2"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        resp = ec2.describe_addresses()
        for addr in resp.get("Addresses", []):
            if addr.get("AssociationId"):
                continue
            tags = {t["Key"]: t["Value"] for t in addr.get("Tags", [])}
            findings.append(Finding(
                resource_id=addr.get("AllocationId", addr.get("PublicIp", "unknown")),
                resource_type="elastic_ip",
                service=self.service,
                category=self.category,
                title="Elastic IP not associated with any resource",
                description=f"EIP {addr.get('PublicIp')} is idle. Costs ${EIP_MONTHLY}/mo.",
                severity=SEVERITY_INFO,
                monthly_cost_usd=EIP_MONTHLY,
                estimated_savings_usd=EIP_MONTHLY,
                region=self._aws.region,
                tags=tags,
                metadata={"public_ip": addr.get("PublicIp")},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("eipalloc-001", "elastic_ip", self.service, self.category,
                    "Elastic IP not associated", "54.92.1.10 idle. $3.60/mo.",
                    SEVERITY_INFO, EIP_MONTHLY, EIP_MONTHLY, "us-east-1",
                    metadata={"public_ip": "54.92.1.10"}),
            Finding("eipalloc-002", "elastic_ip", self.service, self.category,
                    "Elastic IP not associated", "18.210.3.44 idle. $3.60/mo.",
                    SEVERITY_INFO, EIP_MONTHLY, EIP_MONTHLY, "us-east-1",
                    metadata={"public_ip": "18.210.3.44"}),
        ]


class EC2UntaggedAnalyzer(BaseAnalyzer):
    service = "EC2"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        resp = ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running"]}])
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                missing = [k for k in ("env", "Environment", "team", "Team") if k not in tags]
                if len(missing) < 2:
                    continue
                itype = inst.get("InstanceType", "unknown")
                findings.append(Finding(
                    resource_id=inst["InstanceId"],
                    resource_type="ec2_instance",
                    service=self.service,
                    category=self.category,
                    title="EC2 instance missing required tags (env, team)",
                    description=f"{itype} has no cost allocation tags. Cannot attribute spend to team/env.",
                    severity=SEVERITY_INFO,
                    monthly_cost_usd=_ec2_monthly(itype),
                    estimated_savings_usd=0,
                    region=self._aws.region,
                    tags=tags,
                    metadata={"instance_type": itype, "missing_tags": missing},
                ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("i-0untagged001", "ec2_instance", self.service, self.category,
                    "EC2 missing required tags (env, team)", "m5.xlarge running without cost allocation tags. Cannot attribute spend.",
                    SEVERITY_INFO, 140.0, 0.0, "us-east-1",
                    metadata={"instance_type": "m5.xlarge", "missing_tags": ["env", "team"]}),
            Finding("i-0untagged002", "ec2_instance", self.service, self.category,
                    "EC2 missing required tags (env, team)", "c5.2xlarge running without tags. $248/mo unattributed.",
                    SEVERITY_INFO, 248.0, 0.0, "us-east-1",
                    metadata={"instance_type": "c5.2xlarge", "missing_tags": ["env", "team"]}),
            Finding("i-0untagged003", "ec2_instance", self.service, self.category,
                    "EC2 missing required tags (env, team)", "r5.xlarge (RDS-like workload?) running without tags.",
                    SEVERITY_INFO, 182.0, 0.0, "us-east-1",
                    metadata={"instance_type": "r5.xlarge", "missing_tags": ["env", "team"]}),
        ]


class EC2OrphanAMIAnalyzer(BaseAnalyzer):
    service = "EC2"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        ami_resp = ec2.describe_images(Owners=["self"])
        inst_resp = ec2.describe_instances()
        used_amis = {
            inst.get("ImageId")
            for res in inst_resp.get("Reservations", [])
            for inst in res.get("Instances", [])
        }
        cutoff = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
        for ami in ami_resp.get("Images", []):
            if ami["ImageId"] in used_amis:
                continue
            if ami.get("CreationDate", "") > cutoff:
                continue
            tags = {t["Key"]: t["Value"] for t in ami.get("Tags", [])}
            snap_count = len(ami.get("BlockDeviceMappings", []))
            cost = 2.0 * snap_count
            findings.append(Finding(
                resource_id=ami["ImageId"],
                resource_type="ami",
                service=self.service,
                category=self.category,
                title="AMI with no running instances",
                description=f"AMI {ami.get('Name', ami['ImageId'])} not used by any instance. ~${cost}/mo in snapshots.",
                severity=SEVERITY_INFO,
                monthly_cost_usd=cost,
                estimated_savings_usd=cost,
                region=self._aws.region,
                tags=tags,
                metadata={"ami_name": ami.get("Name"), "snapshot_count": snap_count},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ami-0orphan001", "ami", self.service, self.category,
                    "AMI with no running instances", "ami-bastion-v3 not in use. $4/mo in snapshots.",
                    SEVERITY_INFO, 4.0, 4.0, "us-east-1", idle_days=45,
                    metadata={"ami_name": "bastion-2024-q2", "snapshot_count": 2}),
        ]


class SecurityGroupUnusedAnalyzer(BaseAnalyzer):
    service = "EC2"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        sg_resp = ec2.describe_security_groups()
        eni_resp = ec2.describe_network_interfaces()
        used_sgs = {
            sg_id
            for eni in eni_resp.get("NetworkInterfaces", [])
            for sg in eni.get("Groups", [])
            for sg_id in [sg.get("GroupId")]
        }
        for sg in sg_resp.get("SecurityGroups", []):
            if sg["GroupId"] in used_sgs or sg["GroupName"] == "default":
                continue
            tags = {t["Key"]: t["Value"] for t in sg.get("Tags", [])}
            findings.append(Finding(
                resource_id=sg["GroupId"],
                resource_type="security_group",
                service=self.service,
                category=self.category,
                title="Security group not attached to any resource",
                description=f"SG '{sg.get('GroupName')}' has no ENIs. Unused SGs create security noise.",
                severity=SEVERITY_INFO,
                monthly_cost_usd=0.0,
                estimated_savings_usd=0.0,
                region=self._aws.region,
                tags=tags,
                metadata={"sg_name": sg.get("GroupName"), "rules_count": len(sg.get("IpPermissions", []))},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("sg-0unused001", "security_group", self.service, self.category,
                    "Security group not attached to any resource", "'sg-old-bastion-2022' has no ENIs. Stale from decommissioned bastion.",
                    SEVERITY_INFO, 0.0, 0.0, "us-east-1",
                    metadata={"sg_name": "sg-old-bastion-2022", "rules_count": 3}),
            Finding("sg-0unused002", "security_group", self.service, self.category,
                    "Security group not attached to any resource", "'sg-migration-workers' has no ENIs. Migration completed Q4 2024.",
                    SEVERITY_INFO, 0.0, 0.0, "us-east-1",
                    metadata={"sg_name": "sg-migration-workers", "rules_count": 8}),
            Finding("sg-0unused003", "security_group", self.service, self.category,
                    "Security group not attached to any resource", "'sg-lb-old-api-v1' has no ENIs. ALB replaced.",
                    SEVERITY_INFO, 0.0, 0.0, "us-east-1",
                    metadata={"sg_name": "sg-lb-old-api-v1", "rules_count": 2}),
            Finding("sg-0unused004", "security_group", self.service, self.category,
                    "Security group not attached to any resource", "'sg-rds-staging-v1' has no ENIs. Staging DB replaced.",
                    SEVERITY_INFO, 0.0, 0.0, "us-east-1",
                    metadata={"sg_name": "sg-rds-staging-v1", "rules_count": 5}),
        ]


class EBSSnapshotOrphanAnalyzer(BaseAnalyzer):
    service = "EBS"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        snap_resp = ec2.describe_snapshots(OwnerIds=["self"])
        vol_resp = ec2.describe_volumes()
        existing_vols = {v["VolumeId"] for v in vol_resp.get("Volumes", [])}
        for snap in snap_resp.get("Snapshots", []):
            vol_id = snap.get("VolumeId", "")
            if vol_id and vol_id in existing_vols:
                continue
            if not vol_id or vol_id.startswith("vol-"):
                tags = {t["Key"]: t["Value"] for t in snap.get("Tags", [])}
                size = snap.get("VolumeSize", 0)
                cost = round(size * 0.05, 2)
                created = snap.get("StartTime")
                idle_days = (datetime.utcnow() - created.replace(tzinfo=None)).days if created else 0
                findings.append(Finding(
                    resource_id=snap["SnapshotId"],
                    resource_type="ebs_snapshot",
                    service=self.service,
                    category=self.category,
                    title="Snapshot — source volume no longer exists",
                    description=f"{size}GB snapshot, volume {vol_id} deleted. ${cost}/mo.",
                    severity=_severity_from_savings(cost),
                    monthly_cost_usd=cost,
                    estimated_savings_usd=cost,
                    region=self._aws.region,
                    idle_days=idle_days,
                    tags=tags,
                    metadata={"size_gb": size, "source_volume": vol_id},
                ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("snap-0orphan001", "ebs_snapshot", self.service, self.category,
                    "Snapshot — source volume deleted", "500GB snapshot of deleted volume. $25/mo.",
                    SEVERITY_WARNING, 25.0, 25.0, "us-east-1", idle_days=60,
                    metadata={"size_gb": 500, "source_volume": "vol-deleted001"}),
            Finding("snap-0orphan002", "ebs_snapshot", self.service, self.category,
                    "Snapshot — source volume deleted", "100GB snapshot of deleted volume. $5/mo.",
                    SEVERITY_INFO, 5.0, 5.0, "us-east-1", idle_days=90,
                    metadata={"size_gb": 100, "source_volume": "vol-deleted002"}),
        ]


class EBSSnapshotOldAnalyzer(BaseAnalyzer):
    service = "EBS"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        snap_resp = ec2.describe_snapshots(OwnerIds=["self"])
        cutoff = datetime.utcnow() - timedelta(days=90)
        for snap in snap_resp.get("Snapshots", []):
            created = snap.get("StartTime")
            if not created:
                continue
            created_dt = created.replace(tzinfo=None)
            if created_dt > cutoff:
                continue
            idle_days = (datetime.utcnow() - created_dt).days
            size = snap.get("VolumeSize", 0)
            cost = round(size * 0.05, 2)
            tags = {t["Key"]: t["Value"] for t in snap.get("Tags", [])}
            findings.append(Finding(
                resource_id=snap["SnapshotId"],
                resource_type="ebs_snapshot",
                service=self.service,
                category=self.category,
                title=f"Snapshot older than 90 days ({idle_days}d)",
                description=f"{size}GB snapshot from {created_dt.date()}. ${cost}/mo.",
                severity=SEVERITY_INFO,
                monthly_cost_usd=cost,
                estimated_savings_usd=cost,
                region=self._aws.region,
                idle_days=idle_days,
                tags=tags,
                metadata={"size_gb": size},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("snap-0old001", "ebs_snapshot", self.service, self.category,
                    "Snapshot older than 90 days (180d)", "1000GB old snapshot. $50/mo.",
                    SEVERITY_WARNING, 50.0, 50.0, "us-east-1", idle_days=180,
                    metadata={"size_gb": 1000}),
        ]


class EBSSnapshotNoAMIAnalyzer(BaseAnalyzer):
    service = "EBS"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        snap_resp = ec2.describe_snapshots(OwnerIds=["self"])
        ami_resp = ec2.describe_images(Owners=["self"])
        ami_snap_ids = {
            bdm.get("Ebs", {}).get("SnapshotId")
            for img in ami_resp.get("Images", [])
            for bdm in img.get("BlockDeviceMappings", [])
            if bdm.get("Ebs")
        }
        for snap in snap_resp.get("Snapshots", []):
            if snap["SnapshotId"] in ami_snap_ids:
                continue
            desc = snap.get("Description", "")
            if "Created by CreateImage" in desc and snap["SnapshotId"] not in ami_snap_ids:
                size = snap.get("VolumeSize", 0)
                cost = round(size * 0.05, 2)
                tags = {t["Key"]: t["Value"] for t in snap.get("Tags", [])}
                findings.append(Finding(
                    resource_id=snap["SnapshotId"],
                    resource_type="ebs_snapshot",
                    service=self.service,
                    category=self.category,
                    title="Snapshot created by AMI — AMI no longer exists",
                    description=f"{size}GB AMI snapshot with no parent AMI. ${cost}/mo.",
                    severity=_severity_from_savings(cost),
                    monthly_cost_usd=cost,
                    estimated_savings_usd=cost,
                    region=self._aws.region,
                    tags=tags,
                    metadata={"size_gb": size, "description": desc},
                ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("snap-0ami001", "ebs_snapshot", self.service, self.category,
                    "Snapshot created by AMI — AMI no longer exists",
                    "800GB AMI snapshot, parent AMI ami-0payments-v4 deregistered 75d ago. $40/mo.",
                    SEVERITY_WARNING, 40.0, 40.0, "us-east-1", idle_days=75,
                    tags={"team": "payments"}, metadata={"size_gb": 800, "description": "Created by CreateImage for ami-0payments-v4"}),
            Finding("snap-0ami002", "ebs_snapshot", self.service, self.category,
                    "Snapshot created by AMI — AMI no longer exists",
                    "400GB AMI snapshot, parent AMI ami-0worker-v2 deregistered 120d ago. $20/mo.",
                    SEVERITY_INFO, 20.0, 20.0, "us-east-1", idle_days=120,
                    tags={"team": "platform"}, metadata={"size_gb": 400, "description": "Created by CreateImage for ami-0worker-v2"}),
        ]


class RDSStoppedAnalyzer(BaseAnalyzer):
    service = "RDS"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        rds = self._client("rds")
        findings = []
        resp = rds.describe_db_instances()
        for inst in resp.get("DBInstances", []):
            if inst.get("DBInstanceStatus") != "stopped":
                continue
            cls = inst.get("DBInstanceClass", "db.t3.medium")
            storage = inst.get("AllocatedStorage", 0)
            storage_cost = round(storage * 0.115, 2)
            tags = {t["Key"]: t["Value"] for t in inst.get("TagList", [])}
            findings.append(Finding(
                resource_id=inst["DBInstanceIdentifier"],
                resource_type="rds_instance",
                service=self.service,
                category=self.category,
                title="RDS instance stopped — still paying for storage",
                description=f"{cls} stopped. Paying ~${storage_cost}/mo for {storage}GB storage.",
                severity=_severity_from_savings(storage_cost),
                monthly_cost_usd=storage_cost,
                estimated_savings_usd=storage_cost,
                region=self._aws.region,
                tags=tags,
                metadata={"instance_class": cls, "storage_gb": storage, "engine": inst.get("Engine")},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("db-dev-analytics", "rds_instance", self.service, self.category,
                    "RDS stopped — still paying for storage",
                    "db.t3.medium 'db-dev-analytics' stopped 45d. Paying $11.50/mo for 100GB storage while instance is idle.",
                    SEVERITY_INFO, 11.5, 11.5, "us-east-1",
                    tags={"env": "dev", "team": "data"}, metadata={"instance_class": "db.t3.medium", "storage_gb": 100}),
            Finding("db-sandbox-payments", "rds_instance", self.service, self.category,
                    "RDS stopped — still paying for storage",
                    "db.t3.large 'db-sandbox-payments' stopped 20d. $23/mo for storage. Delete or snapshot and delete.",
                    SEVERITY_INFO, 23.0, 23.0, "us-east-1",
                    tags={"env": "sandbox", "team": "payments"}, metadata={"instance_class": "db.t3.large", "storage_gb": 200}),
        ]


class RDSMultiAZNonProdAnalyzer(BaseAnalyzer):
    service = "RDS"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        rds = self._client("rds")
        findings = []
        resp = rds.describe_db_instances()
        for inst in resp.get("DBInstances", []):
            if not inst.get("MultiAZ"):
                continue
            tags = {t["Key"]: t["Value"] for t in inst.get("TagList", [])}
            env = tags.get("env", tags.get("Environment", "")).lower()
            if env in ("production", "prod"):
                continue
            cls = inst.get("DBInstanceClass", "db.t3.medium")
            base = _rds_monthly(cls)
            savings = round(base * 0.5, 2)
            findings.append(Finding(
                resource_id=inst["DBInstanceIdentifier"],
                resource_type="rds_instance",
                service=self.service,
                category=self.category,
                title=f"Multi-AZ enabled on non-production RDS ({env or 'unknown env'})",
                description=f"{cls} with Multi-AZ in non-prod. Disable it to save ~${savings}/mo.",
                severity=_severity_from_savings(savings),
                monthly_cost_usd=base,
                estimated_savings_usd=savings,
                region=self._aws.region,
                tags=tags,
                metadata={"instance_class": cls, "env": env},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("db-staging-postgres", "rds_instance", self.service, self.category,
                    "Multi-AZ on staging RDS — unnecessary",
                    "db.t3.medium 'db-staging-postgres' has Multi-AZ in staging env. No SLA requires HA here. Save $30/mo by disabling.",
                    SEVERITY_WARNING, 60.0, 30.0, "us-east-1",
                    tags={"env": "staging"}, metadata={"instance_class": "db.t3.medium", "env": "staging"}),
            Finding("db-growth-staging", "rds_instance", self.service, self.category,
                    "Multi-AZ on staging RDS — unnecessary",
                    "db.m5.large 'db-growth-staging' Multi-AZ in staging. Disable Multi-AZ. Save $69/mo.",
                    SEVERITY_WARNING, 138.0, 69.0, "us-east-1",
                    tags={"env": "staging", "team": "growth"}, metadata={"instance_class": "db.m5.large", "env": "staging"}),
        ]


class RDSNoRecentSnapshotAnalyzer(BaseAnalyzer):
    service = "RDS"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        rds = self._client("rds")
        findings = []
        resp = rds.describe_db_instances()
        for inst in resp.get("DBInstances", []):
            if inst.get("DBInstanceStatus") != "available":
                continue
            latest = inst.get("LatestRestorableTime")
            if not latest:
                idle_days = 999
            else:
                idle_days = (datetime.utcnow() - latest.replace(tzinfo=None)).days
            if idle_days <= 7:
                continue
            tags = {t["Key"]: t["Value"] for t in inst.get("TagList", [])}
            findings.append(Finding(
                resource_id=inst["DBInstanceIdentifier"],
                resource_type="rds_instance",
                service=self.service,
                category=self.category,
                title=f"RDS no backup in {idle_days} days — data risk",
                description=f"Last restorable point: {idle_days}d ago. Enable automated backups.",
                severity=SEVERITY_WARNING,
                monthly_cost_usd=0.0,
                estimated_savings_usd=0.0,
                region=self._aws.region,
                idle_days=idle_days,
                tags=tags,
                metadata={"latest_restorable": str(latest)},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return []


class RDSSnapshotOldAnalyzer(BaseAnalyzer):
    service = "RDS"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        rds = self._client("rds")
        findings = []
        resp = rds.describe_db_snapshots(SnapshotType="manual")
        cutoff = datetime.utcnow() - timedelta(days=30)
        for snap in resp.get("DBSnapshots", []):
            created = snap.get("SnapshotCreateTime")
            if not created:
                continue
            created_dt = created.replace(tzinfo=None)
            if created_dt > cutoff:
                continue
            idle_days = (datetime.utcnow() - created_dt).days
            size = snap.get("AllocatedStorage", 0)
            cost = round(size * 0.095, 2)
            findings.append(Finding(
                resource_id=snap["DBSnapshotIdentifier"],
                resource_type="rds_snapshot",
                service=self.service,
                category=self.category,
                title=f"Manual RDS snapshot older than 30 days ({idle_days}d)",
                description=f"{size}GB snapshot from {created_dt.date()}. ${cost}/mo.",
                severity=_severity_from_savings(cost),
                monthly_cost_usd=cost,
                estimated_savings_usd=cost,
                region=self._aws.region,
                idle_days=idle_days,
                metadata={"size_gb": size},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("rds-snap-old-001", "rds_snapshot", self.service, self.category,
                    "Manual RDS snapshot older than 30 days (65d)",
                    "500GB snapshot 'prod-before-migration-2024q4' from 65d ago. Migration complete, safe to delete. $47.50/mo.",
                    SEVERITY_WARNING, 47.5, 47.5, "us-east-1", idle_days=65,
                    tags={"team": "platform"}, metadata={"size_gb": 500, "db": "ribbon-prod-postgres"}),
            Finding("rds-snap-old-002", "rds_snapshot", self.service, self.category,
                    "Manual RDS snapshot older than 30 days (120d)",
                    "200GB snapshot 'staging-baseline-2024q3' from 120d ago. $19/mo.",
                    SEVERITY_INFO, 19.0, 19.0, "us-east-1", idle_days=120,
                    tags={"team": "platform"}, metadata={"size_gb": 200, "db": "ribbon-staging-postgres"}),
        ]


class RDSNoRecentSnapshotAnalyzer(BaseAnalyzer):
    service = "RDS"
    category = CATEGORY_RIGHTSIZE

    def _mock(self) -> List[Finding]:
        return [
            Finding("db-payments-replica", "rds_instance", self.service, self.category,
                    "RDS no backup in 12 days — data risk",
                    "db.r5.xlarge production replica has no automated backup configured. Data loss risk.",
                    SEVERITY_WARNING, 0.0, 0.0, "us-east-1", idle_days=12,
                    tags={"env": "production", "team": "payments"},
                    metadata={"latest_restorable": "None"}),
        ]

    def _live(self) -> List[Finding]:
        rds = self._client("rds")
        findings = []
        resp = rds.describe_db_instances()
        for inst in resp.get("DBInstances", []):
            if inst.get("DBInstanceStatus") != "available":
                continue
            latest = inst.get("LatestRestorableTime")
            if not latest:
                idle_days = 999
            else:
                idle_days = (datetime.utcnow() - latest.replace(tzinfo=None)).days
            if idle_days <= 7:
                continue
            tags = {t["Key"]: t["Value"] for t in inst.get("TagList", [])}
            findings.append(Finding(
                resource_id=inst["DBInstanceIdentifier"],
                resource_type="rds_instance",
                service=self.service,
                category=self.category,
                title=f"RDS no backup in {idle_days} days — data risk",
                description=f"Last restorable point: {idle_days}d ago. Enable automated backups.",
                severity=SEVERITY_WARNING,
                monthly_cost_usd=0.0,
                estimated_savings_usd=0.0,
                region=self._aws.region,
                idle_days=idle_days,
                tags=tags,
                metadata={"latest_restorable": str(latest)},
            ))
        return findings


class RDSAuroraNoReadersAnalyzer(BaseAnalyzer):
    service = "RDS"
    category = CATEGORY_RIGHTSIZE

    # Approximate Aurora vs RDS on-demand hourly price delta (USD) by instance class family.
    # Aurora charges ~20-30% more than equivalent RDS for same vCPU/RAM.
    # Source: aws.amazon.com/rds/aurora/pricing (us-east-1 reference, approximate)
    _AURORA_OVERHEAD_PER_HOUR: Dict[str, float] = {
        "db.t3":        0.02,
        "db.t4g":       0.02,
        "db.r5":        0.06,
        "db.r6g":       0.06,
        "db.r6i":       0.06,
        "db.r7g":       0.07,
        "db.x2g":       0.20,
        "db.serverless": 0.0,   # ACU-based — can't estimate without usage data
    }

    def _aurora_monthly_overhead(self, instance_class: str) -> float:
        """Estimated monthly cost overhead of Aurora vs equivalent RDS (compute only)."""
        for prefix, hourly in self._AURORA_OVERHEAD_PER_HOUR.items():
            if instance_class.startswith(prefix):
                return round(hourly * 730, 2)   # 730 hrs/month
        return 0.0

    def _live(self) -> List[Finding]:
        rds = self._client("rds")
        findings = []
        resp = rds.describe_db_clusters()
        for cluster in resp.get("DBClusters", []):
            members = cluster.get("DBClusterMembers", [])
            writers  = [m for m in members if m.get("IsClusterWriter")]
            readers  = [m for m in members if not m.get("IsClusterWriter")]

            # Only flag single-node clusters (writer handles all reads)
            if readers or not members:
                continue

            status = cluster.get("Status", "available")

            # Skip stopped clusters — compute cost is $0, no savings to report
            if status in ("stopped", "stopping"):
                continue

            tags = {t["Key"]: t["Value"] for t in cluster.get("TagList", [])}
            engine         = cluster.get("Engine", "aurora")
            engine_version = cluster.get("EngineVersion", "")
            serverless_cfg = cluster.get("ServerlessV2ScalingConfiguration")

            # Look up writer instance class
            writer_class = "db.serverless" if serverless_cfg else "unknown"
            if writers and writer_class == "unknown":
                try:
                    inst_resp = rds.describe_db_instances(
                        DBInstanceIdentifier=writers[0]["DBInstanceIdentifier"]
                    )
                    insts = inst_resp.get("DBInstances", [])
                    if insts:
                        writer_class = insts[0].get("DBInstanceClass", "unknown")
                except Exception:
                    pass

            overhead = self._aurora_monthly_overhead(writer_class)
            is_serverless = writer_class == "db.serverless" or serverless_cfg is not None

            if is_serverless:
                savings_note = "Aurora Serverless v2 — overhead depends on actual ACU usage; cannot estimate without CloudWatch metrics."
                estimated_savings = 0.0
                monthly_cost = 0.0
            else:
                estimated_savings = overhead
                monthly_cost = overhead  # we only know the overhead, not the full bill
                savings_note = (
                    f"Migrating to RDS {engine.replace('aurora-','')} {writer_class} "
                    f"(same specs, no Aurora overhead) saves ~${overhead:.0f}/mo."
                )

            desc = (
                f"Single-node Aurora cluster ({writer_class}, {engine} {engine_version}). "
                f"Writer handles all reads — no replicas. "
                f"Aurora adds ~20-30% compute overhead vs equivalent RDS for single-node setups. "
                f"{savings_note}"
            )

            findings.append(Finding(
                resource_id=cluster["DBClusterIdentifier"],
                resource_type="aurora_cluster",
                service=self.service,
                category=self.category,
                title=f"Single-node Aurora — no reader replicas ({writer_class})",
                description=desc,
                severity=SEVERITY_INFO if estimated_savings < 50 else SEVERITY_WARNING,
                monthly_cost_usd=monthly_cost,
                estimated_savings_usd=estimated_savings,
                region=self._aws.region,
                tags=tags,
                metadata={
                    "engine": engine,
                    "engine_version": engine_version,
                    "member_count": len(members),
                    "writer_instance_class": writer_class,
                    "status": status,
                    "serverless_v2": is_serverless,
                    "recommendation": "Migrate to RDS MySQL/PostgreSQL (same instance class) to eliminate Aurora overhead, or add a reader replica if read scaling is needed.",
                },
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-data-aurora", "aurora_cluster", self.service, self.category,
                    "Single-node Aurora — no reader replicas (db.r6g.xlarge)",
                    "Single-node Aurora cluster (db.r6g.xlarge, aurora-postgresql). The writer instance handles all reads and writes — no reader replicas exist. Aurora adds ~20-30% cost overhead vs equivalent RDS PostgreSQL for single-node setups. Status: available.",
                    SEVERITY_WARNING, 310.0, 80.0, "us-east-1",
                    tags={"env": "production", "team": "data"},
                    metadata={"engine": "aurora-postgresql", "member_count": 1, "writer_instance_class": "db.r6g.xlarge",
                              "recommendation": "Migrate to RDS PostgreSQL (same instance class) to eliminate Aurora overhead, or add a reader replica if read scaling is needed."}),
        ]


class ELBNoTargetsAnalyzer(BaseAnalyzer):
    service = "ELB"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        elb = self._client("elbv2")
        findings = []
        lbs = elb.describe_load_balancers().get("LoadBalancers", [])
        for lb in lbs:
            tgs = elb.describe_target_groups(LoadBalancerArn=lb["LoadBalancerArn"]).get("TargetGroups", [])
            all_empty = True
            all_unhealthy = True
            for tg in tgs:
                health = elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"]).get("TargetHealthDescriptions", [])
                if health:
                    all_empty = False
                    if any(h.get("TargetHealth", {}).get("State") == "healthy" for h in health):
                        all_unhealthy = False
            if not tgs or all_empty:
                title = "Load balancer has no registered targets"
            elif all_unhealthy:
                title = "Load balancer — all targets unhealthy"
            else:
                continue
            findings.append(Finding(
                resource_id=lb["LoadBalancerArn"].split("/")[-2],
                resource_type="load_balancer",
                service=self.service,
                category=self.category,
                title=title,
                description=f"{lb['Type'].upper()} '{lb['LoadBalancerName']}' serving no traffic. ~${ELB_MONTHLY}/mo.",
                severity=_severity_from_savings(ELB_MONTHLY),
                monthly_cost_usd=ELB_MONTHLY,
                estimated_savings_usd=ELB_MONTHLY,
                region=self._aws.region,
                metadata={"lb_name": lb["LoadBalancerName"], "lb_type": lb.get("Type")},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("alb-old-api", "load_balancer", self.service, self.category,
                    "Load balancer has no registered targets", "ALB 'alb-old-api' empty. $18/mo.",
                    SEVERITY_INFO, ELB_MONTHLY, ELB_MONTHLY, "us-east-1",
                    metadata={"lb_name": "alb-old-api", "lb_type": "application"}),
        ]


class ELBNoListenerAnalyzer(BaseAnalyzer):
    service = "ELB"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        elb = self._client("elbv2")
        findings = []
        lbs = elb.describe_load_balancers().get("LoadBalancers", [])
        for lb in lbs:
            listeners = elb.describe_listeners(LoadBalancerArn=lb["LoadBalancerArn"]).get("Listeners", [])
            if listeners:
                continue
            findings.append(Finding(
                resource_id=lb["LoadBalancerArn"].split("/")[-2],
                resource_type="load_balancer",
                service=self.service,
                category=self.category,
                title="Load balancer with no listeners configured",
                description=f"'{lb['LoadBalancerName']}' has no listeners — cannot route traffic. ${ELB_MONTHLY}/mo.",
                severity=_severity_from_savings(ELB_MONTHLY),
                monthly_cost_usd=ELB_MONTHLY,
                estimated_savings_usd=ELB_MONTHLY,
                region=self._aws.region,
                metadata={"lb_name": lb["LoadBalancerName"]},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("nlb-internal-grpc", "load_balancer", self.service, self.category,
                    "NLB with no listeners configured",
                    "NLB 'nlb-internal-grpc' created for a migration that never completed. No listeners. $18/mo.",
                    SEVERITY_INFO, ELB_MONTHLY, ELB_MONTHLY, "us-east-1",
                    metadata={"lb_name": "nlb-internal-grpc", "lb_type": "network"}),
        ]


class ELBClassicAnalyzer(BaseAnalyzer):
    service = "ELB"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        try:
            elb = self._client("elb")
            findings = []
            lbs = elb.describe_load_balancers().get("LoadBalancerDescriptions", [])
            for lb in lbs:
                findings.append(Finding(
                    resource_id=lb["LoadBalancerName"],
                    resource_type="classic_elb",
                    service=self.service,
                    category=self.category,
                    title="Classic Load Balancer (ELBv1) — deprecated by AWS",
                    description=f"'{lb['LoadBalancerName']}' uses legacy ELBv1. Migrate to ALB/NLB for better features and cost.",
                    severity=SEVERITY_WARNING,
                    monthly_cost_usd=ELB_MONTHLY,
                    estimated_savings_usd=0.0,
                    region=self._aws.region,
                    metadata={"dns_name": lb.get("DNSName")},
                ))
            return findings
        except Exception:
            return []

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-legacy-lb", "classic_elb", self.service, self.category,
                    "Classic Load Balancer (ELBv1) — deprecated by AWS",
                    "'ribbon-legacy-lb' is a Classic ELB from 2019. AWS EOL 2022. Migrate to ALB for connection draining, WAF, and HTTPS header support.",
                    SEVERITY_WARNING, ELB_MONTHLY, 0.0, "us-east-1",
                    tags={"env": "production", "team": "platform"},
                    metadata={"dns_name": "ribbon-legacy-lb-1234567890.us-east-1.elb.amazonaws.com"}),
        ]


class S3NoBucketLifecycleAnalyzer(BaseAnalyzer):
    service = "S3"
    category = CATEGORY_RIGHTSIZE
    IS_GLOBAL = True

    def _live(self) -> List[Finding]:
        svc, cat = self.service, self.category
        def _check(s3, name):
            try:
                s3.get_bucket_lifecycle_configuration(Bucket=name)
                return None
            except Exception as e:
                if "NoSuchLifecycleConfiguration" in str(e):
                    return Finding(
                        resource_id=name, resource_type="s3_bucket", service=svc, category=cat,
                        title="S3 bucket without lifecycle policy",
                        description=f"'{name}' stores data in STANDARD forever. Add lifecycle to transition to IA/Glacier.",
                        severity=SEVERITY_WARNING, monthly_cost_usd=50.0, estimated_savings_usd=30.0,
                        region="us-east-1", metadata={"bucket": name},
                    )
                return None
        return self._iter_buckets_concurrent(_check)

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-prod-logs", "s3_bucket", self.service, self.category,
                    "S3 bucket without lifecycle policy", "'ribbon-prod-logs' has 1.2TB Standard. $110/mo → $25/mo with Glacier.",
                    SEVERITY_WARNING, 110.0, 85.0, "us-east-1",
                    metadata={"bucket": "ribbon-prod-logs", "size_gb": 1200}),
            Finding("ribbon-staging", "s3_bucket", self.service, self.category,
                    "S3 bucket without lifecycle policy", "'ribbon-staging' 70GB Standard no lifecycle.",
                    SEVERITY_INFO, 11.0, 6.0, "us-east-1",
                    metadata={"bucket": "ribbon-staging", "size_gb": 70}),
        ]


class S3MultipartIncompleteAnalyzer(BaseAnalyzer):
    service = "S3"
    category = CATEGORY_CLEANUP
    IS_GLOBAL = True

    def _live(self) -> List[Finding]:
        svc, cat = self.service, self.category
        cutoff = datetime.utcnow() - timedelta(days=7)
        def _check(s3, name):
            try:
                uploads = s3.list_multipart_uploads(Bucket=name).get("Uploads", [])
                stale = [u for u in uploads if u.get("Initiated", datetime.utcnow()).replace(tzinfo=None) < cutoff]
                if not stale:
                    return None
                cost = round(len(stale) * 2.5, 2)
                return Finding(
                    resource_id=name, resource_type="s3_bucket", service=svc, category=cat,
                    title=f"S3 bucket has {len(stale)} stale incomplete multipart uploads",
                    description=f"Incomplete uploads accumulate hidden storage costs. ~${cost}/mo.",
                    severity=SEVERITY_INFO, monthly_cost_usd=cost, estimated_savings_usd=cost,
                    region="us-east-1", metadata={"bucket": name, "stale_uploads": len(stale)},
                )
            except Exception:
                return None
        return self._iter_buckets_concurrent(_check)

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-prod-assets", "s3_bucket", self.service, self.category,
                    "3 stale incomplete multipart uploads", "'ribbon-prod-assets' hidden storage. $7.50/mo.",
                    SEVERITY_INFO, 7.5, 7.5, "us-east-1",
                    metadata={"bucket": "ribbon-prod-assets", "stale_uploads": 3}),
        ]


class S3VersioningNoLifecycleAnalyzer(BaseAnalyzer):
    service = "S3"
    category = CATEGORY_RIGHTSIZE
    IS_GLOBAL = True

    def _live(self) -> List[Finding]:
        svc, cat = self.service, self.category
        def _check(s3, name):
            try:
                if s3.get_bucket_versioning(Bucket=name).get("Status") != "Enabled":
                    return None
                lc = None
                try:
                    lc = s3.get_bucket_lifecycle_configuration(Bucket=name)
                except Exception:
                    pass
                has_version_rule = lc and any(
                    r.get("NoncurrentVersionExpiration") or r.get("NoncurrentVersionTransitions")
                    for r in lc.get("Rules", [])
                )
                if not has_version_rule:
                    return Finding(
                        resource_id=name, resource_type="s3_bucket", service=svc, category=cat,
                        title="S3 versioning enabled — no lifecycle for old versions",
                        description=f"'{name}' accumulates old versions indefinitely. Add NoncurrentVersionExpiration.",
                        severity=SEVERITY_WARNING, monthly_cost_usd=30.0, estimated_savings_usd=20.0,
                        region="us-east-1", metadata={"bucket": name},
                    )
                return None
            except Exception:
                return None
        return self._iter_buckets_concurrent(_check)

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-prod-data", "s3_bucket", self.service, self.category,
                    "Versioning on but no lifecycle for old versions", "'ribbon-prod-data' accumulating versions. $30/mo extra.",
                    SEVERITY_WARNING, 30.0, 20.0, "us-east-1",
                    metadata={"bucket": "ribbon-prod-data"}),
        ]


class S3PublicBucketAnalyzer(BaseAnalyzer):
    service = "S3"
    category = CATEGORY_RIGHTSIZE
    IS_GLOBAL = True

    def _live(self) -> List[Finding]:
        svc, cat = self.service, self.category
        def _check(s3, name):
            try:
                acl = s3.get_bucket_acl(Bucket=name)
                is_public = any(
                    grant.get("Grantee", {}).get("URI", "").endswith(("AllUsers", "AuthenticatedUsers"))
                    for grant in acl.get("Grants", [])
                )
                if not is_public:
                    return None
                return Finding(
                    resource_id=name, resource_type="s3_bucket", service=svc, category=cat,
                    title="S3 bucket is publicly accessible",
                    description=f"'{name}' ACL allows public access. Security risk + potential data exfil cost.",
                    severity=SEVERITY_CRITICAL, monthly_cost_usd=0.0, estimated_savings_usd=0.0,
                    region="us-east-1", metadata={"bucket": name},
                )
            except Exception:
                return None
        return self._iter_buckets_concurrent(_check)

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-ml-datasets-public", "s3_bucket", self.service, self.category,
                    "S3 bucket publicly accessible — SECURITY RISK",
                    "'ribbon-ml-datasets-public' has public-read ACL from a 2023 ML experiment. Contains anonymized transaction data samples. BLOCK PUBLIC ACCESS immediately before AWS reports it.",
                    SEVERITY_CRITICAL, 0.0, 0.0, "us-east-1",
                    tags={"team": "data", "env": "production"},
                    metadata={"bucket": "ribbon-ml-datasets-public", "public_grant": "AllUsers", "size_gb": 8.4}),
        ]


class CloudWatchLogsNoRetentionAnalyzer(BaseAnalyzer):
    service = "CloudWatch Logs"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        logs = self._client("logs")
        findings = []
        paginator = logs.get_paginator("describe_log_groups")
        for page in paginator.paginate():
            for lg in page.get("logGroups", []):
                if lg.get("retentionInDays"):
                    continue
                stored_bytes = lg.get("storedBytes", 0)
                size_gb = round(stored_bytes / 1e9, 2)
                cost = round(size_gb * 0.03, 2)
                findings.append(Finding(
                    resource_id=lg["logGroupName"],
                    resource_type="log_group",
                    service=self.service,
                    category=self.category,
                    title="Log group without retention policy — stored forever",
                    description=f"'{lg['logGroupName']}' {size_gb}GB, no expiry. ~${cost}/mo growing.",
                    severity=_severity_from_savings(cost),
                    monthly_cost_usd=cost,
                    estimated_savings_usd=round(cost * 0.7, 2),
                    region=self._aws.region,
                    metadata={"size_gb": size_gb, "log_group": lg["logGroupName"]},
                ))
        return findings

    def _mock(self) -> List[Finding]:
        groups = [
            ("/aws/lambda/ribbon-payments-processor", 45.0, 12.0),
            ("/aws/lambda/ribbon-notifications", 8.0, 2.0),
            ("/ecs/ribbon-api-prod", 120.0, 40.0),
            ("/aws/rds/instance/ribbon-prod/postgresql", 30.0, 9.0),
        ]
        return [
            Finding(name, "log_group", self.service, self.category,
                    "Log group without retention policy", f"'{name}' stored forever. ${cost}/mo.",
                    _severity_from_savings(cost), cost, savings, "us-east-1",
                    metadata={"log_group": name})
            for name, cost, savings in groups
        ]


class CloudWatchLogsEmptyAnalyzer(BaseAnalyzer):
    service = "CloudWatch Logs"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        logs = self._client("logs")
        findings = []
        cutoff = (datetime.utcnow() - timedelta(days=30)).timestamp() * 1000
        paginator = logs.get_paginator("describe_log_groups")
        for page in paginator.paginate():
            for lg in page.get("logGroups", []):
                if lg.get("storedBytes", 0) > 0:
                    continue
                if lg.get("creationTime", 0) > cutoff:
                    continue
                findings.append(Finding(
                    resource_id=lg["logGroupName"],
                    resource_type="log_group",
                    service=self.service,
                    category=self.category,
                    title="Log group empty for 30+ days",
                    description=f"'{lg['logGroupName']}' has 0 bytes stored. Safe to delete.",
                    severity=SEVERITY_INFO,
                    monthly_cost_usd=0.0,
                    estimated_savings_usd=0.0,
                    region=self._aws.region,
                    metadata={"log_group": lg["logGroupName"]},
                ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("/aws/lambda/ribbon-old-job", "log_group", self.service, self.category,
                    "Log group empty for 30+ days", "Empty log group from deleted Lambda.",
                    SEVERITY_INFO, 0.0, 0.0, "us-east-1",
                    metadata={"log_group": "/aws/lambda/ribbon-old-job"}),
        ]


class CloudWatchLogsOrphanLambdaAnalyzer(BaseAnalyzer):
    service = "CloudWatch Logs"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        logs = self._client("logs")
        lam = self._client("lambda")
        findings = []
        functions = {f["FunctionName"] for f in lam.list_functions().get("Functions", [])}
        paginator = logs.get_paginator("describe_log_groups")
        for page in paginator.paginate(logGroupNamePrefix="/aws/lambda/"):
            for lg in page.get("logGroups", []):
                func_name = lg["logGroupName"].replace("/aws/lambda/", "")
                if func_name not in functions:
                    stored = round(lg.get("storedBytes", 0) / 1e9, 3)
                    cost = round(stored * 0.03, 2)
                    findings.append(Finding(
                        resource_id=lg["logGroupName"],
                        resource_type="log_group",
                        service=self.service,
                        category=self.category,
                        title="Lambda log group for deleted function",
                        description=f"'{func_name}' Lambda no longer exists but log group remains. ${cost}/mo.",
                        severity=SEVERITY_INFO,
                        monthly_cost_usd=cost,
                        estimated_savings_usd=cost,
                        region=self._aws.region,
                        metadata={"function_name": func_name, "log_group": lg["logGroupName"]},
                    ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("/aws/lambda/ribbon-deprecated-cron", "log_group", self.service, self.category,
                    "Lambda log group for deleted function", "'ribbon-deprecated-cron' deleted, logs remain.",
                    SEVERITY_INFO, 0.5, 0.5, "us-east-1",
                    metadata={"function_name": "ribbon-deprecated-cron"}),
        ]


class DynamoDBNoBackupAnalyzer(BaseAnalyzer):
    service = "DynamoDB"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        ddb = self._client("dynamodb")
        findings = []
        tables = ddb.list_tables().get("TableNames", [])
        for table in tables:
            try:
                backup = ddb.describe_continuous_backups(TableName=table)
                pitr = backup.get("ContinuousBackupsDescription", {}).get("PointInTimeRecoveryDescription", {})
                if pitr.get("PointInTimeRecoveryStatus") == "ENABLED":
                    continue
            except Exception:
                pass
            findings.append(Finding(
                resource_id=table,
                resource_type="dynamodb_table",
                service=self.service,
                category=self.category,
                title="DynamoDB table without PITR backup",
                description=f"Table '{table}' has no Point-in-Time Recovery enabled. Data loss risk.",
                severity=SEVERITY_WARNING,
                monthly_cost_usd=0.0,
                estimated_savings_usd=0.0,
                region=self._aws.region,
                metadata={"table": table},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-payments-events", "dynamodb_table", self.service, self.category,
                    "DynamoDB table without PITR backup", "'ribbon-payments-events' has no backup. Data risk.",
                    SEVERITY_WARNING, 0.0, 0.0, "us-east-1",
                    metadata={"table": "ribbon-payments-events"}),
        ]


class CloudFrontDisabledAnalyzer(BaseAnalyzer):
    service = "CloudFront"
    category = CATEGORY_CLEANUP
    IS_GLOBAL = True

    def _live(self) -> List[Finding]:
        cf = self._client("cloudfront")
        findings = []
        resp = cf.list_distributions()
        items = resp.get("DistributionList", {}).get("Items", [])
        for dist in items:
            if dist.get("Enabled", True):
                continue
            findings.append(Finding(
                resource_id=dist["Id"],
                resource_type="cloudfront_distribution",
                service=self.service,
                category=self.category,
                title="CloudFront distribution disabled but still exists",
                description=f"Distribution '{dist.get('Comment', dist['Id'])}' disabled. Delete if no longer needed.",
                severity=SEVERITY_INFO,
                monthly_cost_usd=5.0,
                estimated_savings_usd=5.0,
                region="us-east-1",
                metadata={"domain": dist.get("DomainName"), "origins": len(dist.get("Origins", {}).get("Items", []))},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("E1ABCDEF123456", "cloudfront_distribution", self.service, self.category,
                    "CloudFront distribution disabled — staging frontend",
                    "Distribution for old staging frontend disabled 60d ago. Delete after confirming migration complete.",
                    SEVERITY_INFO, 5.0, 5.0, "us-east-1", idle_days=60,
                    tags={"env": "staging", "team": "growth"},
                    metadata={"domain": "d1abcdefghijk.cloudfront.net", "origins": 1, "comment": "ribbon-staging-frontend-old"}),
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# TIER 2 — CloudWatch metrics required
# ═══════════════════════════════════════════════════════════════════════════════

class RDSIdleAnalyzer(BaseAnalyzer):
    service = "RDS"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        rds = self._client("rds")
        findings = []
        resp = rds.describe_db_instances()
        for inst in resp.get("DBInstances", []):
            if inst.get("DBInstanceStatus") != "available":
                continue
            db_id = inst["DBInstanceIdentifier"]
            avg_connections = self._cw_avg(
                "AWS/RDS", "DatabaseConnections",
                [{"Name": "DBInstanceIdentifier", "Value": db_id}],
                days=7,
            )
            if avg_connections >= 1:
                continue
            cls = inst.get("DBInstanceClass", "db.t3.medium")
            cost = _rds_monthly(cls)
            tags = {t["Key"]: t["Value"] for t in inst.get("TagList", [])}
            findings.append(Finding(
                resource_id=db_id,
                resource_type="rds_instance",
                service=self.service,
                category=self.category,
                title=f"RDS idle — 0 connections in last 7 days",
                description=f"{cls} with 0 avg connections 7d. ${cost}/mo wasted.",
                severity=_severity_from_savings(cost),
                monthly_cost_usd=cost,
                estimated_savings_usd=cost,
                region=self._aws.region,
                idle_days=7,
                tags=tags,
                metadata={"instance_class": cls, "avg_connections_7d": avg_connections},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("db-analytics-staging", "rds_instance", self.service, self.category,
                    "RDS idle — 0 connections in last 7 days",
                    "db.r5.xlarge 'db-analytics-staging' with 0 avg connections in 7d. Provisioned for a reporting feature not yet launched. $364/mo wasted.",
                    SEVERITY_CRITICAL, 364.0, 364.0, "us-east-1", idle_days=14,
                    tags={"env": "staging", "team": "data"},
                    metadata={"instance_class": "db.r5.xlarge", "avg_connections_7d": 0, "engine": "postgres"}),
            Finding("db-loadtest-replica", "rds_instance", self.service, self.category,
                    "RDS idle — 0 connections in last 7 days",
                    "db.m5.xlarge 'db-loadtest-replica' created for load test 30d ago, never cleaned up. $276/mo wasted.",
                    SEVERITY_CRITICAL, 276.0, 276.0, "us-east-1", idle_days=30,
                    tags={"env": "staging", "team": "platform"},
                    metadata={"instance_class": "db.m5.xlarge", "avg_connections_7d": 0, "engine": "mysql"}),
        ]


class RDSOversizedAnalyzer(BaseAnalyzer):
    service = "RDS"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        rds = self._client("rds")
        findings = []
        for inst in rds.describe_db_instances().get("DBInstances", []):
            if inst.get("DBInstanceStatus") != "available":
                continue
            db_id = inst["DBInstanceIdentifier"]
            cpu = self._cw_avg("AWS/RDS", "CPUUtilization",
                               [{"Name": "DBInstanceIdentifier", "Value": db_id}], days=7)
            free_mem = self._cw_avg("AWS/RDS", "FreeableMemory",
                                    [{"Name": "DBInstanceIdentifier", "Value": db_id}], days=7)
            if cpu == 0 and free_mem == 0:
                continue
            if cpu > 10:
                continue
            cls = inst.get("DBInstanceClass", "db.t3.medium")
            cost = _rds_monthly(cls)
            savings = round(cost * 0.5, 2)
            tags = {t["Key"]: t["Value"] for t in inst.get("TagList", [])}
            findings.append(Finding(
                resource_id=db_id,
                resource_type="rds_instance",
                service=self.service,
                category=self.category,
                title=f"RDS oversized — {cpu:.1f}% CPU avg (7d)",
                description=f"{cls} at {cpu:.1f}% CPU. Downsize to save ~${savings}/mo.",
                severity=_severity_from_savings(savings),
                monthly_cost_usd=cost,
                estimated_savings_usd=savings,
                region=self._aws.region,
                tags=tags,
                metadata={"instance_class": cls, "cpu_avg_7d": round(cpu, 1), "free_mem_bytes": free_mem},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-prod-postgres-primary", "rds_instance", self.service, self.category,
                    "RDS oversized — 4.2% CPU avg (7d)",
                    "db.r5.2xlarge 'ribbon-prod-postgres-primary' at 4.2% CPU avg 7d, 12% peak. Downsize to db.r5.xlarge. Save $364/mo. Take snapshot before change.",
                    SEVERITY_CRITICAL, 728.0, 364.0, "us-east-1",
                    tags={"env": "production", "team": "platform"},
                    metadata={"instance_class": "db.r5.2xlarge", "cpu_avg_7d": 4.2, "cpu_peak_7d": 12.0, "target": "db.r5.xlarge", "engine": "postgres 15"}),
            Finding("ribbon-prod-mysql", "rds_instance", self.service, self.category,
                    "RDS oversized — 6.8% CPU avg (7d)",
                    "db.r5.xlarge 'ribbon-prod-mysql' at 6.8% CPU avg 7d. Downsize to db.r5.large. Save $182/mo.",
                    SEVERITY_CRITICAL, 364.0, 182.0, "us-east-1",
                    tags={"env": "production", "team": "growth"},
                    metadata={"instance_class": "db.r5.xlarge", "cpu_avg_7d": 6.8, "target": "db.r5.large", "engine": "mysql 8"}),
        ]


class EC2LowCPUAnalyzer(BaseAnalyzer):
    service = "EC2"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        resp = ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running"]}])
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                iid = inst["InstanceId"]
                cpu = self._cw_avg("AWS/EC2", "CPUUtilization",
                                   [{"Name": "InstanceId", "Value": iid}], days=7)
                if cpu == 0 or cpu >= 10:
                    continue
                itype = inst.get("InstanceType", "unknown")
                cost = _ec2_monthly(itype)
                savings = round(cost * 0.5, 2)
                severity = SEVERITY_CRITICAL if cpu < 5 else SEVERITY_WARNING
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                name = tags.get("Name", iid)
                findings.append(Finding(
                    resource_id=iid,
                    resource_type="ec2_instance",
                    service=self.service,
                    category=self.category,
                    title=f"EC2 low CPU — {cpu:.1f}% avg (7d)",
                    description=f"'{name}' ({itype}) at {cpu:.1f}% CPU. Downsize or migrate to Graviton. Save ~${savings}/mo.",
                    severity=severity,
                    monthly_cost_usd=cost,
                    estimated_savings_usd=savings,
                    region=self._aws.region,
                    tags=tags,
                    metadata={"instance_type": itype, "cpu_avg_7d": round(cpu, 1), "name": name},
                ))
        return findings

    def _mock(self) -> List[Finding]:
        instances = [
            ("i-0lowcpu001", "eks-node-stg-1", "t3.large", 4.2, 60.0, 30.0),
            ("i-0lowcpu002", "eks-node-stg-2", "t3.large", 7.8, 60.0, 25.0),
            ("i-0lowcpu003", "bastion-old", "m5.xlarge", 2.1, 140.0, 100.0),
        ]
        return [
            Finding(iid, "ec2_instance", self.service, self.category,
                    f"EC2 low CPU — {cpu:.1f}% avg (7d)",
                    f"'{name}' ({itype}) at {cpu:.1f}% CPU. ~${savings}/mo savings.",
                    SEVERITY_CRITICAL if cpu < 5 else SEVERITY_WARNING,
                    cost, savings, "us-east-1",
                    tags={"env": "staging"}, metadata={"instance_type": itype, "cpu_avg_7d": cpu, "name": name})
            for iid, name, itype, cpu, cost, savings in instances
        ]


class ElastiCacheIdleAnalyzer(BaseAnalyzer):
    service = "ElastiCache"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        ec = self._client("elasticache")
        findings = []
        for cluster in ec.describe_cache_clusters().get("CacheClusters", []):
            cid = cluster["CacheClusterId"]
            conns = self._cw_avg("AWS/ElastiCache", "CurrConnections",
                                 [{"Name": "CacheClusterId", "Value": cid}], days=14)
            if conns > 0:
                continue
            node_type = cluster.get("CacheNodeType", "cache.t3.micro")
            cost = _cache_monthly(node_type)
            findings.append(Finding(
                resource_id=cid,
                resource_type="elasticache_cluster",
                service=self.service,
                category=self.category,
                title="ElastiCache cluster — 0 connections in 14 days",
                description=f"{node_type} with zero connections 14d. ${cost}/mo wasted.",
                severity=_severity_from_savings(cost),
                monthly_cost_usd=cost,
                estimated_savings_usd=cost,
                region=self._aws.region,
                idle_days=14,
                metadata={"node_type": node_type, "engine": cluster.get("Engine")},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-dev-redis", "elasticache_cluster", self.service, self.category,
                    "ElastiCache — 0 connections in 14 days",
                    "cache.m5.large 'ribbon-dev-redis' with 0 connections for 14d. Dev environment Redis left running. $96/mo.",
                    SEVERITY_WARNING, 96.0, 96.0, "us-east-1", idle_days=14,
                    tags={"env": "dev", "team": "platform"}, metadata={"node_type": "cache.m5.large", "engine": "redis"}),
            Finding("ribbon-loadtest-memcache", "elasticache_cluster", self.service, self.category,
                    "ElastiCache — 0 connections in 21 days",
                    "cache.r5.large 'ribbon-loadtest-memcache' created for load test, 0 connections 21d. $148/mo.",
                    SEVERITY_CRITICAL, 148.0, 148.0, "us-east-1", idle_days=21,
                    tags={"env": "staging", "team": "growth"}, metadata={"node_type": "cache.r5.large", "engine": "memcached"}),
        ]


class ElastiCacheOversizedAnalyzer(BaseAnalyzer):
    service = "ElastiCache"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        ec = self._client("elasticache")
        findings = []
        for cluster in ec.describe_cache_clusters().get("CacheClusters", []):
            cid = cluster["CacheClusterId"]
            free_mem = self._cw_avg("AWS/ElastiCache", "FreeableMemory",
                                    [{"Name": "CacheClusterId", "Value": cid}], days=7)
            if free_mem == 0:
                continue
            node_type = cluster.get("CacheNodeType", "cache.t3.micro")
            total_mem_map = {"cache.t3.micro": 512, "cache.m5.large": 6742, "cache.m6g.large": 6742,
                             "cache.r5.large": 13600, "cache.r6g.large": 13600}
            total_mem = total_mem_map.get(node_type, 4000) * 1024 * 1024
            free_pct = (free_mem / total_mem * 100) if total_mem > 0 else 0
            if free_pct < 80:
                continue
            cost = _cache_monthly(node_type)
            savings = round(cost * 0.4, 2)
            findings.append(Finding(
                resource_id=cid,
                resource_type="elasticache_cluster",
                service=self.service,
                category=self.category,
                title=f"ElastiCache oversized — {free_pct:.0f}% memory free",
                description=f"{node_type} with {free_pct:.0f}% free memory. Downsize to save ~${savings}/mo.",
                severity=_severity_from_savings(savings),
                monthly_cost_usd=cost,
                estimated_savings_usd=savings,
                region=self._aws.region,
                metadata={"node_type": node_type, "free_mem_pct": round(free_pct, 1)},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-prod-redis", "elasticache_cluster", self.service, self.category,
                    "ElastiCache oversized — 84% memory free", "cache.r6g.large, 84% free. Downsize saves $50/mo.",
                    SEVERITY_WARNING, 125.0, 50.0, "us-east-1",
                    metadata={"node_type": "cache.r6g.large", "free_mem_pct": 84.0}),
        ]


class ElastiCacheNoReplicaReadsAnalyzer(BaseAnalyzer):
    service = "ElastiCache"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        ec = self._client("elasticache")
        findings = []
        for rg in ec.describe_replication_groups().get("ReplicationGroups", []):
            members = rg.get("MemberClusters", [])
            if len(members) < 2:
                continue
            replicas = members[1:]
            all_idle = True
            for replica in replicas:
                gets = self._cw_avg("AWS/ElastiCache", "GetTypeCmds",
                                    [{"Name": "CacheClusterId", "Value": replica}], days=7)
                if gets > 0:
                    all_idle = False
                    break
            if not all_idle:
                continue
            findings.append(Finding(
                resource_id=rg["ReplicationGroupId"],
                resource_type="elasticache_replication_group",
                service=self.service,
                category=self.category,
                title="ElastiCache replicas with 0 reads — not being used",
                description=f"Replication group '{rg['ReplicationGroupId']}' has {len(replicas)} idle replicas.",
                severity=SEVERITY_WARNING,
                monthly_cost_usd=100.0,
                estimated_savings_usd=50.0,
                region=self._aws.region,
                metadata={"replica_count": len(replicas)},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-staging-redis-rg", "elasticache_replication_group", self.service, self.category,
                    "ElastiCache replicas with 0 reads — not being used",
                    "'ribbon-staging-redis-rg' has 2 read replicas with 0 GetType commands 7d. App reads directly from primary only.",
                    SEVERITY_WARNING, 200.0, 100.0, "us-east-1",
                    tags={"env": "staging"},
                    metadata={"replica_count": 2, "recommendation": "Remove read replicas or configure app read endpoint"}),
        ]


class LambdaUnusedAnalyzer(BaseAnalyzer):
    service = "Lambda"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        lam = self._client("lambda")
        findings = []
        paginator = lam.get_paginator("list_functions")
        for page in paginator.paginate():
            for func in page.get("Functions", []):
                name = func["FunctionName"]
                invocations = self._cw_avg("AWS/Lambda", "Invocations",
                                           [{"Name": "FunctionName", "Value": name}],
                                           days=90, stat="Sum")
                if invocations and invocations > 0:
                    continue
                memory_mb = func.get("MemorySize", 128)
                cost = round(memory_mb * 0.0001, 2)
                findings.append(Finding(
                    resource_id=name,
                    resource_type="lambda_function",
                    service=self.service,
                    category=self.category,
                    title="Lambda function — 0 invocations in 90 days",
                    description=f"'{name}' not invoked in 90d. Still paying for provisioned concurrency if set.",
                    severity=SEVERITY_INFO,
                    monthly_cost_usd=cost,
                    estimated_savings_usd=cost,
                    region=self._aws.region,
                    idle_days=90,
                    metadata={"memory_mb": memory_mb, "runtime": func.get("Runtime")},
                ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-old-migration-job", "lambda_function", self.service, self.category,
                    "Lambda — 0 invocations in 90 days", "'ribbon-old-migration-job' not invoked 90d. One-time migration completed Q1.",
                    SEVERITY_INFO, 0.5, 0.5, "us-east-1", idle_days=90,
                    tags={"team": "platform"}, metadata={"memory_mb": 512, "runtime": "python3.9"}),
            Finding("ribbon-test-webhook", "lambda_function", self.service, self.category,
                    "Lambda — 0 invocations in 90 days", "'ribbon-test-webhook' unused 90d. Dev test function left deployed.",
                    SEVERITY_INFO, 0.1, 0.1, "us-east-1", idle_days=90,
                    tags={"env": "dev"}, metadata={"memory_mb": 128, "runtime": "nodejs18.x"}),
            Finding("ribbon-sms-fallback-v1", "lambda_function", self.service, self.category,
                    "Lambda — 0 invocations in 90 days", "'ribbon-sms-fallback-v1' unused 90d. Replaced by v2 with SNS.",
                    SEVERITY_INFO, 0.3, 0.3, "us-east-1", idle_days=90,
                    tags={"team": "payments"}, metadata={"memory_mb": 256, "runtime": "python3.11"}),
            Finding("ribbon-report-scheduler-cron", "lambda_function", self.service, self.category,
                    "Lambda — 0 invocations in 90 days", "'ribbon-report-scheduler-cron' unused 90d. Reports now generated by ECS batch job.",
                    SEVERITY_INFO, 0.2, 0.2, "us-east-1", idle_days=90,
                    tags={"team": "data"}, metadata={"memory_mb": 256, "runtime": "python3.10"}),
        ]


class LambdaHighErrorRateAnalyzer(BaseAnalyzer):
    service = "Lambda"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        lam = self._client("lambda")
        findings = []
        for func in lam.list_functions().get("Functions", []):
            name = func["FunctionName"]
            errors = self._cw_avg("AWS/Lambda", "Errors",
                                  [{"Name": "FunctionName", "Value": name}], days=7, stat="Sum")
            invocations = self._cw_avg("AWS/Lambda", "Invocations",
                                       [{"Name": "FunctionName", "Value": name}], days=7, stat="Sum")
            if not invocations or invocations == 0:
                continue
            error_rate = errors / invocations * 100
            if error_rate < 50:
                continue
            findings.append(Finding(
                resource_id=name,
                resource_type="lambda_function",
                service=self.service,
                category=self.category,
                title=f"Lambda high error rate — {error_rate:.0f}% (7d)",
                description=f"'{name}' failing {error_rate:.0f}% of invocations. Fix or disable.",
                severity=SEVERITY_CRITICAL,
                monthly_cost_usd=5.0,
                estimated_savings_usd=0.0,
                region=self._aws.region,
                metadata={"error_rate_pct": round(error_rate, 1), "errors_7d": errors, "invocations_7d": invocations},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-payment-retry", "lambda_function", self.service, self.category,
                    "Lambda high error rate — 73% (7d)",
                    "'ribbon-payment-retry' failing 73% of calls over 7d. Likely an upstream API timeout. Fix or disable to stop CloudWatch Logs cost growth.",
                    SEVERITY_CRITICAL, 5.0, 0.0, "us-east-1",
                    tags={"team": "payments", "env": "production"},
                    metadata={"error_rate_pct": 73.0, "errors_7d": 1460, "invocations_7d": 2000, "p99_duration_ms": 29800}),
            Finding("ribbon-fraud-score-enricher", "lambda_function", self.service, self.category,
                    "Lambda high error rate — 58% (7d)",
                    "'ribbon-fraud-score-enricher' failing 58% of calls. DynamoDB throttling errors. Add capacity or enable on-demand mode.",
                    SEVERITY_CRITICAL, 12.0, 0.0, "us-east-1",
                    tags={"team": "payments", "env": "production"},
                    metadata={"error_rate_pct": 58.0, "errors_7d": 580, "invocations_7d": 1000, "error_type": "DynamoDB ProvisionedThroughputExceededException"}),
        ]


class NATLowTrafficAnalyzer(BaseAnalyzer):
    service = "NAT Gateway"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        nats = ec2.describe_nat_gateways(Filters=[{"Name": "state", "Values": ["available"]}]).get("NatGateways", [])
        for nat in nats:
            nat_id = nat["NatGatewayId"]
            bytes_out = self._cw_avg("AWS/NATGateway", "BytesOutToDestination",
                                     [{"Name": "NatGatewayId", "Value": nat_id}], days=7, stat="Sum")
            gb_per_day = bytes_out / 1e9 / 7 if bytes_out else 0
            if gb_per_day >= 1:
                continue
            cost = round(NAT_HOUR * 730, 2)
            # Zero traffic for 7 days → cleanup candidate (delete it); low traffic → rightsize
            is_dead = gb_per_day == 0
            category = CATEGORY_CLEANUP if is_dead else CATEGORY_RIGHTSIZE
            severity = SEVERITY_CRITICAL if is_dead else SEVERITY_WARNING
            savings = cost if is_dead else round(cost * 0.6, 2)
            title = (f"NAT Gateway idle — 0 traffic for 7+ days" if is_dead
                     else f"NAT Gateway low traffic — {gb_per_day:.2f}GB/day avg")
            description = (f"NAT {nat_id} has had zero traffic for 7+ days. ${cost}/mo wasted. Safe to delete."
                           if is_dead
                           else f"NAT {nat_id} processing <1GB/day. ${cost}/mo base cost. Consider VPC Endpoints.")
            findings.append(Finding(
                resource_id=nat_id,
                resource_type="nat_gateway",
                service=self.service,
                category=category,
                title=title,
                description=description,
                severity=severity,
                monthly_cost_usd=cost,
                estimated_savings_usd=savings,
                region=self._aws.region,
                metadata={"gb_per_day": round(gb_per_day, 3), "subnet_id": nat.get("SubnetId"), "idle": is_dead},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("nat-0lowtraffic001", "nat_gateway", self.service, self.category,
                    "NAT Gateway low traffic — 0.12GB/day avg", "NAT in staging, 0.12GB/day. $32/mo.",
                    SEVERITY_WARNING, 32.85, 19.0, "us-east-1",
                    metadata={"gb_per_day": 0.12, "subnet_id": "subnet-staging-1a"}),
        ]


class NATMultipleSameAZAnalyzer(BaseAnalyzer):
    service = "NAT Gateway"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        nats = ec2.describe_nat_gateways(Filters=[{"Name": "state", "Values": ["available"]}]).get("NatGateways", [])
        subnets_resp = ec2.describe_subnets()
        subnet_az = {s["SubnetId"]: s["AvailabilityZone"] for s in subnets_resp.get("Subnets", [])}
        az_nats: Dict[str, List] = {}
        for nat in nats:
            az = subnet_az.get(nat.get("SubnetId", ""), "unknown")
            az_nats.setdefault(az, []).append(nat)
        for az, nat_list in az_nats.items():
            if len(nat_list) < 2:
                continue
            cost_each = round(NAT_HOUR * 730, 2)
            savings = cost_each * (len(nat_list) - 1)
            findings.append(Finding(
                resource_id=f"nat-multiple-{az}",
                resource_type="nat_gateway",
                service=self.service,
                category=self.category,
                title=f"{len(nat_list)} NAT Gateways in same AZ ({az})",
                description=f"Redundant NATs in {az}. Keep 1, remove {len(nat_list)-1}. Save ~${savings}/mo.",
                severity=_severity_from_savings(savings),
                monthly_cost_usd=cost_each * len(nat_list),
                estimated_savings_usd=savings,
                region=self._aws.region,
                metadata={"az": az, "nat_count": len(nat_list), "nat_ids": [n["NatGatewayId"] for n in nat_list]},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        cost_each = round(NAT_HOUR * 730, 2)
        return [
            Finding("nat-multiple-us-east-1a", "nat_gateway", self.service, self.category,
                    "2 NAT Gateways in same AZ (us-east-1a) — redundant",
                    "nat-0prod001 and nat-0prod002 both in us-east-1a. Created during DR test, second one never removed. Remove one. Save $32.85/mo.",
                    SEVERITY_WARNING, cost_each * 2, cost_each, "us-east-1",
                    metadata={"az": "us-east-1a", "nat_count": 2,
                              "nat_ids": ["nat-0prod001", "nat-0prod002"]}),
        ]


class NATVPCEndpointOpportunityAnalyzer(BaseAnalyzer):
    service = "NAT Gateway"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        nats = ec2.describe_nat_gateways(Filters=[{"Name": "state", "Values": ["available"]}]).get("NatGateways", [])
        endpoints = ec2.describe_vpc_endpoints().get("VpcEndpoints", [])
        s3_endpoint_vpcs = {ep["VpcId"] for ep in endpoints if "s3" in ep.get("ServiceName", "").lower()}
        for nat in nats:
            vpc_id = nat.get("VpcId")
            if vpc_id in s3_endpoint_vpcs:
                continue
            bytes_out = self._cw_avg("AWS/NATGateway", "BytesOutToDestination",
                                     [{"Name": "NatGatewayId", "Value": nat["NatGatewayId"]}],
                                     days=7, stat="Sum")
            if not bytes_out or bytes_out < 1e9:
                continue
            savings = round(bytes_out / 1e9 * 0.045, 2)
            findings.append(Finding(
                resource_id=nat["NatGatewayId"],
                resource_type="nat_gateway",
                service=self.service,
                category=self.category,
                title="NAT traffic to S3/DynamoDB — VPC Endpoint opportunity",
                description=f"Traffic through NAT could use VPC Endpoint (free for S3/DynamoDB). Save ~${savings}/mo.",
                severity=_severity_from_savings(savings),
                monthly_cost_usd=round(bytes_out / 1e9 * 0.045 * 2, 2),
                estimated_savings_usd=savings,
                region=self._aws.region,
                metadata={"vpc_id": vpc_id, "bytes_out_gb_7d": round(bytes_out / 1e9, 2)},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("nat-0prod001", "nat_gateway", self.service, self.category,
                    "NAT traffic to S3 — VPC Endpoint opportunity", "60GB/week S3 traffic via NAT. Add endpoint saves $97/mo.",
                    SEVERITY_WARNING, 194.0, 97.0, "us-east-1",
                    metadata={"vpc_id": "vpc-prod", "bytes_out_gb_7d": 60}),
        ]


class NATCrossAZAnalyzer(BaseAnalyzer):
    service = "NAT Gateway"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        return []

    def _mock(self) -> List[Finding]:
        return [
            Finding("nat-crossaz-us-east-1", "nat_gateway", self.service, self.category,
                    "Cross-AZ traffic through NAT Gateway — avoidable", "Instances in us-east-1b routing via NAT in us-east-1a. $142/mo cross-AZ.",
                    SEVERITY_WARNING, 142.0, 100.0, "us-east-1",
                    metadata={"cross_az_gb_weekly": 142, "recommendation": "Add NAT per AZ or use VPC Endpoints"}),
        ]


class EBSNoIOPSAnalyzer(BaseAnalyzer):
    service = "EBS"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        vols = ec2.describe_volumes(Filters=[{"Name": "status", "Values": ["in-use"]}]).get("Volumes", [])
        for vol in vols:
            vid = vol["VolumeId"]
            reads = self._cw_avg("AWS/EBS", "VolumeReadOps",
                                 [{"Name": "VolumeId", "Value": vid}], days=7, stat="Sum")
            writes = self._cw_avg("AWS/EBS", "VolumeWriteOps",
                                  [{"Name": "VolumeId", "Value": vid}], days=7, stat="Sum")
            if reads > 0 or writes > 0:
                continue
            size = vol.get("Size", 0)
            vtype = vol.get("VolumeType", "gp2")
            cost = _ebs_monthly(vtype, size)
            tags = {t["Key"]: t["Value"] for t in vol.get("Tags", [])}
            findings.append(Finding(
                resource_id=vid,
                resource_type="ebs_volume",
                service=self.service,
                category=self.category,
                title="EBS volume — 0 IOPS in last 7 days",
                description=f"{size}GB {vtype} with no I/O 7d. ${cost}/mo.",
                severity=_severity_from_savings(cost),
                monthly_cost_usd=cost,
                estimated_savings_usd=cost,
                region=self._aws.region,
                idle_days=7,
                tags=tags,
                metadata={"size_gb": size, "volume_type": vtype},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("vol-0noiops001", "ebs_volume", self.service, self.category,
                    "EBS volume — 0 IOPS in last 7 days", "300GB gp2, no I/O 7d. $30/mo.",
                    SEVERITY_WARNING, 30.0, 30.0, "us-east-1", idle_days=7,
                    metadata={"size_gb": 300, "volume_type": "gp2"}),
        ]


class DynamoDBIdleAnalyzer(BaseAnalyzer):
    service = "DynamoDB"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        ddb = self._client("dynamodb")
        findings = []
        for table in ddb.list_tables().get("TableNames", []):
            reads = self._cw_avg("AWS/DynamoDB", "ConsumedReadCapacityUnits",
                                 [{"Name": "TableName", "Value": table}], days=30, stat="Sum")
            writes = self._cw_avg("AWS/DynamoDB", "ConsumedWriteCapacityUnits",
                                  [{"Name": "TableName", "Value": table}], days=30, stat="Sum")
            if reads > 0 or writes > 0:
                continue
            try:
                desc = ddb.describe_table(TableName=table)["Table"]
                billing = desc.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
                rcu = desc.get("ProvisionedThroughput", {}).get("ReadCapacityUnits", 0)
                wcu = desc.get("ProvisionedThroughput", {}).get("WriteCapacityUnits", 0)
                cost = round((rcu + wcu) * 0.00065 * 730, 2)
            except Exception:
                cost = 5.0
            findings.append(Finding(
                resource_id=table,
                resource_type="dynamodb_table",
                service=self.service,
                category=self.category,
                title="DynamoDB table — 0 reads/writes in 30 days",
                description=f"Table '{table}' idle 30d. ${cost}/mo wasted.",
                severity=_severity_from_savings(cost),
                monthly_cost_usd=cost,
                estimated_savings_usd=cost,
                region=self._aws.region,
                idle_days=30,
                metadata={"table": table},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-feature-flags-v1", "dynamodb_table", self.service, self.category,
                    "DynamoDB table idle 30 days", "'ribbon-feature-flags-v1' replaced by v2, idle. $12/mo.",
                    SEVERITY_INFO, 12.0, 12.0, "us-east-1", idle_days=30,
                    metadata={"table": "ribbon-feature-flags-v1"}),
        ]


class DynamoDBGSIUnusedAnalyzer(BaseAnalyzer):
    service = "DynamoDB"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        ddb = self._client("dynamodb")
        findings = []
        for table in ddb.list_tables().get("TableNames", []):
            try:
                desc = ddb.describe_table(TableName=table)["Table"]
                for gsi in desc.get("GlobalSecondaryIndexes", []):
                    idx_name = gsi["IndexName"]
                    reads = self._cw_avg("AWS/DynamoDB", "ConsumedReadCapacityUnits",
                                         [{"Name": "TableName", "Value": table},
                                          {"Name": "GlobalSecondaryIndexName", "Value": idx_name}],
                                         days=30, stat="Sum")
                    if reads > 0:
                        continue
                    rcu = gsi.get("ProvisionedThroughput", {}).get("ReadCapacityUnits", 5)
                    cost = round(rcu * 0.00065 * 730, 2)
                    findings.append(Finding(
                        resource_id=f"{table}/{idx_name}",
                        resource_type="dynamodb_gsi",
                        service=self.service,
                        category=self.category,
                        title=f"DynamoDB GSI unused — '{idx_name}'",
                        description=f"GSI '{idx_name}' on '{table}' with 0 reads 30d. ${cost}/mo.",
                        severity=_severity_from_savings(cost),
                        monthly_cost_usd=cost,
                        estimated_savings_usd=cost,
                        region=self._aws.region,
                        idle_days=30,
                        metadata={"table": table, "gsi": idx_name},
                    ))
            except Exception:
                continue
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-payments-events/gsi-status-createdAt", "dynamodb_gsi", self.service, self.category,
                    "DynamoDB GSI unused — 'gsi-status-createdAt'",
                    "GSI 'gsi-status-createdAt' on 'ribbon-payments-events' with 0 reads 30d. Created for a dashboard query that was moved to the analytics DB. $2.38/mo.",
                    SEVERITY_INFO, 2.38, 2.38, "us-east-1", idle_days=30,
                    tags={"team": "payments"},
                    metadata={"table": "ribbon-payments-events", "gsi": "gsi-status-createdAt", "rcu": 5}),
            Finding("ribbon-users/gsi-email-index-old", "dynamodb_gsi", self.service, self.category,
                    "DynamoDB GSI unused — 'gsi-email-index-old'",
                    "GSI 'gsi-email-index-old' on 'ribbon-users' — replaced by 'gsi-email-v2'. 0 reads 30d. $9.50/mo.",
                    SEVERITY_INFO, 9.50, 9.50, "us-east-1", idle_days=45,
                    tags={"team": "platform"},
                    metadata={"table": "ribbon-users", "gsi": "gsi-email-index-old", "rcu": 20}),
        ]


class DynamoDBProvisionedAnalyzer(BaseAnalyzer):
    service = "DynamoDB"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        ddb = self._client("dynamodb")
        findings = []
        for table in ddb.list_tables().get("TableNames", []):
            try:
                desc = ddb.describe_table(TableName=table)["Table"]
                if desc.get("BillingModeSummary", {}).get("BillingMode") == "PAY_PER_REQUEST":
                    continue
                rcu = desc.get("ProvisionedThroughput", {}).get("ReadCapacityUnits", 0)
                wcu = desc.get("ProvisionedThroughput", {}).get("WriteCapacityUnits", 0)
                prov_cost = round((rcu + wcu) * 0.00065 * 730, 2)
                reads = self._cw_avg("AWS/DynamoDB", "ConsumedReadCapacityUnits",
                                     [{"Name": "TableName", "Value": table}], days=30, stat="Sum")
                writes = self._cw_avg("AWS/DynamoDB", "ConsumedWriteCapacityUnits",
                                      [{"Name": "TableName", "Value": table}], days=30, stat="Sum")
                ondemand_cost = round(((reads or 0) * 0.000000125 + (writes or 0) * 0.000000625) * 4, 2)
                if ondemand_cost >= prov_cost * 0.8:
                    continue
                savings = round(prov_cost - ondemand_cost, 2)
                findings.append(Finding(
                    resource_id=table,
                    resource_type="dynamodb_table",
                    service=self.service,
                    category=self.category,
                    title="DynamoDB PROVISIONED mode — PAY_PER_REQUEST would be cheaper",
                    description=f"Table '{table}': provisioned ${prov_cost}/mo vs on-demand ~${ondemand_cost}/mo. Save ${savings}/mo.",
                    severity=_severity_from_savings(savings),
                    monthly_cost_usd=prov_cost,
                    estimated_savings_usd=savings,
                    region=self._aws.region,
                    metadata={"table": table, "rcu": rcu, "wcu": wcu},
                ))
            except Exception:
                continue
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-sessions", "dynamodb_table", self.service, self.category,
                    "DynamoDB PROVISIONED — PAY_PER_REQUEST cheaper", "'ribbon-sessions' provisioned $47/mo vs on-demand $18/mo.",
                    SEVERITY_WARNING, 47.0, 29.0, "us-east-1",
                    metadata={"table": "ribbon-sessions", "rcu": 50, "wcu": 10}),
        ]


class S3EmptyBucketAnalyzer(BaseAnalyzer):
    service = "S3"
    category = CATEGORY_CLEANUP
    IS_GLOBAL = True

    def _live(self) -> List[Finding]:
        svc, cat = self.service, self.category
        cw = self._client("cloudwatch")
        end = datetime.utcnow()
        start = end - timedelta(days=2)
        def _check(s3, name):
            try:
                resp = cw.get_metric_statistics(
                    Namespace="AWS/S3", MetricName="BucketSizeBytes",
                    Dimensions=[{"Name": "BucketName", "Value": name},
                                {"Name": "StorageType", "Value": "StandardStorage"}],
                    StartTime=start, EndTime=end, Period=86400, Statistics=["Average"],
                )
                pts = resp.get("Datapoints", [])
                if pts and pts[0].get("Average", 1) > 0:
                    return None
                return Finding(
                    resource_id=name, resource_type="s3_bucket", service=svc, category=cat,
                    title="S3 bucket empty",
                    description=f"Bucket '{name}' has 0 bytes. Safe to delete if not needed.",
                    severity=SEVERITY_INFO, monthly_cost_usd=0.0, estimated_savings_usd=0.0,
                    region="us-east-1", metadata={"bucket": name},
                )
            except Exception:
                return None
        return self._iter_buckets_concurrent(_check)

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-test-uploads-2024", "s3_bucket", self.service, self.category,
                    "S3 bucket empty", "'ribbon-test-uploads-2024' has 0 bytes.",
                    SEVERITY_INFO, 0.0, 0.0, "us-east-1",
                    metadata={"bucket": "ribbon-test-uploads-2024"}),
        ]


class Route53IdleAnalyzer(BaseAnalyzer):
    service = "Route53"
    category = CATEGORY_CLEANUP
    IS_GLOBAL = True

    def _live(self) -> List[Finding]:
        r53 = self._client("route53")
        findings = []
        zones = r53.list_hosted_zones().get("HostedZones", [])
        for zone in zones:
            zone_id = zone["Id"].split("/")[-1]
            queries = self._cw_avg("AWS/Route53", "DNSQueries",
                                   [{"Name": "HostedZoneId", "Value": zone_id}],
                                   days=30, stat="Sum")
            if queries and queries > 0:
                continue
            findings.append(Finding(
                resource_id=zone_id,
                resource_type="route53_hosted_zone",
                service=self.service,
                category=self.category,
                title="Route53 hosted zone — 0 queries in 30 days",
                description=f"Zone '{zone.get('Name')}' idle 30d. $0.50/mo.",
                severity=SEVERITY_INFO,
                monthly_cost_usd=0.50,
                estimated_savings_usd=0.50,
                region="us-east-1",
                idle_days=30,
                metadata={"zone_name": zone.get("Name"), "record_count": zone.get("ResourceRecordSetCount", 0)},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("Z1ABCDEF012345", "route53_hosted_zone", self.service, self.category,
                    "Route53 hosted zone — 0 queries in 30 days",
                    "Zone 'staging-old.ribbon.io.' idle 30d. No DNS queries detected. Old staging domain. $0.50/mo.",
                    SEVERITY_INFO, 0.50, 0.50, "us-east-1", idle_days=60,
                    metadata={"zone_name": "staging-old.ribbon.io.", "record_count": 4}),
            Finding("Z2FEDCBA987654", "route53_hosted_zone", self.service, self.category,
                    "Route53 hosted zone — 0 queries in 30 days",
                    "Zone 'ribbon-fintech-demo.com.' idle 30d. Created for a hackathon. $0.50/mo.",
                    SEVERITY_INFO, 0.50, 0.50, "us-east-1", idle_days=90,
                    metadata={"zone_name": "ribbon-fintech-demo.com.", "record_count": 2}),
        ]


class SQSIdleQueueAnalyzer(BaseAnalyzer):
    service = "SQS"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        sqs = self._client("sqs")
        findings = []
        queues = sqs.list_queues().get("QueueUrls", [])
        for url in queues:
            name = url.split("/")[-1]
            msgs = self._cw_avg("AWS/SQS", "NumberOfMessagesSent",
                                [{"Name": "QueueName", "Value": name}], days=30, stat="Sum")
            if msgs and msgs > 0:
                continue
            findings.append(Finding(
                resource_id=name,
                resource_type="sqs_queue",
                service=self.service,
                category=self.category,
                title="SQS queue — 0 messages in 30 days",
                description=f"Queue '{name}' inactive 30d. Delete if abandoned.",
                severity=SEVERITY_INFO,
                monthly_cost_usd=0.0,
                estimated_savings_usd=0.0,
                region=self._aws.region,
                idle_days=30,
                metadata={"queue_name": name, "queue_url": url},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-old-notifications-dlq", "sqs_queue", self.service, self.category,
                    "SQS queue — 0 messages in 30 days", "'ribbon-old-notifications-dlq' idle 30d.",
                    SEVERITY_INFO, 0.0, 0.0, "us-east-1", idle_days=30,
                    metadata={"queue_name": "ribbon-old-notifications-dlq"}),
        ]


class SNSIdleTopicAnalyzer(BaseAnalyzer):
    service = "SNS"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        sns = self._client("sns")
        findings = []
        for topic in sns.list_topics().get("Topics", []):
            arn = topic["TopicArn"]
            name = arn.split(":")[-1]
            published = self._cw_avg("AWS/SNS", "NumberOfMessagesPublished",
                                     [{"Name": "TopicName", "Value": name}], days=30, stat="Sum")
            if published and published > 0:
                continue
            findings.append(Finding(
                resource_id=arn,
                resource_type="sns_topic",
                service=self.service,
                category=self.category,
                title="SNS topic — 0 messages published in 30 days",
                description=f"Topic '{name}' inactive 30d.",
                severity=SEVERITY_INFO,
                monthly_cost_usd=0.0,
                estimated_savings_usd=0.0,
                region=self._aws.region,
                idle_days=30,
                metadata={"topic_name": name},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        arn_base = "arn:aws:sns:us-east-1:123456789012"
        return [
            Finding(f"{arn_base}:ribbon-fraud-alerts-v1", "sns_topic", self.service, self.category,
                    "SNS topic — 0 messages in 30 days",
                    "'ribbon-fraud-alerts-v1' replaced by v2 topic. Zero messages 45d. Safe to delete.",
                    SEVERITY_INFO, 0.0, 0.0, "us-east-1", idle_days=45,
                    tags={"team": "payments"}, metadata={"topic_name": "ribbon-fraud-alerts-v1"}),
            Finding(f"{arn_base}:ribbon-staging-deploys", "sns_topic", self.service, self.category,
                    "SNS topic — 0 messages in 30 days",
                    "'ribbon-staging-deploys' no activity in 30d — staging pipeline moved to GitHub Actions.",
                    SEVERITY_INFO, 0.0, 0.0, "us-east-1", idle_days=30,
                    tags={"env": "staging"}, metadata={"topic_name": "ribbon-staging-deploys"}),
        ]


class SecretsManagerUnusedAnalyzer(BaseAnalyzer):
    service = "Secrets Manager"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        sm = self._client("secretsmanager")
        findings = []
        paginator = sm.get_paginator("list_secrets")
        for page in paginator.paginate():
            for secret in page.get("SecretList", []):
                last_accessed = secret.get("LastAccessedDate")
                if last_accessed:
                    days = (datetime.utcnow() - last_accessed.replace(tzinfo=None)).days
                else:
                    days = 999
                if days < 90:
                    continue
                findings.append(Finding(
                    resource_id=secret.get("ARN", secret["Name"]),
                    resource_type="secret",
                    service=self.service,
                    category=self.category,
                    title=f"Secret not accessed in {days} days",
                    description=f"'{secret['Name']}' last accessed {days}d ago. $0.40/mo per secret.",
                    severity=SEVERITY_INFO,
                    monthly_cost_usd=0.40,
                    estimated_savings_usd=0.40,
                    region=self._aws.region,
                    idle_days=days,
                    metadata={"secret_name": secret["Name"]},
                ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("arn:aws:secretsmanager:us-east-1:123:secret:ribbon/old-api-key",
                    "secret", self.service, self.category,
                    "Secret not accessed in 120 days",
                    "'ribbon/old-api-key' idle 120d. Third-party API key rotated, old secret never deleted. $0.40/mo.",
                    SEVERITY_INFO, 0.40, 0.40, "us-east-1", idle_days=120,
                    tags={"team": "platform"}, metadata={"secret_name": "ribbon/old-api-key"}),
            Finding("arn:aws:secretsmanager:us-east-1:123:secret:ribbon/staging-db-pass-v1",
                    "secret", self.service, self.category,
                    "Secret not accessed in 95 days",
                    "'ribbon/staging-db-pass-v1' idle 95d. DB migrated, old credential not cleaned up. $0.40/mo.",
                    SEVERITY_INFO, 0.40, 0.40, "us-east-1", idle_days=95,
                    tags={"env": "staging"}, metadata={"secret_name": "ribbon/staging-db-pass-v1"}),
            Finding("arn:aws:secretsmanager:us-east-1:123:secret:ribbon/sendgrid-test",
                    "secret", self.service, self.category,
                    "Secret not accessed in 180 days",
                    "'ribbon/sendgrid-test' idle 180d. Test API key from 2024 hackathon. $0.40/mo.",
                    SEVERITY_INFO, 0.40, 0.40, "us-east-1", idle_days=180,
                    tags={"env": "dev"}, metadata={"secret_name": "ribbon/sendgrid-test"}),
        ]


class ECRUnusedRepoAnalyzer(BaseAnalyzer):
    service = "ECR"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        ecr = self._client("ecr")
        findings = []
        repos = ecr.describe_repositories().get("repositories", [])
        cutoff = datetime.utcnow() - timedelta(days=90)
        for repo in repos:
            last_pull = repo.get("lastRecordedPullTime")
            if last_pull and last_pull.replace(tzinfo=None) > cutoff:
                continue
            idle_days = (datetime.utcnow() - last_pull.replace(tzinfo=None)).days if last_pull else 999
            findings.append(Finding(
                resource_id=repo["repositoryName"],
                resource_type="ecr_repository",
                service=self.service,
                category=self.category,
                title=f"ECR repository — no pulls in {idle_days} days",
                description=f"'{repo['repositoryName']}' not pulled in {idle_days}d. Storage cost accumulates.",
                severity=SEVERITY_INFO,
                monthly_cost_usd=2.0,
                estimated_savings_usd=2.0,
                region=self._aws.region,
                idle_days=idle_days,
                metadata={"repo_uri": repo.get("repositoryUri")},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-old-worker", "ecr_repository", self.service, self.category,
                    "ECR repository — no pulls in 180 days", "'ribbon-old-worker' 180d no pulls. $2/mo.",
                    SEVERITY_INFO, 2.0, 2.0, "us-east-1", idle_days=180,
                    metadata={"repo_uri": "123456789012.dkr.ecr.us-east-1.amazonaws.com/ribbon-old-worker"}),
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# TIER 3 — Migration opportunities
# ═══════════════════════════════════════════════════════════════════════════════

GRAVITON_MAP = {
    "m5": "m6g", "m5a": "m6g", "m6i": "m7g",
    "c5": "c6g", "c6i": "c7g",
    "r5": "r6g", "r6i": "r7g",
    "t3": "t4g",
}

class EC2GravitonAnalyzer(BaseAnalyzer):
    service = "EC2"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        resp = ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running"]}])
        for res in resp.get("Reservations", []):
            for inst in res.get("Instances", []):
                itype = inst.get("InstanceType", "")
                family = itype.split(".")[0] if "." in itype else ""
                if family not in GRAVITON_MAP:
                    continue
                target_family = GRAVITON_MAP[family]
                target_type = itype.replace(family, target_family)
                cost = _ec2_monthly(itype)
                savings = round(cost * 0.2, 2)
                tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                findings.append(Finding(
                    resource_id=inst["InstanceId"],
                    resource_type="ec2_instance",
                    service=self.service,
                    category=self.category,
                    title=f"EC2 Graviton migration opportunity: {itype} → {target_type}",
                    description=f"Migrate to {target_type} (Graviton). ~20% cheaper + better performance. Save ${savings}/mo.",
                    severity=_severity_from_savings(savings),
                    monthly_cost_usd=cost,
                    estimated_savings_usd=savings,
                    region=self._aws.region,
                    tags=tags,
                    metadata={"current_type": itype, "target_type": target_type, "family": family},
                ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("i-0eks001", "ec2_instance", self.service, self.category,
                    "EC2 Graviton: m5.xlarge → m6g.xlarge", "4 EKS nodes m5.xlarge → m6g.xlarge. Save $112/mo total.",
                    SEVERITY_WARNING, 560.0, 112.0, "us-east-1",
                    tags={"env": "production", "role": "eks-node"},
                    metadata={"current_type": "m5.xlarge", "target_type": "m6g.xlarge", "count": 4}),
        ]


class EBSgp2tog3Analyzer(BaseAnalyzer):
    service = "EBS"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        ec2 = self._client("ec2")
        findings = []
        resp = ec2.describe_volumes(Filters=[{"Name": "volume-type", "Values": ["gp2"]}])
        for vol in resp.get("Volumes", []):
            size = vol.get("Size", 0)
            if size < 100:
                continue
            cost_gp2 = _ebs_monthly("gp2", size)
            cost_gp3 = _ebs_monthly("gp3", size)
            savings = round(cost_gp2 - cost_gp3, 2)
            tags = {t["Key"]: t["Value"] for t in vol.get("Tags", [])}
            findings.append(Finding(
                resource_id=vol["VolumeId"],
                resource_type="ebs_volume",
                service=self.service,
                category=self.category,
                title=f"EBS gp2 → gp3 migration ({size}GB)",
                description=f"{size}GB gp2 ${cost_gp2}/mo → gp3 ${cost_gp3}/mo. Save ${savings}/mo (no downtime).",
                severity=_severity_from_savings(savings),
                monthly_cost_usd=cost_gp2,
                estimated_savings_usd=savings,
                region=self._aws.region,
                tags=tags,
                metadata={"size_gb": size, "current_cost": cost_gp2, "target_cost": cost_gp3},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("vol-0gp2big001", "ebs_volume", self.service, self.category,
                    "EBS gp2 → gp3 migration (2870GB)", "2870GB gp2 $287/mo → gp3 $229/mo. Save $57/mo.",
                    SEVERITY_WARNING, 287.0, 57.0, "us-east-1",
                    metadata={"size_gb": 2870, "current_cost": 287.0, "target_cost": 229.0}),
        ]


class ElastiCacheOldGenAnalyzer(BaseAnalyzer):
    service = "ElastiCache"
    category = CATEGORY_RIGHTSIZE

    OLD_GEN = {"cache.m5": "cache.m6g", "cache.r5": "cache.r6g", "cache.m4": "cache.m6g"}

    def _live(self) -> List[Finding]:
        ec = self._client("elasticache")
        findings = []
        for cluster in ec.describe_cache_clusters().get("CacheClusters", []):
            node_type = cluster.get("CacheNodeType", "")
            family = ".".join(node_type.split(".")[:2])
            if family not in self.OLD_GEN:
                continue
            target_family = self.OLD_GEN[family]
            target_type = node_type.replace(family, target_family)
            cost = _cache_monthly(node_type)
            savings = round(cost * 0.18, 2)
            findings.append(Finding(
                resource_id=cluster["CacheClusterId"],
                resource_type="elasticache_cluster",
                service=self.service,
                category=self.category,
                title=f"ElastiCache old generation: {node_type} → {target_type}",
                description=f"Migrate to Graviton-based {target_type}. ~18% cheaper + better perf. Save ${savings}/mo.",
                severity=_severity_from_savings(savings),
                monthly_cost_usd=cost,
                estimated_savings_usd=savings,
                region=self._aws.region,
                metadata={"current_type": node_type, "target_type": target_type},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-prod-redis", "elasticache_cluster", self.service, self.category,
                    "ElastiCache old gen: cache.r5.large → cache.r6g.large", "Migrate to Graviton. Save $22/mo.",
                    SEVERITY_INFO, 148.0, 22.0, "us-east-1",
                    metadata={"current_type": "cache.r5.large", "target_type": "cache.r6g.large"}),
        ]


class LambdaDeprecatedRuntimeAnalyzer(BaseAnalyzer):
    service = "Lambda"
    category = CATEGORY_RIGHTSIZE

    DEPRECATED = {"nodejs12.x", "nodejs14.x", "python3.7", "python3.8", "java8", "ruby2.7", "go1.x"}
    NEAR_EOL = {"nodejs16.x", "python3.9"}

    def _live(self) -> List[Finding]:
        lam = self._client("lambda")
        findings = []
        for func in lam.list_functions().get("Functions", []):
            runtime = func.get("Runtime", "")
            if runtime in self.DEPRECATED:
                sev = SEVERITY_CRITICAL
                desc = f"Runtime '{runtime}' is deprecated/EOL. Security risk. Upgrade immediately."
            elif runtime in self.NEAR_EOL:
                sev = SEVERITY_WARNING
                desc = f"Runtime '{runtime}' is near end-of-life. Plan migration."
            else:
                continue
            findings.append(Finding(
                resource_id=func["FunctionName"],
                resource_type="lambda_function",
                service=self.service,
                category=self.category,
                title=f"Lambda deprecated runtime: {runtime}",
                description=desc,
                severity=sev,
                monthly_cost_usd=1.0,
                estimated_savings_usd=0.0,
                region=self._aws.region,
                metadata={"runtime": runtime, "function": func["FunctionName"]},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-email-service", "lambda_function", self.service, self.category,
                    "Lambda deprecated runtime: python3.8", "'ribbon-email-service' on Python 3.8 (EOL). Upgrade now.",
                    SEVERITY_CRITICAL, 1.0, 0.0, "us-east-1",
                    metadata={"runtime": "python3.8", "function": "ribbon-email-service"}),
            Finding("ribbon-legacy-webhook", "lambda_function", self.service, self.category,
                    "Lambda deprecated runtime: nodejs14.x", "'ribbon-legacy-webhook' on Node.js 14 (EOL).",
                    SEVERITY_CRITICAL, 1.0, 0.0, "us-east-1",
                    metadata={"runtime": "nodejs14.x", "function": "ribbon-legacy-webhook"}),
        ]


class LambdaOversizedMemoryAnalyzer(BaseAnalyzer):
    service = "Lambda"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        lam = self._client("lambda")
        findings = []
        for func in lam.list_functions().get("Functions", []):
            mem = func.get("MemorySize", 128)
            if mem <= 256:
                continue
            name = func["FunctionName"]
            avg_dur = self._cw_avg("AWS/Lambda", "Duration",
                                   [{"Name": "FunctionName", "Value": name}], days=7)
            if avg_dur == 0:
                continue
            cost_current = round(mem / 1024 * avg_dur / 1000 * 0.0000166667 * 1000000, 2)
            target_mem = max(128, mem // 2)
            cost_target = round(target_mem / 1024 * avg_dur / 1000 * 0.0000166667 * 1000000, 2)
            savings = round(cost_current - cost_target, 2)
            if savings < 1:
                continue
            findings.append(Finding(
                resource_id=name,
                resource_type="lambda_function",
                service=self.service,
                category=self.category,
                title=f"Lambda oversized memory: {mem}MB (consider {target_mem}MB)",
                description=f"'{name}' allocated {mem}MB. Avg duration {avg_dur:.0f}ms. Try {target_mem}MB. Save ~${savings}/mo.",
                severity=_severity_from_savings(savings),
                monthly_cost_usd=cost_current,
                estimated_savings_usd=savings,
                region=self._aws.region,
                metadata={"current_memory_mb": mem, "target_memory_mb": target_mem, "avg_duration_ms": avg_dur},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        return [
            Finding("ribbon-report-generator", "lambda_function", self.service, self.category,
                    "Lambda oversized memory: 1024MB (consider 512MB)",
                    "'ribbon-report-generator' 1024MB, avg 120ms. Try 512MB. Save $8/mo.",
                    SEVERITY_INFO, 16.0, 8.0, "us-east-1",
                    metadata={"current_memory_mb": 1024, "target_memory_mb": 512, "avg_duration_ms": 120}),
        ]


class LambdaOrphanLayerAnalyzer(BaseAnalyzer):
    service = "Lambda"
    category = CATEGORY_CLEANUP

    def _live(self) -> List[Finding]:
        lam = self._client("lambda")
        findings = []
        layers_resp = lam.list_layers()
        all_layers = {l["LayerArn"] for l in layers_resp.get("Layers", [])}
        used_layers: set = set()
        for func in lam.list_functions().get("Functions", []):
            for layer in func.get("Layers", []):
                arn_base = ":".join(layer.get("Arn", "").split(":")[:-1])
                used_layers.add(arn_base)
        for layer_arn in all_layers - used_layers:
            name = layer_arn.split(":")[-1]
            findings.append(Finding(
                resource_id=layer_arn,
                resource_type="lambda_layer",
                service=self.service,
                category=self.category,
                title=f"Lambda layer not used by any function",
                description=f"Layer '{name}' has no functions referencing it.",
                severity=SEVERITY_INFO,
                monthly_cost_usd=0.5,
                estimated_savings_usd=0.5,
                region=self._aws.region,
                metadata={"layer_name": name},
            ))
        return findings

    def _mock(self) -> List[Finding]:
        arn_base = "arn:aws:lambda:us-east-1:123456789012:layer"
        return [
            Finding(f"{arn_base}:ribbon-common-utils", "lambda_layer", self.service, self.category,
                    "Lambda layer not used by any function",
                    "Layer 'ribbon-common-utils' (v3) orphaned after all functions upgraded to v4.",
                    SEVERITY_INFO, 0.5, 0.5, "us-east-1",
                    metadata={"layer_name": "ribbon-common-utils", "version": 3}),
            Finding(f"{arn_base}:ribbon-crypto-deps", "lambda_layer", self.service, self.category,
                    "Lambda layer not used by any function",
                    "Layer 'ribbon-crypto-deps' (v1) has no referencing functions. Stale from Q1 migration.",
                    SEVERITY_INFO, 0.5, 0.5, "us-east-1",
                    metadata={"layer_name": "ribbon-crypto-deps", "version": 1}),
        ]


class ACMExpiringCertAnalyzer(BaseAnalyzer):
    service = "ACM"
    category = CATEGORY_RIGHTSIZE

    def _live(self) -> List[Finding]:
        acm = self._client("acm")
        findings = []
        certs = acm.list_certificates().get("CertificateSummaryList", [])
        for cert in certs:
            try:
                detail = acm.describe_certificate(CertificateArn=cert["CertificateArn"])
                not_after = detail.get("Certificate", {}).get("NotAfter")
                if not not_after:
                    continue
                days_left = (not_after.replace(tzinfo=None) - datetime.utcnow()).days
                if days_left > 30:
                    continue
                sev = SEVERITY_CRITICAL if days_left <= 7 else SEVERITY_WARNING
                findings.append(Finding(
                    resource_id=cert["CertificateArn"],
                    resource_type="acm_certificate",
                    service=self.service,
                    category=self.category,
                    title=f"ACM certificate expiring in {days_left} days",
                    description=f"Certificate for '{cert.get('DomainName')}' expires in {days_left}d. Renew now.",
                    severity=sev,
                    monthly_cost_usd=0.0,
                    estimated_savings_usd=0.0,
                    region=self._aws.region,
                    metadata={"domain": cert.get("DomainName"), "days_left": days_left},
                ))
            except Exception:
                continue
        return findings

    def _mock(self) -> List[Finding]:
        arn_base = "arn:aws:acm:us-east-1:123456789012:certificate"
        return [
            Finding(f"{arn_base}/abc-001", "acm_certificate", self.service, self.category,
                    "ACM certificate expiring in 12 days",
                    "Certificate for 'api.ribbon.io' expires in 12 days. Not managed by ACM auto-renew (was imported). Renew immediately or ALB will return 503.",
                    SEVERITY_WARNING, 0.0, 0.0, "us-east-1",
                    metadata={"domain": "api.ribbon.io", "days_left": 12, "renewal_type": "IMPORTED"}),
            Finding(f"{arn_base}/abc-002", "acm_certificate", self.service, self.category,
                    "ACM certificate expiring in 5 days — URGENT",
                    "Certificate for '*.ribbon-staging.io' expires in 5 days. Staging CI/CD will break. URGENT: enable auto-renewal or issue new cert.",
                    SEVERITY_CRITICAL, 0.0, 0.0, "us-east-1",
                    metadata={"domain": "*.ribbon-staging.io", "days_left": 5, "renewal_type": "IMPORTED"}),
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# Registry: all analyzers
# ═══════════════════════════════════════════════════════════════════════════════

ALL_ANALYZER_CLASSES = [
    # Tier 1 — cleanup
    EBSUnattachedAnalyzer,
    EC2StoppedWithEBSAnalyzer,
    ElasticIPUnusedAnalyzer,
    EC2OrphanAMIAnalyzer,
    SecurityGroupUnusedAnalyzer,
    EBSSnapshotOrphanAnalyzer,
    EBSSnapshotOldAnalyzer,
    EBSSnapshotNoAMIAnalyzer,
    RDSStoppedAnalyzer,
    RDSSnapshotOldAnalyzer,
    RDSAuroraNoReadersAnalyzer,
    RDSNoRecentSnapshotAnalyzer,
    ELBNoTargetsAnalyzer,
    ELBNoListenerAnalyzer,
    ELBClassicAnalyzer,
    S3MultipartIncompleteAnalyzer,
    CloudWatchLogsEmptyAnalyzer,
    CloudWatchLogsOrphanLambdaAnalyzer,
    DynamoDBNoBackupAnalyzer,
    CloudFrontDisabledAnalyzer,
    LambdaOrphanLayerAnalyzer,
    SecretsManagerUnusedAnalyzer,
    ECRUnusedRepoAnalyzer,
    SQSIdleQueueAnalyzer,
    SNSIdleTopicAnalyzer,
    # Tier 1 — rightsize
    EC2UntaggedAnalyzer,
    RDSMultiAZNonProdAnalyzer,
    S3NoBucketLifecycleAnalyzer,
    S3VersioningNoLifecycleAnalyzer,
    S3PublicBucketAnalyzer,
    CloudWatchLogsNoRetentionAnalyzer,
    # Tier 2
    RDSIdleAnalyzer,
    RDSOversizedAnalyzer,
    EC2LowCPUAnalyzer,
    ElastiCacheIdleAnalyzer,
    ElastiCacheOversizedAnalyzer,
    ElastiCacheNoReplicaReadsAnalyzer,
    LambdaUnusedAnalyzer,
    LambdaHighErrorRateAnalyzer,
    NATLowTrafficAnalyzer,
    NATMultipleSameAZAnalyzer,
    NATVPCEndpointOpportunityAnalyzer,
    NATCrossAZAnalyzer,
    EBSNoIOPSAnalyzer,
    DynamoDBIdleAnalyzer,
    DynamoDBGSIUnusedAnalyzer,
    DynamoDBProvisionedAnalyzer,
    S3EmptyBucketAnalyzer,
    Route53IdleAnalyzer,
    # Tier 3
    EC2GravitonAnalyzer,
    EBSgp2tog3Analyzer,
    ElastiCacheOldGenAnalyzer,
    LambdaDeprecatedRuntimeAnalyzer,
    LambdaOversizedMemoryAnalyzer,
    ACMExpiringCertAnalyzer,
]


def run_all_analyzers(
    aws_config: AWSConfig,
    localstack_config: LocalStackConfig,
    progress_cb=None,
    findings_cb=None,
) -> List[Finding]:
    """Run all analyzers across configured scan_regions.

    Global analyzers (IS_GLOBAL=True, e.g. S3, Route53, CloudFront) are run only once
    using the first region. Regional analyzers run once per region.

    progress_cb(analyzer_name, region, done, total) — called before each analyzer starts.
    findings_cb(findings, analyzer_name, region) — called after each analyzer with its results,
        enabling incremental persistence (show findings as they arrive).
    """
    from dataclasses import replace
    scan_regions = aws_config.scan_regions or [aws_config.region]
    global_classes = [cls for cls in ALL_ANALYZER_CLASSES if getattr(cls, "IS_GLOBAL", False)]
    regional_classes = [cls for cls in ALL_ANALYZER_CLASSES if not getattr(cls, "IS_GLOBAL", False)]
    total = len(global_classes) + len(regional_classes) * len(scan_regions)
    done = 0
    logger.info(
        f"Scan plan: {len(regional_classes)} regional analyzers × {len(scan_regions)} region(s)"
        f" + {len(global_classes)} global analyzers (run once) = {total} total"
    )

    all_findings: List[Finding] = []

    # ── Global analyzers — run once against the primary region ────────────────
    primary_region = scan_regions[0]
    global_config = replace(aws_config, region=primary_region)
    logger.info(f"[global / {primary_region}] Running {len(global_classes)} global analyzers...")
    for i, cls in enumerate(global_classes, 1):
        if progress_cb:
            progress_cb(cls.__name__, f"global/{primary_region}", done, total)
        try:
            logger.info(f"[global] ({i}/{len(global_classes)}) {cls.__name__}...")
            analyzer = cls(global_config, localstack_config)
            findings = analyzer.run()
            all_findings.extend(findings)
            logger.info(f"[global] ({i}/{len(global_classes)}) {cls.__name__} → {len(findings)} findings")
            if findings_cb and findings:
                findings_cb(findings, cls.__name__, f"global/{primary_region}")
        except Exception as e:
            logger.warning(f"[global] ({i}/{len(global_classes)}) {cls.__name__} failed: {e}")
        done += 1

    # ── Regional analyzers — run per region ───────────────────────────────────
    for region in scan_regions:
        regional_config = replace(aws_config, region=region)
        logger.info(f"[{region}] Running {len(regional_classes)} regional analyzers...")
        for i, cls in enumerate(regional_classes, 1):
            if progress_cb:
                progress_cb(cls.__name__, region, done, total)
            try:
                logger.info(f"[{region}] ({i}/{len(regional_classes)}) {cls.__name__}...")
                analyzer = cls(regional_config, localstack_config)
                findings = analyzer.run()
                all_findings.extend(findings)
                logger.info(f"[{region}] ({i}/{len(regional_classes)}) {cls.__name__} → {len(findings)} findings")
                if findings_cb and findings:
                    findings_cb(findings, cls.__name__, region)
            except Exception as e:
                logger.warning(f"[{region}] ({i}/{len(regional_classes)}) {cls.__name__} failed: {e}")
            done += 1
        logger.info(f"[{region}] done — {len(all_findings)} total findings so far")

    logger.info(f"Scan complete: {len(all_findings)} total findings")
    return all_findings


# ═══════════════════════════════════════════════════════════════════════════════
# WasteTools — BaseTool interface for the ReasoningEngine
# ═══════════════════════════════════════════════════════════════════════════════

WASTE_TOOL_DEFINITIONS = [
    {
        "name": "get_waste_findings",
        "description": (
            "Query waste findings detected by automated infrastructure analyzers. "
            "Returns resources that are idle, zombie, orphaned, or misconfigured — with estimated monthly savings. "
            "Use this to answer questions about what's wasted, what can be deleted, or what should be rightsized."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Filter by AWS service. E.g.: EBS, RDS, EC2, ELB, Lambda, S3, NAT Gateway, ElastiCache, DynamoDB, CloudWatch Logs. Default: all.",
                },
                "category": {
                    "type": "string",
                    "enum": ["cleanup", "rightsize"],
                    "description": "Filter by category: 'cleanup' (resources to delete) or 'rightsize' (resources to optimize). Default: all.",
                },
                "severity": {
                    "type": "string",
                    "enum": ["critical", "warning", "info"],
                    "description": "Filter by severity level. Default: all.",
                },
                "min_savings": {
                    "type": "number",
                    "description": "Minimum monthly savings in USD to include. E.g. 100 returns only findings saving >= $100/mo.",
                },
                "region": {
                    "type": "string",
                    "description": "Filter by AWS region. Default: all regions.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_findings_trend",
        "description": (
            "Get historical trend of waste findings over time. "
            "Shows how the number of findings and total savings identified have changed across scans. "
            "Use this to answer: 'Is waste increasing or decreasing?', 'Did we improve last month?'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Filter trend by specific AWS service. Default: all services.",
                },
                "days": {
                    "type": "integer",
                    "description": "Number of days of history to return. Default: 30.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_waste_summary",
        "description": (
            "Get a high-level summary of all current waste findings: total count, total savings identified, "
            "breakdown by service and severity. Use this for a quick overview before diving into details."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


class WasteTools(BaseTool):
    """Exposes FindingsStore data to the ReasoningEngine as LLM-callable tools."""

    def __init__(self, store):
        self._store = store

    def get_definitions(self) -> List[Dict[str, Any]]:
        return WASTE_TOOL_DEFINITIONS

    def get_tool_names(self) -> List[str]:
        return [t["name"] for t in WASTE_TOOL_DEFINITIONS]

    def execute(self, tool_name: str, parameters: Dict[str, Any]) -> ToolResult:
        import time
        start = time.time()
        try:
            if tool_name == "get_waste_findings":
                data = self._store.get_findings(
                    service=parameters.get("service"),
                    category=parameters.get("category"),
                    severity=parameters.get("severity"),
                    min_savings=parameters.get("min_savings", 0),
                    region=parameters.get("region"),
                )
                return ToolResult(
                    tool_name=tool_name, operation=tool_name, success=True,
                    data={
                        "findings": data[:50],
                        "total_count": len(data),
                        "total_savings_usd": round(sum(f.get("estimated_savings_usd", 0) for f in data), 2),
                        "filters_applied": {k: v for k, v in parameters.items() if v is not None},
                    },
                    execution_time=round(time.time() - start, 3),
                )
            elif tool_name == "get_findings_trend":
                data = self._store.get_trends(
                    service=parameters.get("service"),
                    days=parameters.get("days", 30),
                )
                return ToolResult(
                    tool_name=tool_name, operation=tool_name, success=True,
                    data={"trend": data, "days": parameters.get("days", 30)},
                    execution_time=round(time.time() - start, 3),
                )
            elif tool_name == "get_waste_summary":
                data = self._store.get_summary()
                return ToolResult(
                    tool_name=tool_name, operation=tool_name, success=True,
                    data=data,
                    execution_time=round(time.time() - start, 3),
                )
            else:
                return ToolResult(tool_name=tool_name, operation=tool_name, success=False,
                                  error=f"Unknown tool: {tool_name}")
        except Exception as e:
            return ToolResult(tool_name=tool_name, operation=tool_name, success=False,
                              error=str(e), execution_time=round(time.time() - start, 3))
