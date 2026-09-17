import json
from bridge import sync


def test_request_dumps_logs_and_turn_context_never_leave_the_microvm():
    assert sync.is_excluded("sessions/request_dump_20260916T101500.json") is True
    assert sync.is_excluded("logs/hermes.log") is True
    assert sync.is_excluded(".lightbulb-turn/room:agent.json") is True
    assert sync.is_excluded("state.db-wal") is True
    assert sync.is_excluded("memories/MEMORY.md") is False
    assert sync.is_excluded("sessions/index.db") is False


def test_boot_secrets_come_from_secrets_manager_not_the_runtime_env():
    class FakeSM:
        def get_secret_value(self, SecretId):
            assert SecretId == "lightbulb/dev/hermes/org_test"
            return {"SecretString": json.dumps({"hookToken": "h", "gatewayToken": "g", "bedrockBearer": "b"})}

    assert sync.load_boot_secrets("lightbulb/dev/hermes/org_test", client=FakeSM()) == {
        "hookToken": "h", "gatewayToken": "g", "bedrockBearer": "b"
    }
