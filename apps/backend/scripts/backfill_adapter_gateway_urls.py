"""ONE-SHOT: point every openclaw_gateway adapter at the gateway relay URL.

Run once per environment after that environment's relay deploy (see
docs/superpowers/sdd/2026-09-12-machine-relay/task-6-brief.md), then DELETE
this file (CLAUDE.md: one-shot backfills do not accumulate; git history is
the record). Dry run by default; ``--apply`` writes.

    cd apps/backend && AWS_PROFILE=isol8-admin uv run python scripts/backfill_adapter_gateway_urls.py
    cd apps/backend && AWS_PROFILE=isol8-admin uv run python scripts/backfill_adapter_gateway_urls.py --apply
"""

import argparse
import asyncio
import sys
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, ".")

from core.config import settings  # noqa: E402
from core.dynamodb import run_in_thread  # noqa: E402
from core.repositories.paperclip_repo import PaperclipRepo  # noqa: E402
from core.services.company_deploy_service import _open_admin_session  # noqa: E402
from core.services.inbound_triggers import _agent_rows  # noqa: E402


async def _companies() -> list[str]:
    table = PaperclipRepo(table_name="paperclip-companies")._table()
    ids, kwargs = [], {"ProjectionExpression": "company_id"}
    while True:
        page = await run_in_thread(table.scan, **kwargs)
        ids.extend(row["company_id"] for row in page.get("Items", []) if row.get("company_id"))
        if "LastEvaluatedKey" not in page:
            return sorted(set(ids))
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _relay_url(old: str, base: str) -> str:
    """Keep the query (token + clientId); replace scheme/host/path with the relay's."""
    query = urlsplit(old).query
    b = urlsplit(base)
    return urlunsplit((b.scheme, b.netloc, b.path, query, ""))


async def main(apply: bool) -> int:
    base = settings.GATEWAY_RELAY_URL
    http, admin, cookie = await _open_admin_session()
    changed = 0
    try:
        for company_id in await _companies():
            agents = _agent_rows(await admin.list_agents(session_cookie=cookie, company_id=company_id))
            for agent in agents:
                if agent.get("adapterType") != "openclaw_gateway":
                    continue
                cfg = agent.get("adapterConfig") or {}
                url = cfg.get("url") or ""
                if url.startswith(base):
                    continue
                changed += 1
                print(f"{'PATCH' if apply else 'would patch'} company={company_id} agent={agent['id']}")
                if apply:
                    await admin.patch_agent(
                        session_cookie=cookie,
                        agent_id=agent["id"],
                        body={
                            "adapterConfig": {**cfg, "url": _relay_url(url, base)},
                            "replaceAdapterConfig": True,
                        },
                    )
    finally:
        await http.aclose()
    print(f"{'patched' if apply else 'stale'}: {changed}")
    return changed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write (default: dry run)")
    raise SystemExit(0 if asyncio.run(main(parser.parse_args().apply)) >= 0 else 1)
