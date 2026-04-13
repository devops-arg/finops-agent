#!/usr/bin/env python3
"""
Seed LocalStack with demo AWS resources for the FinOps Intelligence Platform.
Simulates a Series A LatAm fintech startup's AWS footprint.
"""
import os
import sys
import time
import boto3
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

ENDPOINT = os.environ.get("LOCALSTACK_URL", "http://localhost:4566")
REGION = "us-east-1"

BOTO_KWARGS = dict(
    endpoint_url=ENDPOINT,
    region_name=REGION,
    aws_access_key_id="test",
    aws_secret_access_key="test",
)


def wait_for_localstack(retries=20, delay=3):
    import urllib.request
    url = ENDPOINT + "/_localstack/health"
    for i in range(retries):
        try:
            urllib.request.urlopen(url, timeout=3)
            logger.info("LocalStack is ready.")
            return True
        except Exception:
            logger.info(f"Waiting for LocalStack... ({i+1}/{retries})")
            time.sleep(delay)
    logger.error("LocalStack did not become ready in time.")
    return False


def seed_s3():
    logger.info("Seeding S3 buckets...")
    s3 = boto3.client("s3", **BOTO_KWARGS)
    buckets = [
        "fintech-prod-data",
        "fintech-prod-backups",
        "fintech-staging-data",
        "fintech-ml-models",
    ]
    for name in buckets:
        try:
            s3.create_bucket(Bucket=name)
            logger.info(f"  Created bucket: {name}")
        except s3.exceptions.BucketAlreadyExists:
            logger.info(f"  Bucket already exists: {name}")
        except Exception as e:
            logger.warning(f"  Failed to create bucket {name}: {e}")

    # Add tags to prod bucket
    try:
        s3.put_bucket_tagging(
            Bucket="fintech-prod-data",
            Tagging={"TagSet": [
                {"Key": "Environment", "Value": "production"},
                {"Key": "Team", "Value": "platform"},
                {"Key": "CostCenter", "Value": "engineering"},
            ]},
        )
    except Exception:
        pass


def seed_ec2():
    logger.info("Seeding EC2 instances...")
    ec2 = boto3.client("ec2", **BOTO_KWARGS)

    instances = [
        # Production cluster
        {"type": "m5.xlarge", "count": 3, "name": "api-prod", "env": "production"},
        {"type": "c5.2xlarge", "count": 2, "name": "worker-prod", "env": "production"},
        {"type": "m5.large", "count": 2, "name": "web-prod", "env": "production"},
        # Staging
        {"type": "t3.large", "count": 2, "name": "api-staging", "env": "staging"},
        {"type": "t3.medium", "count": 1, "name": "worker-staging", "env": "staging"},
        # Dev (stopped)
        {"type": "t3.micro", "count": 2, "name": "dev-instance", "env": "development"},
    ]

    for spec in instances:
        for i in range(spec["count"]):
            try:
                resp = ec2.run_instances(
                    ImageId="ami-12345678",  # Fake AMI for LocalStack
                    MinCount=1,
                    MaxCount=1,
                    InstanceType=spec["type"],
                    TagSpecifications=[{
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "Name", "Value": f"{spec['name']}-{i+1}"},
                            {"Key": "Environment", "Value": spec["env"]},
                            {"Key": "Team", "Value": "platform"},
                        ],
                    }],
                )
                iid = resp["Instances"][0]["InstanceId"]
                logger.info(f"  Started {spec['type']} instance: {spec['name']}-{i+1} ({iid})")
                # Stop dev instances
                if spec["env"] == "development":
                    time.sleep(0.3)
                    ec2.stop_instances(InstanceIds=[iid])
                    logger.info(f"  Stopped dev instance: {iid}")
            except Exception as e:
                logger.warning(f"  EC2 error: {e}")


