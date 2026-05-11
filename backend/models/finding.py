"""
Finding model — core data structure produced by all waste analyzers.
Every analyzer yields List[Finding] regardless of service or check type.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional
import uuid


# ── Severity levels ───────────────────────────────────────────────────────────
SEVERITY_CRITICAL = "critical"   # > $200/mo waste or security risk
SEVERITY_WARNING  = "warning"    # $50–$200/mo waste
SEVERITY_INFO     = "info"       # < $50/mo or best-practice gap

# ── Finding categories ────────────────────────────────────────────────────────
CATEGORY_CLEANUP    = "cleanup"    # resource should be deleted (zombie/orphan)
CATEGORY_RIGHTSIZE  = "rightsize"  # resource should be resized / reconfigured


def _severity_from_savings(savings: float) -> str:
    if savings >= 200:
        return SEVERITY_CRITICAL
    if savings >= 50:
        return SEVERITY_WARNING
    return SEVERITY_INFO


@dataclass
class Finding:
    resource_id: str
    resource_type: str          # "ebs_volume", "rds_instance", "ec2_instance", …
    service: str                # "EBS", "RDS", "EC2", "ELB", "Lambda", …
    category: str               # CATEGORY_CLEANUP | CATEGORY_RIGHTSIZE
    title: str
    description: str
    severity: str               # SEVERITY_CRITICAL | SEVERITY_WARNING | SEVERITY_INFO
    monthly_cost_usd: float
    estimated_savings_usd: float
    region: str
    account_id: str = "666666666666"
    idle_days: Optional[int] = None
    tags: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    detected_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def fix_command(self) -> Optional[str]:
        """Return the AWS CLI command to delete/fix this resource, or None for rightsizing findings."""
        r = f" --region {self.region}" if self.region else ""
        rid = self.resource_id
        rt = self.resource_type

        if rt == "ebs_volume":
            return f"aws ec2 delete-volume --volume-id {rid}{r}"
        if rt == "ec2_instance":
            return f"aws ec2 terminate-instances --instance-ids {rid}{r}"
        if rt == "elastic_ip":
            if rid.startswith("eipalloc-"):
                return f"aws ec2 release-address --allocation-id {rid}{r}"
            return f"aws ec2 release-address --public-ip {rid}{r}"
        if rt == "ami":
            return f"aws ec2 deregister-image --image-id {rid}{r}"
        if rt == "security_group":
            return f"aws ec2 delete-security-group --group-id {rid}{r}"
        if rt == "ebs_snapshot":
            return f"aws ec2 delete-snapshot --snapshot-id {rid}{r}"
        if rt == "nat_gateway":
            return f"aws ec2 delete-nat-gateway --nat-gateway-id {rid}{r}"
        if rt == "rds_instance" and self.category == CATEGORY_CLEANUP:
            return f"aws rds delete-db-instance --db-instance-identifier {rid} --skip-final-snapshot{r}"
        if rt == "rds_snapshot":
            return f"aws rds delete-db-snapshot --db-snapshot-identifier {rid}{r}"
        if rt == "aurora_cluster" and self.category == CATEGORY_CLEANUP:
            return f"aws rds delete-db-cluster --db-cluster-identifier {rid} --skip-final-snapshot{r}"
        if rt == "lambda_function":
            return f"aws lambda delete-function --function-name {rid}{r}"
        if rt == "cloudwatch_log_group":
            return f"aws logs delete-log-group --log-group-name '{rid}'{r}"
        if rt in ("elb", "alb", "nlb", "load_balancer"):
            arn = self.metadata.get("load_balancer_arn") or self.metadata.get("arn")
            if arn:
                return f"aws elbv2 delete-load-balancer --load-balancer-arn {arn}{r}"
            return f"aws elbv2 delete-load-balancer --load-balancer-arn <ARN>{r}"
        if rt == "elasticache_cluster":
            grp = self.metadata.get("replication_group_id")
            if grp:
                return f"aws elasticache delete-replication-group --replication-group-id {grp}{r}"
            return f"aws elasticache delete-cache-cluster --cache-cluster-id {rid}{r}"
        if rt == "ecr_image":
            repo = self.metadata.get("repository_name", rid)
            digest = self.metadata.get("image_digest", "")
            if digest:
                return f"aws ecr batch-delete-image --repository-name {repo} --image-ids imageDigest={digest}{r}"
        if rt == "s3_bucket":
            return f"aws s3 rb s3://{rid} --force"
        if rt == "acm_certificate":
            arn = self.metadata.get("certificate_arn", rid)
            return f"aws acm delete-certificate --certificate-arn {arn}{r}"
        if rt == "secrets_manager_secret":
            return f"aws secretsmanager delete-secret --secret-id {rid} --force-delete-without-recovery{r}"
        if rt == "sqs_queue":
            url = self.metadata.get("queue_url", rid)
            return f"aws sqs delete-queue --queue-url {url}{r}"
        if rt == "route53_hosted_zone":
            zone_id = self.metadata.get("hosted_zone_id", rid)
            return f"aws route53 delete-hosted-zone --id {zone_id}"
        if rt == "cloudfront_distribution":
            dist_id = self.metadata.get("distribution_id", rid)
            return f"aws cloudfront delete-distribution --id {dist_id} --if-match <ETag>"
        if rt == "ecr_repository" and self.category == CATEGORY_CLEANUP:
            return f"aws ecr delete-repository --repository-name {rid} --force{r}"
        if rt == "secret" or rt == "secretsmanager_secret":
            name = self.metadata.get("secret_name") or rid
            return f"aws secretsmanager delete-secret --secret-id {name} --force-delete-without-recovery{r}"
        if rt == "rds_instance" and self.category == CATEGORY_RIGHTSIZE:
            new_class = self.metadata.get("recommended_class", "<new-instance-class>")
            return f"aws rds modify-db-instance --db-instance-identifier {rid} --db-instance-class {new_class} --apply-immediately{r}"
        if rt == "aurora_cluster" and self.category == CATEGORY_RIGHTSIZE:
            new_class = self.metadata.get("recommended_class", "<new-instance-class>")
            return f"# Modify each instance in the cluster:\naws rds modify-db-instance --db-instance-identifier <instance-id> --db-instance-class {new_class} --apply-immediately{r}"
        # Rightsizing or unknown — no simple delete command
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "resource_id": self.resource_id,
            "resource_type": self.resource_type,
            "service": self.service,
            "category": self.category,
            "title": self.title,
            "description": self.description,
            "severity": self.severity,
            "monthly_cost_usd": self.monthly_cost_usd,
            "estimated_savings_usd": self.estimated_savings_usd,
            "region": self.region,
            "account_id": self.account_id,
            "idle_days": self.idle_days,
            "tags": self.tags,
            "metadata": self.metadata,
            "detected_at": self.detected_at,
            "fix_command": self.fix_command(),
        }


def compute_fix_command(d: Dict[str, Any]) -> Optional[str]:
    """Compute the fix command for a finding dict loaded from SQLite (no Finding object needed)."""
    rt = d.get("resource_type", "")
    rid = d.get("resource_id", "")
    region = d.get("region", "")
    category = d.get("category", "")
    meta = d.get("metadata") or {}
    r = f" --region {region}" if region else ""

    if rt == "ebs_volume":
        return f"aws ec2 delete-volume --volume-id {rid}{r}"
    if rt == "ec2_instance":
        return f"aws ec2 terminate-instances --instance-ids {rid}{r}"
    if rt == "elastic_ip":
        if rid.startswith("eipalloc-"):
            return f"aws ec2 release-address --allocation-id {rid}{r}"
        return f"aws ec2 release-address --public-ip {rid}{r}"
    if rt == "ami":
        return f"aws ec2 deregister-image --image-id {rid}{r}"
    if rt == "security_group":
        return f"aws ec2 delete-security-group --group-id {rid}{r}"
    if rt == "ebs_snapshot":
        return f"aws ec2 delete-snapshot --snapshot-id {rid}{r}"
    if rt == "nat_gateway":
        return f"aws ec2 delete-nat-gateway --nat-gateway-id {rid}{r}"
    if rt == "rds_instance" and category == CATEGORY_CLEANUP:
        return f"aws rds delete-db-instance --db-instance-identifier {rid} --skip-final-snapshot{r}"
    if rt == "rds_snapshot":
        return f"aws rds delete-db-snapshot --db-snapshot-identifier {rid}{r}"
    if rt == "aurora_cluster" and category == CATEGORY_CLEANUP:
        return f"aws rds delete-db-cluster --db-cluster-identifier {rid} --skip-final-snapshot{r}"
    if rt == "lambda_function":
        return f"aws lambda delete-function --function-name {rid}{r}"
    if rt == "cloudwatch_log_group":
        return f"aws logs delete-log-group --log-group-name '{rid}'{r}"
    if rt in ("elb", "alb", "nlb", "load_balancer"):
        arn = meta.get("load_balancer_arn") or meta.get("arn")
        if arn:
            return f"aws elbv2 delete-load-balancer --load-balancer-arn {arn}{r}"
        return f"aws elbv2 delete-load-balancer --load-balancer-arn <ARN>{r}"
    if rt == "elasticache_cluster":
        grp = meta.get("replication_group_id")
        if grp:
            return f"aws elasticache delete-replication-group --replication-group-id {grp}{r}"
        return f"aws elasticache delete-cache-cluster --cache-cluster-id {rid}{r}"
    if rt == "ecr_image":
        repo = meta.get("repository_name", rid)
        digest = meta.get("image_digest", "")
        if digest:
            return f"aws ecr batch-delete-image --repository-name {repo} --image-ids imageDigest={digest}{r}"
    if rt == "s3_bucket":
        return f"aws s3 rb s3://{rid} --force"
    if rt == "acm_certificate":
        arn = meta.get("certificate_arn", rid)
        return f"aws acm delete-certificate --certificate-arn {arn}{r}"
    if rt == "secrets_manager_secret":
        return f"aws secretsmanager delete-secret --secret-id {rid} --force-delete-without-recovery{r}"
    if rt == "sqs_queue":
        url = meta.get("queue_url", rid)
        return f"aws sqs delete-queue --queue-url {url}{r}"
    if rt == "route53_hosted_zone":
        zone_id = meta.get("hosted_zone_id", rid)
        return f"aws route53 delete-hosted-zone --id {zone_id}"
    if rt == "cloudfront_distribution":
        dist_id = meta.get("distribution_id", rid)
        return f"aws cloudfront delete-distribution --id {dist_id} --if-match <ETag>"
    if rt == "ecr_repository" and category == CATEGORY_CLEANUP:
        return f"aws ecr delete-repository --repository-name {rid} --force{r}"
    if rt in ("secret", "secretsmanager_secret", "secrets_manager_secret"):
        name = meta.get("secret_name") or rid
        return f"aws secretsmanager delete-secret --secret-id {name} --force-delete-without-recovery{r}"
    if rt == "rds_instance" and category == CATEGORY_RIGHTSIZE:
        new_class = meta.get("recommended_class", "<new-instance-class>")
        return f"aws rds modify-db-instance --db-instance-identifier {rid} --db-instance-class {new_class} --apply-immediately{r}"
    if rt == "aurora_cluster" and category == CATEGORY_RIGHTSIZE:
        new_class = meta.get("recommended_class", "<new-instance-class>")
        return f"# Modify each instance in the cluster:\naws rds modify-db-instance --db-instance-identifier <instance-id> --db-instance-class {new_class} --apply-immediately{r}"
    return None
