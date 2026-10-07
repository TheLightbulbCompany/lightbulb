"""Marketplace listing state machine — reference-model comparison.

Generates a random sequence of seller/admin actions over a single fresh
listing and applies each to BOTH a Python reference model and the real
marketplace_service, asserting they never diverge.

INVARIANTS:
  * the real status after each action matches the reference state machine
    (legal transitions only: draft -> review -> published, review -> draft)
  * illegal actions raise the documented exception and leave status unchanged
  * at most one row per listing_id is ever 'published'

Each example creates a new UUID listing, so there is no cross-example state
bleed through the persistent moto fixture (unlike the usage counters)."""

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_ACTIONS = ["replace", "submit", "approve", "reject"]


class _Model:
    """Reference state machine for a single listing version."""

    def __init__(self):
        self.status = "draft"
        self.uploaded = False

    def predict(self, action):
        """Return ("ok", new_status) or ("raise", ExceptionName) without mutating."""
        s, up = self.status, self.uploaded
        if action == "replace":
            return ("ok", "draft") if s == "draft" else ("raise", "InvalidStateError")
        if action == "submit":
            if s == "draft" and up:
                return ("ok", "review")
            if s == "draft" and not up:
                return ("raise", "ArtifactNotUploadedError")
            return ("raise", "InvalidStateError")
        if action == "approve":
            return ("ok", "published") if s == "review" else ("raise", "InvalidStateError")
        if action == "reject":
            return ("ok", "draft") if s == "review" else ("raise", "InvalidStateError")
        raise AssertionError(action)

    def commit(self, action):
        if action == "replace" and self.status == "draft":
            self.uploaded = True
        elif action == "submit" and self.status == "draft" and self.uploaded:
            self.status = "review"
        elif action == "approve" and self.status == "review":
            self.status = "published"
        elif action == "reject" and self.status == "review":
            self.status = "draft"


@pytest.mark.chaos
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(actions=st.lists(st.sampled_from(_ACTIONS), min_size=0, max_size=12))
@pytest.mark.asyncio
async def test_marketplace_transitions_match_reference(chaos_ddb, actions):
    from core.repositories import marketplace_repo
    from core.services import marketplace_service as mp

    seller = "user_chaos_seller"
    draft = await mp.create_draft(
        seller_id=seller,
        slug="listing-0",
        name="Chaos Listing",
        description_md="x",
        format="agent",
        price_cents=0,
        tags=[],
        artifact_bytes=b"",
        manifest={},
    )
    lid = draft["listing_id"]
    model = _Model()

    async def _run(action):
        if action == "replace":
            await mp.replace_artifact(
                listing_id=lid, seller_id=seller, artifact_bytes=b"x", manifest={"k": "v"}, openclaw_slice={}
            )
        elif action == "submit":
            await mp.submit_for_review(listing_id=lid, seller_id=seller, version=1)
        elif action == "approve":
            await mp.approve(listing_id=lid, version=1, approved_by="admin_chaos")
        elif action == "reject":
            await mp.reject(listing_id=lid, version=1, notes="no", rejected_by="admin_chaos")

    for action in actions:
        kind, expected = model.predict(action)
        if kind == "raise":
            with pytest.raises((mp.InvalidStateError, mp.ArtifactNotUploadedError)) as exc:
                await _run(action)
            assert type(exc.value).__name__ == expected, (
                f"action={action} from status={model.status}/uploaded={model.uploaded}: "
                f"expected {expected}, got {type(exc.value).__name__}"
            )
        else:
            await _run(action)
            model.commit(action)
            row = await mp.get_by_id(listing_id=lid, version=1)
            assert row is not None and row["status"] == model.status, (
                f"after {action}: DB status={row and row['status']} != model {model.status}"
            )

        # at-most-one-published invariant: count published rows across ALL
        # versions of this listing_id (get_published_by_id is the wrong probe —
        # it falls back to the highest version when nothing is published).
        resp = marketplace_repo._listings_table().query(
            KeyConditionExpression="listing_id = :l",
            ExpressionAttributeValues={":l": lid},
        )
        published_rows = [r for r in resp.get("Items", []) if r.get("status") == "published"]
        assert len(published_rows) <= 1, f"{len(published_rows)} published rows for {lid}"