def seed_rds():
    logger.info("Seeding RDS instances...")
    rds = boto3.client("rds", **BOTO_KWARGS)

    instances = [
        {
            "DBInstanceIdentifier": "fintech-prod-primary",
            "DBInstanceClass": "db.r5.2xlarge",
            "Engine": "postgres",
            "MasterUsername": "admin",
            "MasterUserPassword": "demo-password-123",
            "DBName": "fintech_prod",
            "Tags": [
                {"Key": "Environment", "Value": "production"},
                {"Key": "Role", "Value": "primary"},
            ],
        },
        {
            "DBInstanceIdentifier": "fintech-prod-replica",
            "DBInstanceClass": "db.r5.xlarge",
            "Engine": "postgres",
            "MasterUsername": "admin",
            "MasterUserPassword": "demo-password-123",
            "DBName": "fintech_prod",
            "Tags": [
                {"Key": "Environment", "Value": "production"},
                {"Key": "Role", "Value": "read-replica"},
            ],
        },
        {
            "DBInstanceIdentifier": "fintech-staging-db",
            "DBInstanceClass": "db.t3.medium",
            "Engine": "postgres",
            "MasterUsername": "admin",
            "MasterUserPassword": "demo-password-123",
            "DBName": "fintech_staging",
            "Tags": [{"Key": "Environment", "Value": "staging"}],
        },
    ]

    for params in instances:
        try:
            rds.create_db_instance(**params)
            logger.info(f"  Created RDS: {params['DBInstanceIdentifier']} ({params['DBInstanceClass']})")
        except Exception as e:
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if "AlreadyExists" in err_code:
                logger.info(f"  RDS already exists: {params['DBInstanceIdentifier']}")
            else:
                logger.warning(f"  RDS error for {params['DBInstanceIdentifier']}: {e}")


def seed_elasticache():
    logger.info("Seeding ElastiCache clusters...")
    ec = boto3.client("elasticache", **BOTO_KWARGS)

    clusters = [
        {
            "CacheClusterId": "fintech-prod-cache",
            "CacheNodeType": "cache.r6g.large",
            "Engine": "redis",
            "NumCacheNodes": 1,
            "Tags": [{"Key": "Environment", "Value": "production"}],
        },
        {
            "CacheClusterId": "fintech-staging-cache",
            "CacheNodeType": "cache.t3.micro",
            "Engine": "redis",
            "NumCacheNodes": 1,
            "Tags": [{"Key": "Environment", "Value": "staging"}],
        },
    ]

    for params in clusters:
        try:
            ec.create_cache_cluster(**params)
            logger.info(f"  Created ElastiCache: {params['CacheClusterId']} ({params['CacheNodeType']})")
        except Exception as e:
            logger.warning(f"  ElastiCache error: {e}")


def seed_secretsmanager():
    logger.info("Seeding Secrets Manager...")
    sm = boto3.client("secretsmanager", **BOTO_KWARGS)

    secrets = [
        ("fintech/prod/db-password", '{"username":"admin","password":"DEMO_ONLY"}'),
        ("fintech/prod/api-keys", '{"stripe":"sk_test_demo","sendgrid":"SG.demo"}'),
        ("fintech/staging/db-password", '{"username":"admin","password":"DEMO_ONLY"}'),
    ]

    for name, value in secrets:
        try:
            sm.create_secret(Name=name, SecretString=value)
            logger.info(f"  Created secret: {name}")
        except Exception as e:
            logger.warning(f"  Secret error: {e}")


def main():
    logger.info("=" * 60)
    logger.info("FinOps Demo — LocalStack Seeder")
    logger.info(f"Target: {ENDPOINT}")
    logger.info("=" * 60)

    if not wait_for_localstack():
        sys.exit(1)

    for name, fn in [("S3", seed_s3), ("EC2", seed_ec2), ("RDS", seed_rds),
                      ("ElastiCache", seed_elasticache), ("SecretsManager", seed_secretsmanager)]:
        try:
            fn()
        except Exception as e:
            logger.warning(f"Skipping {name} (not available in LocalStack free): {e}")

    logger.info("=" * 60)
    logger.info("Seeding complete. LocalStack is ready for demo.")
    logger.info("Start the backend: uvicorn backend.server.main:app --reload")
    logger.info("Open frontend:     http://localhost:3000")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
