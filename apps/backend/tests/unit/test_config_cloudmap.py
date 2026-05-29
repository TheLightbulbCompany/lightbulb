"""Settings expose Cloud Map namespace/service NAMES for DiscoverInstances."""

from core.config import Settings


def test_cloud_map_name_settings_read_env(monkeypatch):
    monkeypatch.setenv("CLOUD_MAP_NAMESPACE_NAME", "isol8-test.local")
    monkeypatch.setenv("CLOUD_MAP_SERVICE_NAME", "openclaw-containers")
    # Instantiate a throwaway Settings() (pydantic BaseSettings reads env by
    # field name) rather than importlib.reload(core.config): a reload swaps the
    # module-level `settings` object, diverging from the instance every
    # `from core.config import settings` module bound at import — which
    # silently broke other tests' settings mutations (e.g. the workspace chown
    # guard) when this test ran earlier in the suite.
    s = Settings()
    assert s.CLOUD_MAP_NAMESPACE_NAME == "isol8-test.local"
    assert s.CLOUD_MAP_SERVICE_NAME == "openclaw-containers"
