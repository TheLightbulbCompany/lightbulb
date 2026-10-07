from unittest.mock import Mock

import pytest

from core.billing import openrouter_gate_url as gate


def test_ssm_function_url_becomes_chat_completions_base(monkeypatch):
    monkeypatch.setattr(gate.settings, "OPENROUTER_GATE_URL", "")
    monkeypatch.setattr(gate.settings, "ENVIRONMENT", "dev")
    client = Mock()
    client.get_parameter.return_value = {"Parameter": {"Value": "https://abc.lambda-url.us-east-1.on.aws/"}}
    monkeypatch.setattr(gate.boto3, "client", lambda *args, **kwargs: client)
    gate.reset_cache()

    assert gate.gate_url() == "https://abc.lambda-url.us-east-1.on.aws/v1"
    client.get_parameter.assert_called_once_with(Name="/lightbulb/dev/openrouter-gate-url")


def test_missing_parameter_fails_closed(monkeypatch):
    monkeypatch.setattr(gate.settings, "OPENROUTER_GATE_URL", "")
    client = Mock()
    client.get_parameter.side_effect = RuntimeError("missing")
    monkeypatch.setattr(gate.boto3, "client", lambda *args, **kwargs: client)
    gate.reset_cache()

    with pytest.raises(gate.GateUrlUnavailable):
        gate.gate_url()
