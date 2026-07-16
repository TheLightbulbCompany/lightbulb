"""The gate-URL resolver: SSM-discovered, cached, break-glass-aware, fail-LOUD.

The conftest autouse fixture patches the module ATTRIBUTE ``gate_url`` for the
rest of the suite (tests default to direct mode); this module tests the real
implementation via a from-import reference captured at collection time, which
the attribute patch does not touch.
"""

from unittest.mock import MagicMock, patch

import pytest

from core.billing import bedrock_gate_url as mod
from core.billing.bedrock_gate_url import GateUrlUnavailable
from core.billing.bedrock_gate_url import gate_url as real_gate_url


@pytest.fixture(autouse=True)
def _fresh_cache():
    mod.reset_cache()
    yield
    mod.reset_cache()


def _ssm_returning(value: str):
    client = MagicMock()
    client.get_parameter.return_value = {"Parameter": {"Value": value}}
    return client


def test_settings_override_wins_without_touching_ssm(monkeypatch):
    monkeypatch.setattr(mod.settings, "BEDROCK_GATE_URL", "https://override.example/", raising=False)
    with patch.object(mod, "boto3") as b3:
        assert real_gate_url() == "https://override.example/"
        b3.client.assert_not_called()


def test_reads_the_env_parameter_and_caches(monkeypatch):
    monkeypatch.setattr(mod.settings, "BEDROCK_GATE_URL", "", raising=False)
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev", raising=False)
    client = _ssm_returning("https://gate.dev.example/")
    with patch.object(mod.boto3, "client", return_value=client) as mk:
        assert real_gate_url() == "https://gate.dev.example/"
        assert real_gate_url() == "https://gate.dev.example/"  # cached
        assert mk.call_count == 1
    client.get_parameter.assert_called_once_with(Name="/isol8/dev/bedrock-gate-url")


def test_direct_value_is_break_glass_empty(monkeypatch):
    monkeypatch.setattr(mod.settings, "BEDROCK_GATE_URL", "", raising=False)
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "prod", raising=False)
    with patch.object(mod.boto3, "client", return_value=_ssm_returning("direct")):
        assert real_gate_url() == ""


def test_missing_parameter_raises_loudly(monkeypatch):
    # A silently-direct config would AccessDeny at inference (owner roles carry
    # no bedrock:Invoke) — misconfiguration must fail AT provisioning.
    monkeypatch.setattr(mod.settings, "BEDROCK_GATE_URL", "", raising=False)
    client = MagicMock()
    client.get_parameter.side_effect = Exception("ParameterNotFound")
    with patch.object(mod.boto3, "client", return_value=client):
        with pytest.raises(GateUrlUnavailable, match="bedrock-gate-url"):
            real_gate_url()


def test_reset_cache_forces_reread(monkeypatch):
    monkeypatch.setattr(mod.settings, "BEDROCK_GATE_URL", "", raising=False)
    with patch.object(mod.boto3, "client", return_value=_ssm_returning("https://a/")):
        assert real_gate_url() == "https://a/"
    mod.reset_cache()
    with patch.object(mod.boto3, "client", return_value=_ssm_returning("https://b/")):
        assert real_gate_url() == "https://b/"
