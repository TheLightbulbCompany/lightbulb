"""Spec 05 P1 (dev, behind the super-agent switch): a provisioning consent asked from a task run is a Question on
that task, not a Paperclip board card. The Worker's turn ends on it (the task waits; its next attempt is held until
someone answers), and the answer is the approval: `provisioning_workflow._reconcile_approval` reads the Question row
instead of the card. Switch off, or a call from no live task run: the board card, exactly as before.

Only the consent is a Question. Connect, information and payment setup need the person to do something on a form,
so they stay cards; the company-Stripe card is onboarding, also a person's form.
"""

from __future__ import annotations

from core.repositories import questions_repo, tasks_repo
from core.services import provisioning_approvals, questions, super_agent_flags, task_asks


async def task_for_run(owner_id: str, run_id: str | None, run_secret: str | None) -> dict | None:
    """The working task whose live attempt is `run_id` (`X-Lightbulb-Run`) and whose run secret the caller presents
    (`X-Lightbulb-Run-Secret`, checked as the runtime routes check it), or None: switch off, no or wrong secret, not a
    task run. None files the board card, as before."""
    if not run_id or not run_secret or not await super_agent_flags.flags_for(owner_id):
        return None
    from core.services import effect_doors
    from routers.runtime_internal import _secret_ok

    for att in await effect_doors.live_attempts(owner_id):
        if att.get("runId") == run_id:
            return await tasks_repo.get_task(owner_id, att["taskId"]) if _secret_ok(att, run_secret) else None
    return None


def _title(snapshot: dict) -> str:
    """Read after "{Lightbulb · specialist} needs your OK to …" (questions.card_copy)."""
    what = f"set up {snapshot.get('provider_name')} {snapshot.get('service_ref')}"
    price = provisioning_approvals.price_display(snapshot)
    where = snapshot.get("environment")
    return (what + (f" ({price})" if price else "") + (f" for {where}" if where else ""))[:200]


def _money(snapshot: dict) -> str:
    """Plainly, for a paid service: the price as the provider states it (with its recurrence, e.g. "$20/mo"; never
    re-derived from that text) and the spending limit the approval applies."""
    if snapshot.get("pricing_type") != "paid":
        return ""
    limit = provisioning_approvals.DEFAULT_USAGE_LIMIT
    price = provisioning_approvals.price_display(snapshot) or "see the provider's pricing"
    return (
        f"Price: {price}, billed to your card on your Stripe account. "
        f"Spending limit: ${limit['max_amount'] / 100:,.0f} a {limit['recurring_interval']}."
    )


def _terms(snapshot: dict) -> str:
    return f"Terms: {snapshot['terms_url']}" if snapshot.get("terms_url") else ""


async def ask_consent(owner_id: str, task_id: str, workflow_id: str, snapshot: dict, reason: str, key: str) -> dict:
    """The consent Question on `task_id` (idempotent on `key`); `{taskId, qid}` for the workflow row. Raises
    `task_asks.AskRefused` when it cannot be opened."""
    task = await tasks_repo.get_task(owner_id, task_id)
    if task is None:
        raise task_asks.AskRefused("task_closed")
    n = task.get("attemptCount") or 0
    att = await tasks_repo.get_attempt(owner_id, task_id, n) if n else None
    text = " ".join(x for x in (reason, _money(snapshot), _terms(snapshot)) if x)
    q = await task_asks.open_ask(
        owner_id,
        task,
        qid=questions.qid_for(f"provision:{key}"),
        kind="approval",
        subkind="provider_consent",
        title=_title(snapshot),
        text=text[:500],
        # A resource keeps running (and a paid one keeps billing) after the answer, so never "Allow once". The id
        # stays `allow_once`: the answer route and `answer_of` read it.
        options=[{"id": "allow_once", "label": "Allow"}, {"id": "deny", "label": questions.OPTION_LABELS["deny"]}],
        blocking=True,
        attempt=n,
        run_id=(att or {}).get("runId"),
    )
    return {"taskId": task_id, "qid": q["qid"], "workflowId": workflow_id}


async def answer_of(owner_id: str, ref: dict) -> tuple[str, str | None]:
    """("open" | "approved" | "rejected", who answered) for a workflow's consent Question. A missing or cancelled
    Question (the task was stopped or resolved) is a rejection: nobody is left to approve it."""
    q = await questions_repo.get_question(owner_id, ref["taskId"], ref["qid"])
    if q is None or q.get("state") == "cancelled":
        return "rejected", None
    if q.get("state") != "answered":
        return "open", None
    answer = q.get("answer") or {}
    return ("rejected" if answer.get("optionId") == "deny" else "approved"), q.get("answeredBy")
