"""Tests for the generation-id minting helpers (PR-3 GEN, additive/inert).

No consumer reads either generation yet -- these tests only pin the
minting primitive's contract: real ULIDs, freshly minted on every call.
"""

from ulid import ULID

from core.services.generation_ids import bump_onboarding_generation, new_provision_generation


def test_new_provision_generation_returns_a_real_ulid():
    value = new_provision_generation()
    assert isinstance(value, str)
    # Round-trips through the real ULID parser.
    ULID.from_str(value)


def test_new_provision_generation_mints_a_fresh_value_each_call():
    assert new_provision_generation() != new_provision_generation()


def test_bump_onboarding_generation_returns_a_real_ulid():
    value = bump_onboarding_generation()
    assert isinstance(value, str)
    ULID.from_str(value)


def test_bump_onboarding_generation_mints_a_fresh_value_each_call():
    assert bump_onboarding_generation() != bump_onboarding_generation()
