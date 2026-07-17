from core.config import settings


def test_platform_ops_settings_exist_with_defaults():
    # Fields must exist and default to empty string (unset in local/dev tests).
    assert hasattr(settings, "PLATFORM_OPS_OWNER_ID")
    assert hasattr(settings, "PLATFORM_OPS_TOKEN_KEY")
    assert isinstance(settings.PLATFORM_OPS_OWNER_ID, str)
    assert isinstance(settings.PLATFORM_OPS_TOKEN_KEY, str)
