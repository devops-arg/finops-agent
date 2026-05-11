"""
call_aws — generic AWS API tool that lets the LLM call any AWS read-only API.

This is the boto3 equivalent of awslabs/aws-api-mcp-server's `call_aws` tool.
The LLM passes a standard AWS CLI command string; we parse it and dispatch
to boto3 dynamically.  Only read-only (describe/list/get) operations are
allowed unless the caller explicitly opts in.

Examples the LLM can issue:
  aws rds describe-db-instances --region us-west-2
  aws ec2 describe-instances --region us-west-2 --filters Name=instance-state-name,Values=running
  aws ce get-cost-and-usage --time-period Start=2026-01-01,End=2026-02-01 --granularity MONTHLY --metrics UnblendedCost
  aws elasticache describe-cache-clusters --region us-west-2
  aws eks list-clusters --region us-west-2
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections ...
"""

import json
import logging
import re
import shlex
import time
from typing import Any, Dict, List

from backend.tools.base import BaseTool
from backend.models.core import ToolResult
from backend.config.manager import AWSConfig, LocalStackConfig

logger = logging.getLogger(__name__)

# Operations we explicitly block even in read-only mode (sensitive/expensive)
_BLOCKED_OPERATIONS = {
    "delete", "terminate", "stop", "start", "create", "run", "put",
    "update", "modify", "attach", "detach", "associate", "disassociate",
    "revoke", "authorize", "reset", "restore", "reboot",
}

# Prefix whitelist — only these verb prefixes are allowed
_ALLOWED_PREFIXES = {
    "describe", "list", "get", "search", "scan", "query",
    "filter", "show", "check",
}

TOOL_DEFINITIONS = [
    {
        "name": "call_aws",
        "description": (
            "Execute any read-only AWS CLI command and return the JSON response. "
            "Use this tool whenever you need live data from AWS that isn't covered "
            "by the other specific tools (get_rds_status, list_ec2_instances, etc.). "
            "Provide a standard AWS CLI command starting with 'aws'. "
            "Examples:\n"
            "  aws rds describe-db-instances --region us-west-2\n"
            "  aws ec2 describe-instances --region us-west-2\n"
            "  aws cloudwatch get-metric-statistics --namespace AWS/RDS "
            "--metric-name DatabaseConnections --dimensions Name=DBInstanceIdentifier,Value=my-db "
            "--start-time 2026-05-01T00:00:00Z --end-time 2026-05-08T00:00:00Z "
            "--period 604800 --statistics Average --region us-west-2\n"
            "  aws ce get-cost-and-usage --time-period Start=2026-04-01,End=2026-05-01 "
            "--granularity MONTHLY --metrics UnblendedCost\n"
            "  aws elasticache describe-cache-clusters --region us-west-2\n"
            "  aws eks list-clusters --region us-west-2\n"
            "  aws s3api list-buckets\n"
            "Only read-only operations (describe/list/get/search) are permitted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Full AWS CLI command string, must start with 'aws'. "
                        "Example: 'aws rds describe-db-instances --region us-west-2'"
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "Optional cap on the number of items returned (default: 50).",
                },
            },
            "required": ["command"],
        },
    },
]


def _to_snake(s: str) -> str:
    """kebab-case → snake_case for boto3 **method** names (operation names).
    e.g. describe-db-instances → describe_db_instances"""
    return s.replace("-", "_").lower()


# AWS known abbreviations that should stay uppercase in PascalCase
_AWS_ABBREVS = {
    "db", "id", "ids", "arn", "arns", "uri", "url", "ami", "api",
    "ec2", "rds", "eks", "ecs", "iam", "acl", "acls", "vpc", "vpn",
    "tls", "ssl", "mfa", "kms", "sns", "sqs", "sso", "scp", "nat",
    "az", "ip", "cidr", "dns", "ebs", "efs", "elb", "alb", "nlb",
    "s3", "cf",
}


