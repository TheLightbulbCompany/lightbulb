from core.services import a2a_rules as r

R = r.default_rules("org_1", ["user_admin"])
K = dict(org="org_1", requester_org="org_1", chain_agents=[], session_person=None, exception=None)


def test_in_org_message_to_agent_is_allowed_by_r1():
    d = r.decide(R, requester_person="user_x", resource="agent:friday", skill="message", **K)
    assert (d.effect, d.rule) == ("allow", "r1")


def test_outside_org_is_denied_before_any_rule():
    d = r.decide(
        R, requester_person="user_x", resource="agent:friday", skill="message", **{**K, "requester_org": "org_other"}
    )
    assert (d.effect, d.rule) == ("deny", "outside_org")


def test_ask_session_falls_to_default_hold_with_session_person_as_holder():
    d = r.decide(
        R,
        requester_person="user_x",
        resource="session:abc",
        skill="ask_session",
        **{**K, "session_person": "user_owner"},
    )
    assert (d.effect, d.rule, d.holders) == ("hold", "default", ["user_owner"])


def test_requester_who_is_the_holder_is_allowed_by_rule_holder():
    d = r.decide(
        R,
        requester_person="user_owner",
        resource="session:abc",
        skill="ask_session",
        **{**K, "session_person": "user_owner"},
    )
    assert (d.effect, d.rule) == ("allow", "holder")


def test_deny_rule_wins_over_an_earlier_allow():
    rules = R.model_copy(
        update={
            "rules": [
                *R.rules,
                r.Rule(id="d1", resource="agent:friday", skill="message", requesters=["user_x"], effect="deny"),
            ]
        }
    )
    d = r.decide(rules, requester_person="user_x", resource="agent:friday", skill="message", **K)
    assert (d.effect, d.rule) == ("deny", "d1")


def test_exception_row_allows_within_window():
    d = r.decide(
        R,
        requester_person="user_x",
        resource="session:abc",
        skill="ask_session",
        **{**K, "session_person": "user_owner", "exception": {"id": "exc-9"}},
    )
    assert (d.effect, d.rule) == ("allow", "exception:exc-9")


def test_exception_repo_roundtrip_and_expiry(repos):
    import asyncio

    from core.repositories import a2a_repo

    eid = asyncio.run(
        a2a_repo.put_exception(
            "org_1", "session:abc", "ask_session", "user_x", "sha256:aa", holder="user_owner", task_id="t1", now=1_000
        )
    )

    def get(skill="ask_session", requester="user_x", now=1_001):
        return asyncio.run(a2a_repo.get_exception("org_1", "session:abc", skill, requester, "sha256:aa", now))

    assert get()["id"] == eid
    assert get(skill="message") is None
    assert get(requester="user_bob") is None  # scoped to the requester the holder approved
    assert get(now=1_000 + 86_401) is None


def test_matching_deny_beats_an_exception():
    rules = R.model_copy(
        update={
            "rules": [
                *R.rules,
                r.Rule(id="d1", resource="session:*", skill="ask_session", requesters=["user_x"], effect="deny"),
            ]
        }
    )
    d = r.decide(
        rules,
        requester_person="user_x",
        resource="session:abc",
        skill="ask_session",
        **{**K, "session_person": "user_owner", "exception": {"id": "exc-9"}},
    )
    assert (d.effect, d.rule) == ("deny", "d1")


def test_rules_roundtrip_through_repo(repos):
    import asyncio

    from core.repositories import a2a_repo

    asyncio.run(a2a_repo.put_rules("org_1", R.model_dump(by_alias=True)))
    got = asyncio.run(a2a_repo.get_rules("org_1"))
    assert r.Rules.model_validate(got) == R


def test_holder_whose_request_a_rule_allows_is_allowed_not_rejected_as_self():
    d = r.decide(R, requester_person="user_admin", resource="agent:friday", skill="message", **K)
    assert (d.effect, d.rule) == ("allow", "r1")


def test_agent_originated_ask_is_held_for_its_stand_in_requester_not_self():
    d = r.decide(
        R,
        requester_person="user_owner",
        resource="session:abc",
        skill="ask_session",
        **{**K, "session_person": "user_owner"},
        origin_is_person=False,
    )
    assert (d.effect, d.holders) == ("hold", ["user_owner"])
