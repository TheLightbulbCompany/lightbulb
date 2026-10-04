from huddle_agent.request_list import MAX_WORKING, RequestList, ack_line

NAMES = {"pulse": "Pulse", "forge": "Forge", "friday": "Friday"}.get
SPLIT = [
    {"agentId": "pulse", "task": "an update on the go-to-market strategy"},
    {"agentId": "forge", "task": "what our PRs look like"},
    {"agentId": "friday", "task": "a summary of our rooms"},
]


def routed():
    rl = RequestList()
    rl.pending(7, "u1", ["pulse", "forge", "friday"], 1.0)
    return rl, rl.settle(7, "u1", SPLIT, 2.0)


def started(rl, rows):
    for r, run in zip(rows, ("rp", "rf", "rr")):
        rl.start(r)
        rl.bind(r, run)


def test_pending_rows_show_on_the_strip_until_route_settles_them():
    rl = RequestList()
    rl.pending(7, "u1", ["pulse", "forge", "forge"], 1.0)
    assert [r.agent for r in rl.items] == ["pulse", "forge"]
    assert (
        rl.working("forge") and rl.task("forge") == "" and rl.startable() == []
    )  # never started
    assert rl.drop_pending(7) and rl.items == [] and not rl.drop_pending(7)


def test_settle_replaces_the_pending_rows_with_the_split_in_order():
    rl, rows = routed()
    assert [(r.agent, r.task, r.status, r.routed, r.turn) for r in rl.items] == [
        (x["agentId"], x["task"], "queued", True, 7) for x in SPLIT
    ]
    assert rows == rl.items


def test_at_most_four_work_at_once_and_a_freed_slot_starts_the_next():
    rl = RequestList()
    rl.settle(1, "u1", [{"agentId": f"a{i}", "task": f"t{i}"} for i in range(6)], 0.0)
    first = rl.startable()
    assert [r.agent for r in first] == ["a0", "a1", "a2", "a3"] and MAX_WORKING == 4
    for i, r in enumerate(first):
        rl.start(r)
        rl.bind(r, f"run{i}")
    assert rl.startable() == []
    rl.ready("run0")
    assert [r.agent for r in rl.startable()] == ["a4"]


def test_lifecycle_order_and_postable():
    rl, (p, f, fr) = routed()
    started(rl, (p, f, fr))
    assert rl.order("rf") == 1 and rl.by_run("rf") == [f] and rl.by_run(None) == []
    rl.ready("rf")
    assert f.status == "ready" and not rl.working("forge")
    rl.delivered({"rf"})
    assert f.status == "delivered"
    rl.fail(fr)
    rl.delivered({"rr"})  # a failed row stays failed
    assert fr.status == "failed" and not rl.postable("rr")
    assert rl.postable("rp")
    assert (
        rl.no_post("forge") is None and not p.no_post
    )  # a named agent with nothing open flags nothing
    assert rl.no_post() is p  # nobody named: the room's newest open row
    assert p.no_post and not rl.postable("rp")
    rl.suppress("other")
    assert not rl.postable("other") and rl.postable("unknown")


def test_cancel_by_agent_or_all():
    rl, (p, f, fr) = routed()
    assert rl.cancel("forge") == [f] and f.status == "cancelled"
    assert rl.cancel() == [p, fr] and rl.open() == []


def test_lines_say_what_teammates_are_on():
    rl, (p, f, fr) = routed()
    rl.start(f)
    rl.bind(f, "rf")
    rl.ready("rf")
    assert rl.lines(NAMES, skip_agent="pulse") == [
        'Forge has an answer ready on "what our PRs look like"',
        'Friday is queued for "a summary of our rooms"',
    ]
    assert rl.queued() == [p, fr]
    rl.start(fr)
    assert rl.lines(NAMES)[-1] == 'Friday is answering "a summary of our rooms"'