def _cli_param_to_boto3(cli_key: str) -> str:
    """Convert AWS CLI kebab-case param name to boto3 PascalCase.

    Examples:
      --db-snapshot-identifier  →  DBSnapshotIdentifier
      --instance-ids            →  InstanceIds   (plural 's' stays lowercase)
      --instance-id             →  InstanceId    (trailing 'd' = Id not ID)
      --dry-run                 →  DryRun
      --max-records             →  MaxRecords
      --cache-cluster-id        →  CacheClusterId
    """
    # Standalone abbreviations that should stay fully uppercase ONLY when the
    # whole segment is the abbreviation AND it's not a suffix like 'ids'.
    # Rule: db → DB, ec2 → EC2, but 'ids' → 'Ids', 'id' alone at end → 'Id'
    _FULL_UPPER = {"db", "ec2", "rds", "iam", "vpc", "vpn", "acl",
                   "tls", "ssl", "mfa", "kms", "sns", "sqs", "nat", "ami",
                   "api", "arn", "uri", "url", "az", "s3"}

    parts = cli_key.lstrip("-").split("-")
    result = []
    for p in parts:
        pl = p.lower()
        if pl in _FULL_UPPER:
            result.append(pl.upper())
        else:
            result.append(pl.capitalize())
    return "".join(result)


