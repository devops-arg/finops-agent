import os
import logging
from dataclasses import dataclass, field
from typing import Optional, List
from pathlib import Path

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    pass


@dataclass
class AWSConfig:
    access_key_id: str = ""
    secret_access_key: str = ""
    region: str = "us-east-1"
    profile: Optional[str] = None
    assume_role_arn: Optional[str] = None


@dataclass
class LocalStackConfig:
    enabled: bool = False
    url: str = "http://localhost:4566"


@dataclass
class LLMConfig:
    provider: str = "anthropic"
    anthropic_api_key: Optional[str] = None
    anthropic_model: str = "claude-sonnet-4-20250514"
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o"


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: List[str] = field(default_factory=lambda: [
        "http://localhost:3000", "http://localhost:8080"
    ])


@dataclass
class ReportConfig:
    weeks: int = 4


@dataclass
class FeatureFlags:
    """Feature flags for dashboard endpoints.

    use_mock_data: If True, /api/report, /api/infrastructure, /api/optimize
        return mock/generated data (fast demo). If False and AWS credentials
        are configured, those endpoints hit live AWS APIs (slower, real data).
        Defaults to True when USE_LOCALSTACK=true (no AWS to hit), False otherwise.
    """
    use_mock_data: bool = True


@dataclass
class Config:
    aws: AWSConfig = field(default_factory=AWSConfig)
    localstack: LocalStackConfig = field(default_factory=LocalStackConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    flags: FeatureFlags = field(default_factory=FeatureFlags)


class ConfigurationManager:
    def __init__(self):
        self._config: Optional[Config] = None

    def load_config(self, env_path: str = None) -> Config:
        if env_path:
            self._load_env_file(env_path)
        else:
            for candidate in [".env", "../.env"]:
                if Path(candidate).exists():
                    self._load_env_file(candidate)
                    break

        self._config = self._build_config()
        return self._config

    @property
    def config(self) -> Config:
        if not self._config:
            raise ConfigurationError("Configuration not loaded. Call load_config() first.")
        return self._config

    def _load_env_file(self, path: str):
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    if key and value:
                        os.environ.setdefault(key, value)
            logger.info(f"Loaded environment from {path}")
        except FileNotFoundError:
            logger.warning(f"Environment file not found: {path}")

    def _build_config(self) -> Config:
        localstack_enabled = os.getenv("USE_LOCALSTACK", "false").lower() in ("true", "1", "yes")

        aws = AWSConfig(
            access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test" if localstack_enabled else ""),
            secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test" if localstack_enabled else ""),
            region=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
            profile=os.getenv("AWS_PROFILE"),
            assume_role_arn=os.getenv("AWS_ASSUME_ROLE_ARN"),
        )
        localstack = LocalStackConfig(
            enabled=localstack_enabled,
            url=os.getenv("LOCALSTACK_URL", "http://localhost:4566"),
        )
        llm = LLMConfig(
            provider=os.getenv("AI_PROVIDER", "anthropic"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o"),
        )
        server = ServerConfig(
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
            cors_origins=[
                o.strip()
                for o in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:8080").split(",")
            ],
        )
        report = ReportConfig(
            weeks=int(os.getenv("REPORT_WEEKS", "4")),
        )
        # USE_MOCK_DATA default: true in LocalStack mode (no AWS), false in live mode
        default_mock = "true" if localstack_enabled else "false"
        flags = FeatureFlags(
            use_mock_data=os.getenv("USE_MOCK_DATA", default_mock).lower() in ("true", "1", "yes"),
        )
        return Config(aws=aws, localstack=localstack, llm=llm, server=server, report=report, flags=flags)

    def validate(self) -> List[str]:
        errors = []
        cfg = self.config
        if cfg.llm.provider == "anthropic" and not cfg.llm.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY is required when AI_PROVIDER=anthropic")
        if cfg.llm.provider == "openai" and not cfg.llm.openai_api_key:
            errors.append("OPENAI_API_KEY is required when AI_PROVIDER=openai")

        # Skip AWS validation in LocalStack mode (uses dummy credentials)
        if not cfg.localstack.enabled:
            has_aws_keys = cfg.aws.access_key_id and cfg.aws.secret_access_key
            has_aws_profile = bool(cfg.aws.profile)
            if not has_aws_keys and not has_aws_profile:
                errors.append(
                    "AWS credentials required: set AWS_ACCESS_KEY_ID+AWS_SECRET_ACCESS_KEY "
                    "or AWS_PROFILE, or enable USE_LOCALSTACK=true for demo mode"
                )
        return errors
