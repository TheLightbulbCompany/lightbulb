"""Print one owner id per workspace whose container row opted out of the super agent (`super_agent: false`).

Phase-1 task 3: run before the switch retires; each listed owner is set back by hand with the founder's word.
Read-only. Runs inside the backend task:

    uv run python -m scripts.list_super_agent_optouts
"""

from __future__ import annotations

import asyncio

from core.services import super_agent_flags


async def main(argv: list[str] | None = None) -> int:
    for owner_id in sorted(await super_agent_flags.list_off()):
        print(owner_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