def _parse_value(raw: str) -> Any:
    """Try to parse a raw CLI parameter value as JSON, fall back to string."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _parse_cli_command(cli_command: str) -> tuple[str, str, Dict[str, Any], str]:
    """
    Parse 'aws <service> <operation> [--key value ...]' into
    (service, boto3_operation, params_dict, region).

    Handles:
    - --region us-west-2
    - --filters Name=foo,Values=bar  (both shorthand and JSON)
    - --time-period Start=X,End=Y   (shorthand struct)
    - bare flags like --multi-az
    - positional args (ignored with a warning)
    """
    try:
        parts = shlex.split(cli_command)
    except ValueError:
        parts = cli_command.split()

    if not parts:
        raise ValueError("Empty command")
    if parts[0] == "aws":
        parts = parts[1:]

    if len(parts) < 2:
        raise ValueError(f"Expected 'aws <service> <operation>', got: {cli_command!r}")

    service = parts[0].lower()
    operation_raw = parts[1]
    boto3_op = _to_snake(operation_raw)

    # Map a few CLI service aliases to boto3 service names
    service_aliases = {
        "ce": "ce",
        "cost-explorer": "ce",
        "s3api": "s3",
        "s3": "s3",
        "logs": "logs",
        "cloudwatch": "cloudwatch",
        "cw": "cloudwatch",
    }
    service = service_aliases.get(service, service)

    params: Dict[str, Any] = {}
    region: str = ""

    remaining = parts[2:]
    i = 0
    while i < len(remaining):
        token = remaining[i]

        if not token.startswith("--"):
            # Positional arg — skip with warning
            logger.debug(f"Skipping positional arg: {token!r}")
            i += 1
            continue

        key_raw = token[2:]  # strip leading --

        # Peek at next token
        has_next = (i + 1 < len(remaining)) and not remaining[i + 1].startswith("--")
        if not has_next:
            # bare flag — e.g. --dry-run → DryRun=True
            params[_cli_param_to_boto3(key_raw)] = True
            i += 1
            continue

        value_raw = remaining[i + 1]
        i += 2

        if key_raw == "region":
            region = value_raw
            continue

        # Try JSON first
        parsed = _parse_value(value_raw)
        # If still a string but looks like AWS shorthand (Key=Val,Key2=Val2), try to parse
        if isinstance(parsed, str) and "=" in parsed and not parsed.startswith("{"):
            parsed = _parse_shorthand(parsed)

        params[_cli_param_to_boto3(key_raw)] = parsed

    return service, boto3_op, params, region


def _parse_shorthand(s: str) -> Any:
    """
    Parse AWS CLI shorthand notation.
    Single struct: Key=Val,Key2=Val2  →  {"Key": "Val", "Key2": "Val2"}
    List: Key=Val,Key2=V1 V2 Key=Val3  (not supported, returns original string)
    """
    # Try list-of-structs: 'Name=foo,Values=bar baz' delimited by spaces at top level
    # Simple heuristic: if no spaces, it's a single dict
    try:
        result = {}
        for pair in s.split(","):
            if "=" in pair:
                k, _, v = pair.partition("=")
                result[k.strip()] = v.strip()
        if result:
            return result
    except Exception:
        pass
    return s


def _truncate(data: Any, max_items: int = 50) -> Any:
    """Recursively trim list fields to max_items to avoid LLM context overflow."""
    if isinstance(data, dict):
        return {k: _truncate(v, max_items) for k, v in data.items()}
    if isinstance(data, list):
        trimmed = data[:max_items]
        result = [_truncate(item, max_items) for item in trimmed]
        if len(data) > max_items:
            result.append({"_truncated": f"{len(data) - max_items} more items omitted"})
        return result
    return data


def _is_read_only(operation_boto3: str) -> bool:
    prefix = operation_boto3.split("_")[0].lower()
    return prefix in _ALLOWED_PREFIXES


class AWSAPITool(BaseTool):
    """Generic AWS API tool — lets the LLM call any read-only AWS API."""

    def __init__(self, aws_config: AWSConfig, localstack_config: LocalStackConfig):
        self._aws = aws_config
        self._ls = localstack_config
        self._boto_session = None

    def _get_session(self):
        if self._boto_session:
            return self._boto_session
        import boto3
        if self._aws.profile and not self._ls.enabled:
            session = boto3.Session(
                profile_name=self._aws.profile,
                region_name=self._aws.region,
            )
        else:
            session = boto3.Session(
                aws_access_key_id=self._aws.access_key_id or "test",
                aws_secret_access_key=self._aws.secret_access_key or "test",
                region_name=self._aws.region,
            )
        if self._aws.assume_role_arn and not self._ls.enabled:
            sts = session.client("sts")
            creds = sts.assume_role(
                RoleArn=self._aws.assume_role_arn,
                RoleSessionName="finops-call-aws",
            )["Credentials"]
            session = boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=self._aws.region,
            )
        self._boto_session = session
        return session

    def get_definitions(self) -> List[Dict[str, Any]]:
        return TOOL_DEFINITIONS

    def get_tool_names(self) -> List[str]:
        return ["call_aws"]

    def execute(self, tool_name: str, parameters: Dict[str, Any]) -> ToolResult:
        start = time.time()
        if tool_name != "call_aws":
            return ToolResult(
                tool_name=tool_name, operation=tool_name,
                success=False, error=f"Unknown tool: {tool_name}",
            )
        cli_command = parameters.get("command", "").strip()
        max_results = int(parameters.get("max_results", 50))

        try:
            data = self._call_aws(cli_command, max_results)
            return ToolResult(
                tool_name=tool_name, operation=cli_command,
                success=True, data=data,
                execution_time=round(time.time() - start, 2),
            )
        except Exception as e:
            logger.error(f"call_aws failed [{cli_command!r}]: {e}")
            return ToolResult(
                tool_name=tool_name, operation=cli_command,
                success=False, error=str(e),
                execution_time=round(time.time() - start, 2),
            )

    def _call_aws(self, cli_command: str, max_results: int = 50) -> Dict[str, Any]:
        if not cli_command:
            raise ValueError("command is required")

        service, boto3_op, params, region = _parse_cli_command(cli_command)

        if not _is_read_only(boto3_op):
            raise PermissionError(
                f"Operation '{boto3_op}' is not a read-only operation. "
                "Only describe/list/get/search operations are permitted."
            )

        effective_region = region or self._aws.region or "us-east-1"
        session = self._get_session()

        endpoint_kwargs = {}
        if self._ls.enabled:
            endpoint_kwargs["endpoint_url"] = self._ls.url

        client = session.client(service, region_name=effective_region, **endpoint_kwargs)

        method = getattr(client, boto3_op, None)
        if method is None:
            raise AttributeError(
                f"boto3 client for '{service}' has no method '{boto3_op}'. "
                f"Check the operation name. CLI command was: {cli_command!r}"
            )

        logger.info(f"call_aws → boto3.{service}.{boto3_op}({params}) region={effective_region}")
        response = method(**params)

        # Strip boto3 response metadata
        response.pop("ResponseMetadata", None)

        return _truncate(response, max_results)
