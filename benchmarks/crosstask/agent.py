"""The hop loop (spec 07 §8.6): one model call per step decides answer | follow | recall.

Hop 1 is the first recall; at most ``MAX_HOPS`` backend reads per question, so at most
``MAX_HOPS`` model calls. The loop measures memory + links, not the Router's production model.
"""

import json
import time

MAX_HOPS = 3
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["answer", "follow", "recall"]},
        "link_id": {"type": "string"},
        "query": {"type": "string"},
        "answer": {"type": "string"},
        "abstain": {"type": "boolean"},
        "cited_task_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action", "link_id", "query", "answer", "abstain", "cited_task_ids"],
}
PROMPT = """You answer a teammate's question from the workspace's memory.
Question (asked {asked_at}): {question}

What you have read so far:
{read}

Choose one action:
- "follow": read one linked task in full (set link_id to the id after "task:").
- "recall": search memory again with a different query (set query).
- "answer": give the answer (set answer and cited_task_ids). If memory holds nothing on it, set abstain to true.
{final}Reply as JSON."""
FINAL = 'This is your last step: the action must be "answer".\n'


def _render_read(read: list[str]) -> str:
    return "\n\n".join(read) or "(nothing yet)"


async def run_question(question: dict, backend, llm, *, links: bool = True) -> dict:
    """Returns what was read, reached and answered, for score.py."""
    audience = question["audience"]
    read: list[str] = []
    reached: list[list[str]] = []  # task ids reached at each hop
    latencies: list[float] = []

    async def hop(kind: str, arg: str) -> None:
        t0 = time.perf_counter()
        if kind == "recall":
            page = await backend.recall(arg, audience, links=links)
            got = [lk["id"] for lk in page.get("links", [])]
            read.append(f"[recall: {arg}]\n{page['text']}")
        else:
            res = await backend.read_link(arg, audience)
            got = [arg] if res else []
            read.append(f"[read task:{arg}]\n{json.dumps(res) if res else 'not found'}")
        latencies.append(time.perf_counter() - t0)
        reached.append(got)

    await hop("recall", question["text"])
    decision: dict = {}
    for step in range(1, MAX_HOPS + 1):
        last = step == MAX_HOPS
        prompt = PROMPT.format(
            asked_at=question["askedAt"],
            question=question["text"],
            read=_render_read(read),
            final=FINAL if last else "",
        )
        decision = await llm.complete_json(prompt, SCHEMA)
        action = decision.get("action")
        if last or action == "answer":
            break
        if action == "follow" and decision.get("link_id") and links:
            await hop("follow", decision["link_id"].removeprefix("task:"))
        else:
            await hop("recall", decision.get("query") or question["text"])
    return {
        "id": question["id"],
        "answer": decision.get("answer", ""),
        "abstain": bool(decision.get("abstain")),
        "cited": decision.get("cited_task_ids") or [],
        "read": read,
        "reached": reached,
        "latencies": latencies,
    }


class HeuristicLLM:
    """A deterministic stand-in for Codex: follow the first unread link, then answer from what was read.
    Lets the harness run end to end with no model calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete_json(self, prompt: str, schema: dict) -> dict:
        import re

        self.calls += 1
        read_part = prompt.split("What you have read so far:", 1)[1].split("Choose one action:", 1)[0]
        followed = set(re.findall(r"\[read task:([0-9a-f-]{36})\]", read_part))
        linked = [i for i in re.findall(r"task:([0-9a-f-]{36})", read_part) if i not in followed]
        if linked and FINAL not in prompt:
            return {
                "action": "follow",
                "link_id": linked[0],
                "query": "",
                "answer": "",
                "abstain": False,
                "cited_task_ids": [],
            }
        found = list(dict.fromkeys(re.findall(r"task:([0-9a-f-]{36})", read_part)))
        return {
            "action": "answer",
            "link_id": "",
            "query": "",
            "answer": read_part.strip()[-600:] if found else "I found nothing on that.",
            "abstain": not found,
            "cited_task_ids": found[:1],
        }
