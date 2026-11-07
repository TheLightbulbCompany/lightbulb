"""Landing executors: the backend runs a reviewed draft once a person pressed Send / Publish / Merge (wave2-Q Task 6;
contracts-v2 §8). Each is `(owner_id, operands, answered_by) -> result_ref`.

Three outcomes, so `apply_effect` never sends twice:
- a result ref: it happened;
- `ExecutorRefused`: it definitely did not happen (policy, not ready, not connected) — safe to tell the Worker;
- `ExecutorUnknown`: it may have happened (an error after the request left) — the Effect becomes `unknown`, shown
  plainly, never retried.
The executors reuse the existing doors and their own gates: `agent_mail_send.send` (caps, recipient rules),
GitHub's merge API with the owner's stored token.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

from core.services import agent_mail_send
from core.services.credentials import integration_secret_store

GITHUB_API = "https://api.github.com"
_TIMEOUT_S = 20.0


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT_S)


class ExecutorRefused(Exception):
    """Definitely not done; `code` is the door's own reason, `next` what a person can do."""

    def __init__(self, code: str, next_step: str | None = None):
        super().__init__(code)
        self.code, self.next_step = code, next_step


class ExecutorUnknown(Exception):
    """The request may have taken effect; never retried."""


async def mail_send(owner_id: str, operands: dict, answered_by: str) -> str:
    """`agent_mail_send.send` never raises: "sent", "error" (may be after SES accepted), or a refusal code."""
    result = await agent_mail_send.send(
        owner_id=owner_id,
        agent_id=operands["agentId"],
        subject=operands.get("subject") or "",
        body_text=operands.get("body") or "",
        to=operands.get("to"),
        reply_key=operands.get("replyKey"),
        purpose="task",
    )
    if result == "sent":
        return "mail:sent"
    if result == "error":
        raise ExecutorUnknown("mail send error")
    raise ExecutorRefused(result)


async def pr_merge(owner_id: str, operands: dict, answered_by: str) -> str:
    """Merge with the owner's GitHub token. An already merged PR is a success (a retry after a lost response)."""
    token = await integration_secret_store.github_token(owner_id)
    if not token:
        raise ExecutorRefused("github_not_connected", "connect GitHub in Settings")
    repo, number = operands["repo"], int(operands["number"])
    url = f"{GITHUB_API}/repos/{repo}/pulls/{number}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with _http() as http:
        try:
            pr = await http.get(url, headers=headers)
        except httpx.HTTPError as err:
            raise ExecutorRefused("github_unreachable") from err  # nothing was sent yet
        if pr.status_code != 200:
            raise ExecutorRefused(f"github_{pr.status_code}")
        if pr.json().get("merged"):
            return f"pr:{repo}#{number}:{pr.json().get('merge_commit_sha') or ''}"
        body = {"merge_method": operands.get("method") or "merge"}
        if operands.get("sha"):
            body["sha"] = operands["sha"]  # the reviewed head: a newer push refuses (409), never merges unseen code
        try:
            resp = await http.put(f"{url}/merge", headers=headers, json=body)
        except httpx.HTTPError as err:
            raise ExecutorUnknown(type(err).__name__) from err
    if resp.status_code == 200 and resp.json().get("merged"):
        return f"pr:{repo}#{number}:{resp.json().get('sha') or ''}"
    if resp.status_code >= 500:
        raise ExecutorUnknown(f"github_{resp.status_code}")
    raise ExecutorRefused(f"github_{resp.status_code}")


EXECUTORS: dict[str, Callable[[str, dict, str], Awaitable[str]]] = {
    "mail.send": mail_send,
    "pr.merge": pr_merge,
}
