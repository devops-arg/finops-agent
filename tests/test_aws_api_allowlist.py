"""call_aws read-only allowlist + CLI parser.

These tests guard the I-1 invariant from CLAUDE.md: the agent can NEVER mutate
AWS resources. If any of these tests start failing, treat it as a security
regression — investigate before merging.
"""

from __future__ import annotations

import pytest

from backend.config.manager import AWSConfig, LocalStackConfig
from backend.tools.aws_api import (
    _ALLOWED_PREFIXES,
    _BLOCKED_OPERATIONS,
    AWSAPITool,
    _cli_param_to_boto3,
    _is_read_only,
    _parse_cli_command,
    _to_snake,
)

# ── Read-only enforcement ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "op",
    [
        "describe_instances",
        "list_buckets",
        "get_caller_identity",
        "search_resources",
        "scan",
    ],
)
def test_read_only_operations_allowed(op):
    assert _is_read_only(op) is True


@pytest.mark.parametrize(
    "op",
    [
        "delete_db_instance",
        "terminate_instances",
        "create_bucket",
        "stop_instances",
        "modify_db_instance",
        "put_object",
        "run_instances",
        "attach_volume",
        "reboot_db_instance",
        "authorize_security_group_ingress",
    ],
)
def test_write_operations_rejected(op):
    assert _is_read_only(op) is False


def test_blocked_and_allowed_prefixes_are_disjoint():
    """Sanity: a verb can't be both blocked and allowed."""
    assert _BLOCKED_OPERATIONS.isdisjoint(
        _ALLOWED_PREFIXES
    ), "BLOCKED and ALLOWED prefixes overlap — security guard is broken"


def test_aws_api_tool_rejects_terminate_command():
    """End-to-end: an LLM trying to delete via call_aws gets a PermissionError."""
    tool = AWSAPITool(AWSConfig(), LocalStackConfig(enabled=True))
    result = tool.execute("call_aws", {"command": "aws ec2 terminate-instances --instance-ids i-123"})
    assert result.success is False
    assert "not a read-only operation" in (result.error or "").lower()


def test_aws_api_tool_rejects_create_command():
    tool = AWSAPITool(AWSConfig(), LocalStackConfig(enabled=True))
    result = tool.execute("call_aws", {"command": "aws s3api create-bucket --bucket evil"})
    assert result.success is False
    assert "not a read-only operation" in (result.error or "").lower()


def test_aws_api_tool_rejects_empty_command():
    tool = AWSAPITool(AWSConfig(), LocalStackConfig(enabled=True))
    result = tool.execute("call_aws", {"command": ""})
    assert result.success is False
    assert "command is required" in (result.error or "")


def test_aws_api_tool_rejects_unknown_tool_name():
    tool = AWSAPITool(AWSConfig(), LocalStackConfig(enabled=True))
    result = tool.execute("not_call_aws", {"command": "aws ec2 describe-instances"})
    assert result.success is False
    assert "Unknown tool" in result.error


# ── CLI parser ────────────────────────────────────────────────────────────────


def test_to_snake():
    assert _to_snake("describe-db-instances") == "describe_db_instances"
    assert _to_snake("list-buckets") == "list_buckets"


def test_cli_param_to_boto3_pascal_case():
    assert _cli_param_to_boto3("--db-snapshot-identifier") == "DBSnapshotIdentifier"
    assert _cli_param_to_boto3("--instance-ids") == "InstanceIds"
    assert _cli_param_to_boto3("--max-records") == "MaxRecords"
    assert _cli_param_to_boto3("--dry-run") == "DryRun"


def test_parse_cli_command_basic():
    service, op, params, region = _parse_cli_command("aws ec2 describe-instances --region us-west-2")
    assert service == "ec2"
    assert op == "describe_instances"
    assert region == "us-west-2"
    assert params == {}


def test_parse_cli_command_with_params():
    service, op, params, region = _parse_cli_command(
        "aws rds describe-db-instances --db-instance-identifier my-db --region us-east-1"
    )
    assert service == "rds"
    assert op == "describe_db_instances"
    assert region == "us-east-1"
    assert params == {"DBInstanceIdentifier": "my-db"}


def test_parse_cli_command_bare_flag_becomes_true():
    _, _, params, _ = _parse_cli_command("aws ec2 describe-volumes --dry-run")
    assert params == {"DryRun": True}


def test_parse_cli_command_shorthand_struct():
    _, _, params, _ = _parse_cli_command(
        "aws ce get-cost-and-usage --time-period Start=2026-01-01,End=2026-02-01"
    )
    assert params == {"TimePeriod": {"Start": "2026-01-01", "End": "2026-02-01"}}


def test_parse_cli_command_service_alias_s3api_to_s3():
    service, op, *_ = _parse_cli_command("aws s3api list-buckets")
    assert service == "s3"
    assert op == "list_buckets"


def test_parse_cli_command_empty_raises():
    with pytest.raises(ValueError):
        _parse_cli_command("")


def test_parse_cli_command_too_short_raises():
    with pytest.raises(ValueError):
        _parse_cli_command("aws ec2")
