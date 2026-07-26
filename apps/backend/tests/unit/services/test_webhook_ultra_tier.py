"""Ultra Pro tier derivation in the subscription webhook handler.

`handle_subscription_state` must derive the pricing tier FROM THE PRICE on
the subscription item — never from subscription.metadata (a checkout-time
snapshot that goes stale, the same clobber the provider-choice guard already
defends against). It persists ``plan`` and resizes the container only on the
tier TRANSITION, with an OOM-safe downgrade guard: an oversized company is
left at Ultra size rather than shrunk into an OOM.
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from core.services import stripe_webhook_handlers as handlers  # noqa: E402

ULTRA_PRICE = "price_ultra_test"
FLAT_PRICE = "price_flat_test"
SEAT_PRICE = "price_seat_test"
BASE_PRICE = "price_team_base_test"


@pytest.fixture(autouse=True)
def facts_mock(monkeypatch):
    """The handler refreshes the owner-facts file after persisting state — mock
    it so no test reaches the real billing repo + EFS workspace."""
    mock = AsyncMock()
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", mock)
    return mock


@pytest.fixture(autouse=True)
def ultra_price_configured(monkeypatch):
    """Every test here runs with the Ultra price id configured (CDK-wired in
    dev/prod; empty by default so the app boots without it)."""
    monkeypatch.setattr("core.config.settings.STRIPE_ULTRA_PRICE_ID", ULTRA_PRICE)


@pytest.fixture(autouse=True)
def team_prices_configured(monkeypatch):
    """Sibling of ultra_price_configured above: every test here also runs with
    the team seat + team base price ids configured (CDK-wired in dev/prod;
    empty by default so the app boots without them). Needed because Task 2
    makes team subscriptions reach the tier branch for the first time — tests
    that mix a seat item into `items.data` need a real STRIPE_TEAM_SEAT_PRICE_ID
    for `seat_item` detection to work."""
    monkeypatch.setattr("core.config.settings.STRIPE_TEAM_SEAT_PRICE_ID", SEAT_PRICE)
    monkeypatch.setattr("core.config.settings.STRIPE_TEAM_BASE_PRICE_ID", BASE_PRICE)


@pytest.fixture(autouse=True)
def mock_container_repo():
    """No handler path in these tests should reach real DynamoDB for the
    container row (the trial-conversion branch would stamp it)."""
    with patch.object(handlers, "container_repo") as repo:
        repo.update_fields = AsyncMock(return_value=None)
        repo.get_by_owner_id = AsyncMock(return_value=None)
        yield repo


def _ultra_event(
    *,
    status="active",
    owner_id="org_x",
    item_price=ULTRA_PRICE,
    items_present=True,
    extra_metadata=None,
):
    """Build a customer.subscription.updated event. default_payment_method is
    None so the PM-promotion path stays inert and the test isolates tiering."""
    metadata = {"owner_id": owner_id}
    if extra_metadata:
        metadata.update(extra_metadata)
    obj = {
        "id": "sub_ultra",
        "customer": "cus_x",
        "status": status,
        "metadata": metadata,
        "default_payment_method": None,
        "items": {"data": [{"id": "si_1", "quantity": 1, "price": {"id": item_price}}] if items_present else []},
    }
    return {"id": "evt_ultra", "type": "customer.subscription.updated", "data": {"object": obj}}


def _account(*, plan="standard", owner_id="org_x"):
    return {
        "owner_id": owner_id,
        "owner_type": "org",
        "stripe_customer_id": "cus_x",
        # A live→live event keeps the entering_live provider backup inert so
        # these tests stay scoped to tiering.
        "subscription_status": "active",
        "stripe_subscription_id": "sub_ultra",
        "plan": plan,
    }


async def _drive(event, *, account, agents=None):
    """Run handle_subscription_state with the billing repo, ecs-manager
    singleton, and Paperclip agent fetch all mocked. Returns (repo, mgr)."""
    mgr = MagicMock()
    mgr.resize_user_container = AsyncMock(return_value="arn:new")
    with (
        patch.object(handlers, "billing_repo") as repo,
        patch("core.containers.get_ecs_manager", return_value=mgr),
        patch(
            "core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner",
            AsyncMock(return_value=list(agents or [])),
        ),
    ):
        repo.get_by_owner_id = AsyncMock(return_value=account)
        repo.set_subscription = AsyncMock()
        repo.set_provider_choice = AsyncMock()
        repo.set_team_subscription = AsyncMock()
        repo.set_plan = AsyncMock()
        await handlers.handle_subscription_state(event, MagicMock())
        return repo, mgr


@pytest.mark.asyncio
async def test_upgrade_to_ultra_sets_plan_and_resizes_up():
    """(a) .updated on the Ultra price for a previously-standard owner →
    set_plan("ultra_pro") + resize to 2048/8192."""
    repo, mgr = await _drive(_ultra_event(item_price=ULTRA_PRICE), account=_account(plan="standard"))

    repo.set_plan.assert_awaited_once()
    assert repo.set_plan.await_args.kwargs["plan"] == "ultra_pro"
    mgr.resize_user_container.assert_awaited_once_with("org_x", new_cpu="2048", new_memory="8192")


@pytest.mark.asyncio
async def test_ultra_detected_when_its_price_is_not_the_first_item():
    """Tier detection scans by price id — it must not depend on item order.

    Ultra subs are single-item today, so this cannot happen yet. It becomes
    reachable the moment one grows a second line item (metered overage, a
    Stripe-side add-on or coupon). Under the old items[0] read the ultra price
    at index 1 resolved to plan "standard", silently downgrading a paying Ultra
    owner AND resizing their container down.
    """
    event = _ultra_event(item_price=ULTRA_PRICE)
    event["data"]["object"]["items"]["data"] = [
        {"id": "si_addon", "quantity": 1, "price": {"id": "price_some_addon"}},
        {"id": "si_ultra", "quantity": 1, "price": {"id": ULTRA_PRICE}},
    ]

    repo, mgr = await _drive(event, account=_account(plan="standard"))

    assert repo.set_plan.await_args.kwargs["plan"] == "ultra_pro"
    mgr.resize_user_container.assert_awaited_once_with("org_x", new_cpu="2048", new_memory="8192")


@pytest.mark.asyncio
async def test_downgrade_small_company_resizes_down():
    """(b) Ultra→flat with ≤10 agents → set_plan("standard") + resize down to
    the standard box (PER_USER constants)."""
    from core.containers.ecs_manager import PER_USER_CPU, PER_USER_MEMORY_MIB

    repo, mgr = await _drive(
        _ultra_event(item_price=FLAT_PRICE),
        account=_account(plan="ultra_pro"),
        agents=[{"id": f"a{i}"} for i in range(5)],
    )

    assert repo.set_plan.await_args.kwargs["plan"] == "standard"
    mgr.resize_user_container.assert_awaited_once_with("org_x", new_cpu=PER_USER_CPU, new_memory=PER_USER_MEMORY_MIB)


@pytest.mark.asyncio
async def test_downgrade_oversized_company_defers_resize_with_metric():
    """(c) Ultra→flat with >10 agents → set_plan("standard") + NO down-resize
    (leave at Ultra size to avoid OOM) + the deferred metric."""
    with patch.object(handlers, "put_metric") as metric:
        repo, mgr = await _drive(
            _ultra_event(item_price=FLAT_PRICE),
            account=_account(plan="ultra_pro"),
            agents=[{"id": f"a{i}"} for i in range(11)],
        )

    assert repo.set_plan.await_args.kwargs["plan"] == "standard"
    mgr.resize_user_container.assert_not_awaited()
    assert "ultra.downgrade_deferred_oversized" in [c.args[0] for c in metric.call_args_list]


@pytest.mark.asyncio
async def test_downgrade_with_unavailable_count_defers_resize():
    """OOM-safety: on downgrade, an agent count of 0 means Paperclip was
    unreachable (every provisioned owner always has Friday + Atlas seeded, so a
    reachable list is never empty). Treating 0 as "small" would shrink an
    oversized owner into an OOM that never self-heals. Defer the down-resize +
    emit the unknown-count metric; plan still persists standard."""
    with patch.object(handlers, "put_metric") as metric:
        repo, mgr = await _drive(
            _ultra_event(item_price=FLAT_PRICE),
            account=_account(plan="ultra_pro"),
            agents=[],  # fetch returned [] → Paperclip unreachable
        )

    assert repo.set_plan.await_args.kwargs["plan"] == "standard"
    mgr.resize_user_container.assert_not_awaited()
    names = [c.args[0] for c in metric.call_args_list]
    assert "ultra.downgrade_deferred_unknown_count" in names
    assert "ultra.downgrade_deferred_oversized" not in names


@pytest.mark.asyncio
async def test_metadata_tier_is_ignored_price_wins():
    """(d) subscription.metadata claiming a DIFFERENT tier is ignored — the
    Ultra price still drives ultra_pro + the up-resize."""
    event = _ultra_event(
        item_price=ULTRA_PRICE,
        extra_metadata={"plan": "standard", "tier": "standard"},
    )
    repo, mgr = await _drive(event, account=_account(plan="standard"))

    assert repo.set_plan.await_args.kwargs["plan"] == "ultra_pro"
    mgr.resize_user_container.assert_awaited_once_with("org_x", new_cpu="2048", new_memory="8192")


@pytest.mark.asyncio
async def test_empty_items_does_not_change_plan_or_resize():
    """(e) A partial .updated payload lacking items.data must not downgrade or
    shrink an Ultra owner: no set_plan, no resize."""
    repo, mgr = await _drive(
        _ultra_event(items_present=False),
        account=_account(plan="ultra_pro"),
    )

    repo.set_plan.assert_not_awaited()
    mgr.resize_user_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_transition_ultra_to_ultra_persists_plan_but_no_resize():
    """(f) An already-Ultra owner receiving another Ultra-price event →
    set_plan("ultra_pro") (idempotent) but NO resize (no tier transition)."""
    repo, mgr = await _drive(_ultra_event(item_price=ULTRA_PRICE), account=_account(plan="ultra_pro"))

    assert repo.set_plan.await_args.kwargs["plan"] == "ultra_pro"
    mgr.resize_user_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_standard_to_standard_persists_plan_but_no_resize():
    """A flat-price event on a standard owner persists plan="standard" (harmless
    idempotent write) and never resizes."""
    repo, mgr = await _drive(_ultra_event(item_price=FLAT_PRICE), account=_account(plan="standard"))

    assert repo.set_plan.await_args.kwargs["plan"] == "standard"
    mgr.resize_user_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_set_plan_skips_resize():
    """F4: a failed durable plan write (transient DDB) must SKIP the resize —
    resizing without persisting the plan would leave the container size changed
    while the row still reads the old plan, so a later flat event would
    mis-decide the next resize. The webhook must also not 500."""
    mgr = MagicMock()
    mgr.resize_user_container = AsyncMock(return_value="arn:new")
    with (
        patch.object(handlers, "billing_repo") as repo,
        patch("core.containers.get_ecs_manager", return_value=mgr),
        patch(
            "core.services.paperclip_openclaw_pairing.fetch_paperclip_agents_for_owner",
            AsyncMock(return_value=[]),
        ),
    ):
        repo.get_by_owner_id = AsyncMock(return_value=_account(plan="standard"))
        repo.set_subscription = AsyncMock()
        repo.set_provider_choice = AsyncMock()
        repo.set_team_subscription = AsyncMock()
        # The durable plan write fails — the resize must not follow it.
        repo.set_plan = AsyncMock(side_effect=RuntimeError("ddb down"))
        # An upgrade event (would normally resize UP) — must NOT raise.
        await handlers.handle_subscription_state(_ultra_event(item_price=ULTRA_PRICE), MagicMock())

    repo.set_plan.assert_awaited_once()
    mgr.resize_user_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_team_seat_event_reaches_tier_logic_keyed_by_price():
    """INVERTED from the pre-Task-2 assumption that a team-seat event never
    reaches tier logic. That test protected "tier logic reacts to the
    correct price" — it just expressed it as "team subs are blanket-excluded",
    which is exactly the exclusion this task removes. Tier and headcount are
    independent axes: a team's tier rides its BASE item, so the same
    price-keyed logic that applies to a personal owner must apply here too —
    a team-seat event on the standard base price ties plan to "standard";
    the same event on the ultra price ties plan to "ultra_pro". No seat item
    is present on either event, so set_team_subscription correctly does not
    fire — this test is scoped to tier detection, not seat/tier composition
    (see test_ultra_team_persists_seat_state_and_tier_together for that)."""
    standard_event = _ultra_event(item_price=BASE_PRICE, extra_metadata={"purpose": "team_seat"})
    repo, mgr = await _drive(standard_event, account=_account(plan="standard"))
    assert repo.set_plan.await_args.kwargs["plan"] == "standard"
    repo.set_team_subscription.assert_not_awaited()
    mgr.resize_user_container.assert_not_awaited()

    ultra_event = _ultra_event(item_price=ULTRA_PRICE, extra_metadata={"purpose": "team_seat"})
    repo2, mgr2 = await _drive(ultra_event, account=_account(plan="standard"))
    assert repo2.set_plan.await_args.kwargs["plan"] == "ultra_pro"
    repo2.set_team_subscription.assert_not_awaited()
    mgr2.resize_user_container.assert_awaited_once_with("org_x", new_cpu="2048", new_memory="8192")


@pytest.mark.asyncio
async def test_team_sub_on_the_ultra_price_is_tiered_and_resized_up():
    """A team's tier lives on its base item. Excluding team subs from tier
    logic is what made Ultra unavailable to orgs."""
    event = _ultra_event(item_price=ULTRA_PRICE)
    event["data"]["object"]["metadata"]["purpose"] = "team_seat"
    event["data"]["object"]["items"]["data"] = [
        {"id": "si_ultra", "quantity": 1, "price": {"id": ULTRA_PRICE}},
        {"id": "si_seat", "quantity": 4, "price": {"id": SEAT_PRICE}},
    ]

    repo, mgr = await _drive(event, account=_account(plan="standard"))

    assert repo.set_plan.await_args.kwargs["plan"] == "ultra_pro"
    mgr.resize_user_container.assert_awaited_once_with("org_x", new_cpu="2048", new_memory="8192")


@pytest.mark.asyncio
async def test_ultra_team_persists_seat_state_and_tier_together():
    """Both webhook branches now fire on one subscription for the first time.
    They must not fight: seat persistence writes subscription_item_id +
    seat_count from the SEAT item; tier persistence writes plan + resizes."""
    event = _ultra_event(item_price=ULTRA_PRICE)
    event["data"]["object"]["metadata"]["purpose"] = "team_seat"
    event["data"]["object"]["items"]["data"] = [
        {"id": "si_ultra", "quantity": 1, "price": {"id": ULTRA_PRICE}},
        {"id": "si_seat", "quantity": 4, "price": {"id": SEAT_PRICE}},
    ]

    repo, _mgr = await _drive(event, account=_account(plan="standard"))

    # Seat state comes from the SEAT item, never the ultra/base item.
    seat_kwargs = repo.set_team_subscription.await_args.kwargs
    assert seat_kwargs["subscription_item_id"] == "si_seat"
    assert seat_kwargs["seat_count"] == 4
    # Tier state persisted on the same event.
    assert repo.set_plan.await_args.kwargs["plan"] == "ultra_pro"


@pytest.mark.asyncio
async def test_team_sub_on_the_standard_base_is_tiered_down():
    """Downgrade happens in the Stripe portal (there is no downgrade endpoint);
    the webhook reacts. A team back on the standard base is plan="standard" and
    resizes down, honouring the >10-agent deferral like any other owner."""
    from core.containers.ecs_manager import PER_USER_CPU, PER_USER_MEMORY_MIB

    event = _ultra_event(item_price=BASE_PRICE)
    event["data"]["object"]["metadata"]["purpose"] = "team_seat"
    event["data"]["object"]["items"]["data"] = [
        {"id": "si_base", "quantity": 1, "price": {"id": BASE_PRICE}},
        {"id": "si_seat", "quantity": 4, "price": {"id": SEAT_PRICE}},
    ]

    repo, mgr = await _drive(
        event,
        account=_account(plan="ultra_pro"),
        agents=[{"id": f"a{i}"} for i in range(5)],
    )

    assert repo.set_plan.await_args.kwargs["plan"] == "standard"
    mgr.resize_user_container.assert_awaited_once_with("org_x", new_cpu=PER_USER_CPU, new_memory=PER_USER_MEMORY_MIB)