def test_version_moves_on_every_change():
    rl = RequestList()
    seen = [rl.version]
    rl.pending(1, "u1", ["a"], 0.0)
    seen.append(rl.version)
    (r,) = rl.settle(1, "u1", [{"agentId": "a", "task": "t"}], 0.0)
    seen.append(rl.version)
    rl.start(r)
    seen.append(rl.version)
    assert seen == sorted(set(seen))


def test_the_strip_task_is_cut_to_sixty():
    rl = RequestList()
    rl.settle(1, "u1", [{"agentId": "a", "task": "x" * 100}], 0.0)
    assert rl.task("a") == "x" * 60 and rl.working("a")


def test_the_acknowledgement_names_every_task_once():
    rl, rows = routed()
    started(rl, rows)  # every request starts before its acknowledgement
    assert ack_line(rows, NAMES) == (
        "You are on an update on the go-to-market strategy. Forge is on what our PRs look like. "
        "Friday is on a summary of our rooms. Say that in one short sentence, then wait."
    )


def test_a_cancelled_row_is_never_postable():
    rl = RequestList()
    (r,) = rl.settle(1, "u1", [{"agentId": "a", "task": "t"}], 0.0)
    rl.start(r)
    rl.bind(r, "x")
    rl.cancel()
    rl.ready("x")
    assert r.status == "cancelled" and not rl.postable("x")


def test_settle_after_everyone_stop_adds_nothing():
    rl = RequestList()
    rl.pending(1, "u1", ["a", "b"], 0.0)
    rl.cancel(mark=1)
    assert rl.settle(1, "u1", [{"agentId": "a", "task": "t"}], 1.0) == []
    assert rl.startable() == [] and rl.items == []


def test_a_line_with_no_task_omits_the_quotes():
    rl = RequestList()
    rl.pending(1, "u1", ["pulse"], 0.0)
    rl.items[0].routed = True
    assert rl.lines(NAMES) == ["Pulse is queued for"]


def test_a_repeated_agent_in_one_split_keeps_every_ask_with_its_own_id():
    rl = RequestList()
    rows = rl.settle(1, "u1", [{"agentId": "forge", "task": "x"}, {"agentId": "pulse", "task": "p"},
                               {"agentId": "forge", "task": "y", "by": "fit"}], 0.0)  # fmt: skip
    assert [(r.agent, r.task, r.by) for r in rows] == [
        ("forge", "x", ""),
        ("pulse", "p", ""),
        ("forge", "y", "fit"),
    ]
    assert (
        len({r.id for r in rows}) == 3
        and rl.get(rows[2].id) is rows[2]
        and rl.get("nope") is None
    )
    assert rl.startable() == rows[:2]  # forge works one row at a time
    rl.start(rows[0])
    rl.bind(rows[0], "r")
    assert rl.startable() == [rows[1]]
    rl.fail(rows[0])
    assert rl.startable() == [rows[1], rows[2]]


def test_ids_are_never_reused_and_pending_rows_have_one():
    rl = RequestList()
    rl.pending(1, "u", ["a"], 0.0)
    pid = rl.items[0].id
    (r,) = rl.settle(1, "u", [{"agentId": "a", "task": "t"}], 0.0)
    assert pid and r.id and r.id != pid


def test_five_agents_start_four_and_two_agents_start_together():
    rl = RequestList()
    rl.settle(1, "u", [{"agentId": f"a{i}", "task": "t"} for i in range(5)], 0.0)
    assert len(rl.startable()) == 4
    rl2 = RequestList()
    rl2.settle(
        1, "u", [{"agentId": "a", "task": "1"}, {"agentId": "b", "task": "2"}], 0.0
    )
    assert [r.agent for r in rl2.startable()] == ["a", "b"]


