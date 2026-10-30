"""The judge: one model call reads a case's good and bad lines against the agent's reply.

Over the Vercel AI Gateway with the DEV key (lightbulb/dev/vercel-ai-gateway), like the speak-gate
bench. Anything the judge says that is not the JSON verdict is an ``error``, never a pass.
"""

from __future__ import annotations

import json
import os
import re

import httpx

from scripts.agent_evals.cases import Case

#: The newest Claude Sonnet on the gateway's /v1/models list (30 Oct 2026).
JUDGE_MODEL = "anthropic/claude-sonnet-5.5"
URL = "https://ai-gateway.vercel.sh/v1/chat/completions"
SYSTEM = (
    "You grade one reply from a workplace AI agent. A person sent it a message; you get what a good "
    "reply does and what a bad reply does (the bad line describes a real failure). Judge only "
    "against those two lines. Answer with JSON only: "
    '{"pass": true or false, "reason": "<one sentence>"}'
)
_JSON = re.compile(r"\{.*\}", re.S)


async def gateway_post(body: dict) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(URL, json=body, headers={"Authorization": f"Bearer {os.environ['AI_GATEWAY_API_KEY']}"})
        res.raise_for_status()
        return res.json()


async def judge(case: Case, reply: str, *, post=gateway_post) -> dict:
    user = (
        f"The person said:\n{case.say}\n\nA good reply: {case.good}\nA bad reply: {case.bad}\n\n"
        f"The agent replied:\n{reply}"
    )
    body = {
        "model": JUDGE_MODEL,
        "temperature": 0,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
    }
    try:
        content = (await post(body))["choices"][0]["message"]["content"]
        verdict = json.loads(_JSON.search(content).group(0))
        outcome = "pass" if verdict["pass"] is True else "fail"
        return {"outcome": outcome, "reason": str(verdict.get("reason") or "")}
    except Exception as exc:  # noqa: BLE001 -- an unreadable verdict is an error, never a pass
        return {"outcome": "error", "reason": f"judge failed: {type(exc).__name__}"}
