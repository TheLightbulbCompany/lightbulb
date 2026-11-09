"""Bounded, verbatim context at a delegation boundary; no model or network calls."""

from huddle_agent.mouth import Context


def test_captured_meeting_sales_goal_survives_a_long_product_discussion():
    context = Context()
    # Nov 8 Meet: the request accepted a proposal after a long product discussion.
    context.add("Maya", "We discussed our earlier product pivots. " * 150)
    context.add(
        "Sam",
        "Our goal is five businesses in the next month because five businesses paying 250 bucks covers all of our expenses for the product.",
    )
    request = "Sure, Nova, go figure that out. That'd be awesome. Thank you"
    sent = context.take(request, max_chars=4000)
    assert sent.startswith(request) and len(sent) <= 4000
    assert "five businesses in the next month" in sent and "250 bucks" in sent
    assert context.turns == []


def test_sdk_assistant_proposal_is_handed_over_with_the_location_followup():
    context = Context()
    context.add(
        "Sam",
        "If you could look at places around Miami or Philly, that'd be great as well.",
    )
    sent = context.take(
        "Sure, Nova, go figure that out.",
        max_chars=4000,
        model_turns=[
            "assistant: I can research prospective businesses for the scheduling agent."
        ],
    )
    assert "assistant: I can research prospective businesses" in sent
    assert "Miami or Philly" in sent


def test_one_oversized_recent_turn_is_bounded_and_explicitly_marked():
    context = Context()
    context.add(
        "Sam", "Earlier words. " * 1000 + "The latest instruction is Miami and Philly."
    )
    sent = context.take("Go figure that out.", max_chars=4000)
    assert len(sent) <= 4000 and sent.startswith("Go figure that out.")
    assert "[earlier words omitted]" in sent and sent.endswith(
        "The latest instruction is Miami and Philly."
    )


def test_context_cannot_displace_the_current_request():
    context = Context()
    context.add("Sam", "history " * 1000)
    request = "Current request " * 200
    sent = context.take(request, max_chars=4000)
    assert sent.startswith(request.strip()) and len(sent) <= 4000


def test_sdk_user_paraphrase_does_not_displace_the_diarized_goal_or_assistant_proposal():
    context = Context()
    context.add("Sam", "Our goal is five businesses in the next month, paying 250 bucks each.")
    context.add("Sam", "Look at places around Miami or Philly.")
    sent = context.take("Sure, go figure that out.", model_turns=[
        "user: We discussed the earlier product pivots and fundraising. " * 100,
        "assistant: I can research prospective businesses for the scheduling agent.",
    ])
    assert len(sent) <= 4000
    assert "five businesses in the next month" in sent and "Miami or Philly" in sent
    assert "assistant: I can research prospective businesses" in sent
