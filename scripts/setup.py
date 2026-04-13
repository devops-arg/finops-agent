#!/usr/bin/env python3
"""
Initial setup script for FinOps Agent.

Generates the cost report and populates the knowledge base so the agent
has pre-indexed context for instant answers.

Run this once after configuring .env, and again whenever you want to refresh
the knowledge base with current data.

Usage:
    python scripts/setup.py
    python scripts/setup.py --report-only    # Skip AWS metadata indexing
    python scripts/setup.py --refresh        # Clear KB and rebuild from scratch
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.config.manager import ConfigurationManager
from backend.knowledge.store import KnowledgeStore
from backend.reports.generator import ReportGenerator


def main():
    parser = argparse.ArgumentParser(description="FinOps Agent - Initial Setup")
    parser.add_argument("--report-only", action="store_true", help="Only generate the cost report, skip KB population")
    parser.add_argument("--refresh", action="store_true", help="Clear knowledge base and rebuild from scratch")
    args = parser.parse_args()

    print("=" * 60)
    print("  FinOps Agent — Initial Setup")
    print("=" * 60)

    config_mgr = ConfigurationManager()
    config = config_mgr.load_config()

    errors = config_mgr.validate()
    if errors:
        for err in errors:
            print(f"  CONFIG ERROR: {err}")
        sys.exit(1)

    # ── Step 1: Generate cost report ──────────────────────────
    print("\n[1/3] Generating weekly cost report...")
    start = time.time()

    report_gen = ReportGenerator(config.aws, config.report.weeks)
    try:
        report = report_gen.generate()
        summary = report.get("summary", {})
        print(f"  Last week cost: ${summary.get('lastWeekCost', 0):,.2f}")
        print(f"  Monthly projection: ${summary.get('monthlyProjection', 0):,.2f}")
        print(f"  Active accounts: {summary.get('activeAccounts', 0)}")
        print(f"  Top service: {summary.get('topService', 'N/A')}")
        print(f"  Report saved to report_data.json ({time.time() - start:.1f}s)")
    except Exception as e:
        print(f"  ERROR generating report: {e}")
        print("  Continuing with knowledge base setup...")
        report = None

    if args.report_only:
        print("\n--report-only flag set, skipping knowledge base setup.")
        print("Done!")
        return

    # ── Step 2: Populate knowledge base ───────────────────────
    print("\n[2/3] Populating knowledge base...")
    start = time.time()

    kb = KnowledgeStore()
    if args.refresh:
        print("  Clearing existing knowledge base...")
        kb.clear()

    total_docs = 0

    if report:
        count = kb.ingest_cost_report(report)
        print(f"  Indexed cost report: {count} documents")
        total_docs += count

    # ── Step 3: Index AWS metadata (accounts, services, regions) ──
    print("\n[3/3] Indexing AWS metadata from Cost Explorer...")
    try:
        import boto3
        aws = config.aws

        if aws.profile:
            session = boto3.Session(profile_name=aws.profile, region_name=aws.region)
        else:
            session = boto3.Session(
                aws_access_key_id=aws.access_key_id,
                aws_secret_access_key=aws.secret_access_key,
                region_name=aws.region,
            )

        if aws.assume_role_arn:
            sts = session.client("sts")
            creds = sts.assume_role(RoleArn=aws.assume_role_arn, RoleSessionName="finops-setup")["Credentials"]
            session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=aws.region,
            )

        ce = session.client("ce", region_name="us-east-1")

        from datetime import datetime, timedelta
        today = datetime.utcnow().date()
        start_date = (today - timedelta(days=30)).isoformat()
        end_date = today.isoformat()

        # Accounts
        try:
            acct_resp = ce.get_dimension_values(
                TimePeriod={"Start": start_date, "End": end_date},
                Dimension="LINKED_ACCOUNT",
            )
            accounts = [
                {"value": v.get("Value", ""), "attributes": v.get("Attributes", {})}
                for v in acct_resp.get("DimensionValues", [])
            ]
            count = kb.ingest_account_metadata(accounts)
            print(f"  Indexed {count} AWS accounts")
            total_docs += count
        except Exception as e:
            print(f"  Warning: Could not list accounts: {e}")

        # Services
        try:
            svc_resp = ce.get_dimension_values(
                TimePeriod={"Start": start_date, "End": end_date},
                Dimension="SERVICE",
            )
            services = [{"value": v.get("Value", "")} for v in svc_resp.get("DimensionValues", [])]
            count = kb.ingest_service_list(services)
            print(f"  Indexed service list ({len(services)} services)")
            total_docs += count
        except Exception as e:
            print(f"  Warning: Could not list services: {e}")

        # Regions
        try:
            region_resp = ce.get_dimension_values(
                TimePeriod={"Start": start_date, "End": end_date},
                Dimension="REGION",
            )
            regions = [{"value": v.get("Value", "")} for v in region_resp.get("DimensionValues", [])]
            count = kb.ingest_region_list(regions)
            print(f"  Indexed region list ({len(regions)} regions)")
            total_docs += count
        except Exception as e:
            print(f"  Warning: Could not list regions: {e}")

    except Exception as e:
        print(f"  ERROR indexing AWS metadata: {e}")

    kb.save()

    print("\n" + "=" * 60)
    print(f"  Setup complete!")
    print(f"  Knowledge base: {kb.document_count} documents indexed")
    print(f"  Report: {'report_data.json' if report else 'not generated'}")
    print(f"")
    print(f"  Start the agent:  python run_server.py")
    print(f"  Open the UI:      http://localhost:3000")
    print("=" * 60)


if __name__ == "__main__":
    main()
