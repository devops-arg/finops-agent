#!/usr/bin/env python3
"""Test AWS and LLM connectivity before running the agent."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.config.manager import ConfigurationManager


def test_aws():
    print("Testing AWS connection...")
    try:
        import boto3
        config_mgr = ConfigurationManager()
        config = config_mgr.load_config()
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
            creds = sts.assume_role(RoleArn=aws.assume_role_arn, RoleSessionName="test")["Credentials"]
            session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=aws.region,
            )

        sts = session.client("sts")
        identity = sts.get_caller_identity()
        print(f"  AWS Account: {identity['Account']}")
        print(f"  AWS ARN: {identity['Arn']}")

        ce = session.client("ce", region_name="us-east-1")
        from datetime import datetime, timedelta
        today = datetime.utcnow().date()
        start = (today - timedelta(days=7)).isoformat()
        end = today.isoformat()
        result = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )
        total = sum(
            float(p.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0))
            for p in result.get("ResultsByTime", [])
        )
        print(f"  Last 7 days cost: ${total:.2f}")
        print("  AWS: OK")
        return True
    except Exception as e:
        print(f"  AWS: FAILED - {e}")
        return False


def test_llm():
    print("\nTesting LLM connection...")
    config_mgr = ConfigurationManager()
    config = config_mgr.load_config()

    try:
        if config.llm.provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=config.llm.anthropic_api_key)
            response = client.messages.create(
                model=config.llm.anthropic_model,
                max_tokens=50,
                messages=[{"role": "user", "content": "Say 'FinOps Agent ready' in 3 words."}],
            )
            print(f"  Model: {config.llm.anthropic_model}")
            print(f"  Response: {response.content[0].text}")
        else:
            from openai import OpenAI
            client = OpenAI(api_key=config.llm.openai_api_key)
            response = client.chat.completions.create(
                model=config.llm.openai_model,
                max_tokens=50,
                messages=[{"role": "user", "content": "Say 'FinOps Agent ready' in 3 words."}],
            )
            print(f"  Model: {config.llm.openai_model}")
            print(f"  Response: {response.choices[0].message.content}")

        print("  LLM: OK")
        return True
    except Exception as e:
        print(f"  LLM: FAILED - {e}")
        return False


if __name__ == "__main__":
    print("=" * 50)
    print("FinOps Agent - Connection Test")
    print("=" * 50)

    aws_ok = test_aws()
    llm_ok = test_llm()

    print("\n" + "=" * 50)
    print(f"Results: AWS={'OK' if aws_ok else 'FAIL'}, LLM={'OK' if llm_ok else 'FAIL'}")
    if aws_ok and llm_ok:
        print("All systems ready! Run: python run_server.py")
    else:
        print("Fix the issues above before starting the agent.")
    print("=" * 50)

    sys.exit(0 if (aws_ok and llm_ok) else 1)
