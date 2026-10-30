"""Deterministic checks: a hard floor under the judge. Any failure fails the case."""

from __future__ import annotations


def _statuses(tasks: list[dict]) -> dict[str, str]:
    return {t["title"]: t["status"] for t in tasks}


def run_checks(checks: list[dict], reply: str, before: list[dict], after: list[dict]) -> list[str]:
    """The reasons the reply or the board failed ``checks``; empty when every check passed."""
    text = reply.lower()
    then, now = _statuses(before), _statuses(after)
    failed: list[str] = []
    for check in checks:
        kind = check["kind"]
        if kind == "contains_any" and not any(v.lower() in text for v in check["values"]):
            failed.append(f"reply has none of {check['values']}")
        elif kind == "contains_none" and (hit := [v for v in check["values"] if v.lower() in text]):
            failed.append(f"reply contains {hit}")
        elif kind == "max_chars" and len(reply) > check["n"]:
            failed.append(f"reply is {len(reply)} chars, over {check['n']}")
        elif kind == "tasks_unchanged" and then != now:
            changed = sorted({title for title, _ in set(then.items()) ^ set(now.items())})
            failed.append(f"the board changed: {changed}")
        elif kind == "task_status" and now.get(check["title"]) != check["status"]:
            failed.append(f"'{check['title']}' is {now.get(check['title'], 'missing')}, expected {check['status']}")
    return failed
