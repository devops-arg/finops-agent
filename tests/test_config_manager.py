"""Config loading + validation."""

from __future__ import annotations

from backend.config.manager import ConfigurationManager


def test_localstack_mode_default_uses_mock(monkeypatch):
    monkeypatch.setenv("USE_LOCALSTACK", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg = ConfigurationManager().load_config()
    assert cfg.localstack.enabled is True
    # When LocalStack is on, mock-data defaults to True
    assert cfg.flags.use_mock_data is True
    # Dummy AWS creds are injected so boto3 does not blow up
    assert cfg.aws.access_key_id == "test"


def test_live_mode_requires_aws_creds(monkeypatch):
    monkeypatch.setenv("USE_LOCALSTACK", "false")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    mgr = ConfigurationManager()
    mgr.load_config()
    errors = mgr.validate()
    assert any("AWS credentials required" in e for e in errors), errors


def test_live_mode_with_static_keys_validates(monkeypatch):
    monkeypatch.setenv("USE_LOCALSTACK", "false")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    mgr = ConfigurationManager()
    mgr.load_config()
    assert mgr.validate() == []


def test_anthropic_api_key_required_in_live_mode(monkeypatch):
    monkeypatch.setenv("USE_LOCALSTACK", "false")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AI_PROVIDER", "anthropic")
    mgr = ConfigurationManager()
    mgr.load_config()
    errors = mgr.validate()
    assert any("ANTHROPIC_API_KEY" in e for e in errors), errors


def test_scan_regions_parsed_from_csv(monkeypatch):
    monkeypatch.setenv("USE_LOCALSTACK", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGIONS_TO_ANALYZE", "us-east-1, us-west-2 , eu-west-1")
    cfg = ConfigurationManager().load_config()
    assert cfg.aws.scan_regions == ["us-east-1", "us-west-2", "eu-west-1"]


def test_scan_regions_default_to_default_region(monkeypatch):
    monkeypatch.setenv("USE_LOCALSTACK", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "sa-east-1")
    cfg = ConfigurationManager().load_config()
    assert cfg.aws.scan_regions == ["sa-east-1"]
