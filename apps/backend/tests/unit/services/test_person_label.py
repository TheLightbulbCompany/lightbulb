"""A person with no name is never labelled with an Apple relay's random local part."""

import pytest

from core.services import clerk_admin, messaging, onboarding_brief
from routers.teams.agents import _member_row

RELAY = "m2jdw4mbmt@privaterelay.appleid.com"


def _user(**fields) -> dict:
    email = fields.pop("email", None)
    return {**fields, "email_addresses": [{"id": "e1", "email_address": email}] if email else []}


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        (_user(first_name="Priya", last_name="Sharma", username="ps", email=RELAY), "Priya Sharma"),
        (_user(first_name="Priya", email="p@acme.co"), "Priya"),
        (_user(username="priya", email=RELAY), "priya"),
        (_user(email="priya.sharma@acme.co"), "priya.sharma"),
        (_user(email=RELAY), "New member"),
        (_user(email=RELAY.upper()), "New member"),
        (_user(), "user_1"),
        (None, "user_1"),
    ],
)
async def test_resolve_human_display_name(monkeypatch, user, expected):
    async def get_user(_user_id):
        return user

    monkeypatch.setattr(clerk_admin, "get_user", get_user)
    assert await messaging._resolve_human_display_name("user_1") == expected


def test_the_other_email_fallbacks_follow_the_same_rule():
    row = {"user_id": "user_1", "first_name": "", "last_name": ""}
    assert messaging._display_name_from_membership({**row, "email": RELAY}) == "New member"
    assert messaging._display_name_from_membership({**row, "email": "sam@acme.co"}) == "sam"
    assert messaging._display_name_from_membership({**row, "email": ""}) == "user_1"
    # The @handle comes from the name instead of the relay's local part.
    assert messaging._email_local_part(RELAY) is None
    assert messaging._email_local_part("sam@acme.co") == "sam"
    # Every client renders the roster row's ``name || email``.
    assert _member_row("user_1", "", "", RELAY, None)["name"] == "New member"
    assert _member_row("user_1", "", "", "sam@acme.co", None)["name"] == "sam@acme.co"
    assert _member_row("user_1", "Sam", "", RELAY, None)["name"] == "Sam"
    assert onboarding_brief.first_name_from_email(RELAY) == "there"
    assert onboarding_brief.first_name_from_email("priya.sharma@acme.co") == "Priya Sharma"
