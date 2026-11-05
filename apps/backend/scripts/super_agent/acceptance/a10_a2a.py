"""A10 — A2A (spec 09 §8.3; Q Task 11 step 4).

The cross-org path needs a second org (the old ``sa-dev-acme``), which the single dev workspace no longer has, and
the governance hold path (05 T9/T10, review G11) is not shipped. The CLI-node variant needs an OrbStack node
(spec 09 §8.5), which this harness cannot start. So the body reports why it did not run instead of passing.
"""

from __future__ import annotations

from scripts.super_agent.acceptance.ctx import Ctx, Result


async def a10(ctx: Ctx) -> Result:
    return Result(
        False,
        {
            "reason": "blocked: cross-org needs a second org + the A2A session's hold path (05 T9/T10, G11); "
            "the CLI-node variant is run by hand on OrbStack (spec 09 §8.5)",
        },
    )