def test_for_route_lists_open_rows_then_three_newest_delivered_capped_at_twelve():
    rl = RequestList()
    rows = rl.settle(1, "u", [{"agentId": "a", "task": f"t{i}"} for i in range(5)], 0.0)
    for i, r in enumerate(rows[:4]):
        rl.start(r)
        rl.bind(r, f"run{i}")
    rl.delivered({"run0", "run1", "run2", "run3"})
    rl.items[4].task = "x" * 300
    out = rl.for_route()
    assert [o["task"][:2] for o in out] == ["xx", "t1", "t2", "t3"] and len(
        out[0]["task"]
    ) == 200
    assert out[0] == {
        "id": rows[4].id,
        "agentId": "a",
        "task": "x" * 200,
        "status": "queued",
    }
    big = RequestList()
    big.settle(1, "u", [{"agentId": f"a{i}", "task": "t"} for i in range(15)], 0.0)
    assert [o["agentId"] for o in big.for_route()] == [
        f"a{i}" for i in range(3, 15)
    ]  # the newest stay
    for i, r in enumerate(big.items[:3]):
        big.start(r)
        big.bind(r, f"d{i}")
    big.delivered({"d0", "d1", "d2"})
    big.settle(2, "u", [{"agentId": "z", "task": "t"}], 0.0)
    out = [o["agentId"] for o in big.for_route()]
    assert out == [f"a{i}" for i in range(7, 15)] + ["z", "a0", "a1", "a2"]


def rows_for(*pairs):
    """Working rows: the acknowledgement of work that has started."""
    rows = RequestList().settle(
        1, "u", [{"agentId": a, "task": t} for a, t in pairs], 0.0
    )
    for r in rows:
        r.status = "working"
    return rows


def test_ack_line_one_row_mine():
    rows = rows_for(("pulse", "the update"))
    assert (
        ack_line(rows, NAMES, "pulse")
        == "You are on the update. Say that in one short sentence, then wait."
    )


def test_ack_line_two_mine():
    rows = rows_for(("pulse", "A"), ("pulse", "B"))
    assert (
        ack_line(rows, NAMES)
        == "You are on A and on B. Say that in one short sentence, then wait."
    )


def test_ack_line_mine_and_another_agents_two():
    rows = rows_for(("pulse", "A"), ("forge", "C"), ("forge", "D"))
    assert ack_line(rows, NAMES, "pulse") == (
        "You are on A. Forge is on C and on D. Say that in one short sentence, then wait."
    )


def test_ack_line_none_mine():
    rows = rows_for(("forge", "C"))
    assert (
        ack_line(rows, NAMES, "pulse")
        == "Forge is on C. Say that in one short sentence, then wait."
    )


def test_ack_line_names_three_and_counts_the_rest():
    rows = rows_for(
        ("pulse", "A"), ("forge", "B"), ("friday", "C"), ("pulse", "D"), ("forge", "E")
    )
    assert ack_line(rows, NAMES) == (
        "You are on A and on D. Forge is on B. And 2 more, shown in the room. "
        "Say that in one short sentence, then wait."
    )


def test_one_agents_stop_while_route_decides_drops_only_its_row():
    rl = RequestList()
    rl.pending(1, "u", ["forge", "friday"], 0.0)
    rl.cancel("forge", mark=1)
    rows = rl.settle(
        1,
        "u",
        [{"agentId": "forge", "task": "a"}, {"agentId": "friday", "task": "b"}],
        1.0,
    )
    assert [r.agent for r in rows] == ["friday"] and [r.agent for r in rl.items] == [
        "friday"
    ]


def test_ack_line_says_queued_rows_of_me_are_next_in_line():
    rows = rows_for(("pulse", "A"), ("pulse", "B"), ("forge", "C"))
    rows[1].status = rows[2].status = "queued"  # Forge's queued row still reads "is on"
    assert ack_line([rows[1]], NAMES, "pulse") == (
        "B is next in line for you. Say that in one short sentence, then wait."
    )
    assert ack_line(rows, NAMES, "pulse") == (
        "You are on A. B is next in line for you. Forge is on C. Say that in one short sentence, then wait."
    )
