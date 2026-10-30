"""agent_evals judge: reads good/bad against the reply; anything it cannot parse is an error, never a pass."""

import json

import pytest

from scripts.agent_evals.cases import Case
from scripts.agent_evals.judge import judge

CASE = Case(id="c", source="G1#24", say="How many open tasks?", good="States 12.", bad="Any other number.")


def _answer(content):
    async def post(body):
        post.body = body
        return {"choices": [{"message": {"content": content}}]}

    return post


@pytest.mark.asyncio
async def test_pass_json_is_a_pass():
    verdict = await judge(CASE, "There are 12.", post=_answer('{"pass": true, "reason": "says 12"}'))
    assert verdict == {"outcome": "pass", "reason": "says 12"}


@pytest.mark.asyncio
async def test_fail_json_is_a_fail_with_reason():
    verdict = await judge(CASE, "There are 3.", post=_answer('```json\n{"pass": false, "reason": "says 3"}\n```'))
    assert verdict == {"outcome": "fail", "reason": "says 3"}


@pytest.mark.asyncio
async def test_unparseable_output_is_an_error_not_a_pass():
    verdict = await judge(CASE, "There are 12.", post=_answer("Looks good to me!"))
    assert verdict["outcome"] == "error"


@pytest.mark.asyncio
async def test_post_exception_is_an_error():
    async def post(body):
        raise RuntimeError("gateway down")

    verdict = await judge(CASE, "There are 12.", post=post)
    assert verdict["outcome"] == "error"


@pytest.mark.asyncio
async def test_prompt_carries_good_and_bad_and_the_reply():
    post = _answer('{"pass": true, "reason": "ok"}')
    await judge(CASE, "There are 12.", post=post)
    text = json.dumps(post.body["messages"])
    assert "States 12." in text and "Any other number." in text and "There are 12." in text
    assert post.body["temperature"] == 0
