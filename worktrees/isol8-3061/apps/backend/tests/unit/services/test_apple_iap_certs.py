"""The Apple trust anchors must actually build a verifier.

Separate from test_apple_iap.py because those tests patch `_verifier` away —
they cover the logic around verification and by design never touch the real
certificates. Nothing there would notice the certs directory being empty,
misnamed, moved, or stripped from the Docker build context, and the symptom is
silent: `_verifier` raises AppleIapNotConfigured, the router answers 503, and
every completed StoreKit purchase fails to grant a plan while looking, in the
logs, like a feature that is merely switched off.

These assert by BUILDING the thing, not by checking that files exist.
"""

from pathlib import Path

import pytest

from core.services import apple_iap
from core.services.apple_iap import AppleIapNotConfigured

CERTS_DIR = Path(__file__).resolve().parents[3] / "certs" / "apple"

# Mirrors apps/infra/lib/stacks/service-stack.ts. Wrong values here are not a
# test problem — they are the deployed configuration being wrong.
BUNDLE_ID = "co.isol8.mobile"
PRODUCT_ID = "company.thelightbulb.mobile.plan.monthly"
APP_APPLE_ID = "6788944100"


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_BUNDLE_ID", BUNDLE_ID)
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_PRODUCT_ID", PRODUCT_ID)
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_ROOT_CERTS_DIR", str(CERTS_DIR))
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_APP_APPLE_ID", APP_APPLE_ID)
    apple_iap._verifier.cache_clear()
    yield
    apple_iap._verifier.cache_clear()


@pytest.mark.parametrize("environment", ["production", "sandbox"])
def test_a_verifier_builds_for_both_environments(_configured, environment):
    """Both, because App Review sends SANDBOX transactions to the PRODUCTION
    backend — a production-only trust setup fails the submission."""
    assert apple_iap._verifier(environment) is not None


def test_the_roots_are_self_signed_apple_certificates(_configured):
    """A chain pinned to the wrong anchor is worse than no pinning: it would
    accept transactions signed by whoever issued that anchor."""
    from cryptography import x509

    files = sorted(CERTS_DIR.glob("*.cer"))
    assert files, f"no .cer files in {CERTS_DIR} — the verifier cannot be built"

    for path in files:
        cert = x509.load_der_x509_certificate(path.read_bytes())
        assert cert.issuer == cert.subject, f"{path.name} is not self-signed"
        assert "Apple" in cert.subject.rfc4514_string(), f"{path.name} is not an Apple root"


def test_an_empty_certs_dir_disables_the_feature_rather_than_weakening_it(monkeypatch, tmp_path):
    """Fail closed. The alternative — building a verifier with no anchors — is
    a verifier that trusts anything."""
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_BUNDLE_ID", BUNDLE_ID)
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_PRODUCT_ID", PRODUCT_ID)
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_ROOT_CERTS_DIR", str(tmp_path))
    apple_iap._verifier.cache_clear()
    try:
        with pytest.raises(AppleIapNotConfigured, match="no Apple root certificates"):
            apple_iap._verifier("sandbox")
    finally:
        apple_iap._verifier.cache_clear()


def test_the_bundle_id_agrees_across_every_file_that_declares_it():
    """One bundle id lives in four places and Apple only honours one of them.

    `APPLE_IAP_BUNDLE_ID` (what the backend demands of a StoreKit JWS),
    `app.json`'s `bundleIdentifier` (what the binary is signed as) and the
    `apple-app-site-association` appID (what universal links resolve to) must
    be the same string. A rename that misses one is silent in every test and
    every build: the app signs, the site file serves, and only a real purchase
    or a real universal link fails. #2098 renamed all of them to
    `company.thelightbulb.mobile` while App Store Connect held exactly one
    record, on `co.isol8.mobile`, with no App ID registered for the new id —
    which stranded the app entirely. This pins them together so the next
    rename is all-or-nothing.
    """
    import json
    import re

    repo = Path(__file__).resolve().parents[5]
    stack = repo / "apps/infra/lib/stacks/service-stack.ts"
    app_json = repo / "apps/mobile/app.json"
    aasa = repo / "apps/frontend/public/.well-known/apple-app-site-association"
    if not (stack.exists() and app_json.exists() and aasa.exists()):
        pytest.skip("running outside a full repo checkout")

    match = re.search(r'APPLE_IAP_BUNDLE_ID:\s*"([^"]+)"', stack.read_text())
    assert match, "APPLE_IAP_BUNDLE_ID not found in service-stack.ts"
    assert match.group(1) == BUNDLE_ID

    ios = json.loads(app_json.read_text())["expo"]["ios"]
    assert ios["bundleIdentifier"] == BUNDLE_ID

    app_ids = json.loads(aasa.read_text())["applinks"]["details"][0]["appIDs"]
    team = ios["appleTeamId"]
    assert app_ids == [f"{team}.{BUNDLE_ID}"]
