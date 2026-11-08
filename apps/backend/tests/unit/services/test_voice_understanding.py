"""Conversation understanding §4.2 step 1: where messaging cuts a spoken line into parts."""

import pytest

from core.services import messaging


# ---- the cut (§4.2 step 1) ----


def parts(line):
    return [messaging._clean(line[s:e], into) for kind, s, e, _, into in messaging._pieces(line, [], cues=True)]


@pytest.mark.parametrize(
    "line,want",
    [
        (
            "I also want to know what features we've shipped and what PRs are currently open.",
            ["I also want to know what features we've shipped", "what PRs are currently open"],
        ),
        (
            "check if Stripe paid out yesterday, see if any new support tickets came in, and tell me how many opened it",
            [
                "check if Stripe paid out yesterday",
                "see if any new support tickets came in",
                "tell me how many opened it",
            ],
        ),
        (
            "can you see how many trials converted this week and uh is the api still throwing those five hundreds",
            ["can you see how many trials converted this week", "is the api still throwing those five hundreds"],
        ),
        (
            "im gonna rattle off a bunch of stuff and you guys sort it out after um first whats our burn",
            ["im gonna rattle off a bunch of stuff and you guys sort it out after", "first whats our burn"],
        ),
        ("Oh, one more thing, can you check the cert?", ["one more thing", "can you check the cert"]),
        # speech-to-text with no punctuation: a filler, "but", "anyway" or "can you" opens the next part
        (
            "good morning everyone um can you check if the nightly backup ran",
            ["good morning everyone", "can you check if the nightly backup ran"],
        ),
        (
            "the tweet's good but can you make a LinkedIn version too",
            ["the tweet's good", "can you make a LinkedIn version too"],
        ),
        (
            "sorry my dog is barking lol anyway how many tickets came in overnight",
            ["sorry my dog is barking lol", "how many tickets came in overnight"],
        ),
        (
            "whats our burn uh how many active users do we have and is the api down",
            ["whats our burn", "how many active users do we have", "is the api down"],
        ),
        ("nice and what about week over week retention", ["nice", "what about week over week retention"]),
        (
            "I'll just rattle them off first one is can you find the Acme NDA",
            ["I'll just rattle them off", "first one is", "can you find the Acme NDA"],
        ),
        ("Anyway, how many trials started yesterday?", ["how many trials started yesterday"]),
        # any word with its object after a joiner is an instruction: no verb list covers what people say
        (
            "how many active users did we have yesterday oh and um like rotate the stripe api key on staging",
            ["how many active users did we have yesterday", "rotate the stripe api key on staging"],
        ),
        ("ugh mondays okay pull the weekly revenue report", ["ugh mondays", "pull the weekly revenue report"]),
        (
            "merge the dark mode pr if checks are green you know and pull signups by country",
            ["merge the dark mode pr if checks are green you know", "pull signups by country"],
        ),
    ],
)
def test_a_sentence_is_cut_where_a_new_request_begins(line, want):
    assert parts(line) == want


@pytest.mark.parametrize(
    "line",
    [
        "Compare signups, activation and paid conversion for August and September.",
        "check the Stripe dashboard and the Mercury balance",
        "Can you find the terms and conditions page that legal sent over?",
        "What did we pay for Figma, Notion, Linear and Slack last year?",
        "Pull the signups for, uh, July, sorry, I mean August.",
        "I'm just gonna say them all, and then we'll figure out kind of how to answer them.",
        "okay can you also rerun the flaky e2e job on the mobile branch",  # "can you" alone is no part
        "export the list of trial users and then send it to Dana",  # "send it" leans on the export: one task
        "check the build and test results",
        "Can you tell me... What the workspace plan costs per month?",  # an ellipsis is no sentence end
        "Can you tell me… um what the workspace plan costs",
        "find the customer who emailed us and asked for a refund",
        "what's the open rate on the um the last two newsletters actually just the last one",
        "tell Priya I need the board deck by Thursday",  # the words to pass on are the same request
        "if the nightly build failed rerun it and let me know",  # "let me know" is a tail
        "Draft the one-pager for Corvid and let me know when it's ready",
        "look up um the invoices and receipts from Figma and Notion",
    ],
)
def test_one_ask_with_several_objects_is_not_cut(line):
    assert len(parts(line)) == 1


# ---- requests and owners (§4.3) ----


# ---- earlier requests (§4.4: steer, follow-up, cancel) ----


# ---- who is implied (§4.4) ----


# ---- failure and limits ----


# ---- a line that only calls on an agent (driven voice: ops.say) ----


# ---- a meeting: a line with no name on it (architecture review, point 4) ----


# ---- a stop is one of two things: a request withdrawn (cancel), or an agent told to stop talking (hush). ----
# ---- Jev says which (the part's kind). Which request it is about (``drops``) is a separate answer.       ----


# ---- a named request never comes back as nothing (10-08 Meet: "Friday, what does the Workspace plan cost
# ---- per month?" got no agent and nothing to do) ----
