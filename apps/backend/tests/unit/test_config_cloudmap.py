"""Settings expose Cloud Map namespace/service NAMES for DiscoverInstances."""

import importlib


def test_cloud_map_name_settings_read_env(monkeypatch):
    monkeypatch.setenv("CLOUD_MAP_NAMESPACE_NAME", "isol8-test.local")
    monkeypatch.setenv("CLOUD_MAP_SERVICE_NAME", "openclaw-containers")
    import core.config as config

    importlib.reload(config)
    try:
        assert config.settings.CLOUD_MAP_NAMESPACE_NAME == "isol8-test.local"
        assert config.settings.CLOUD_MAP_SERVICE_NAME == "openclaw-containers"
    finally:
        importlib.reload(config)
