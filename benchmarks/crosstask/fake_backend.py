"""An in-memory stand-in for the backend's recall and link routes, built from the fixture.

It follows the design's rules, not its engines: room text is stored only for channels and a
person's DM with Lightbulb (people-only DMs and group DMs never), private lanes are read only for a
person audience, task cards feed links (never results), and every link is checked on the live row.
Retrieval is word overlap, so the numbers it gives measure the harness, not memory quality.
"""

import re

_WORD = re.compile(r"[a-z0-9]{3,}")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def link_visible(task: dict, audience: dict, rooms: dict[str, dict]) -> bool:
    """wave2-M Task 6 ``link_visible`` over ``can_view`` for the fixture's rooms."""
    if task.get("hidden"):
        return False
    aud = task["audience"]
    if audience["kind"] == "person":
        user = audience["userId"]
        if aud["kind"] == "person":
            return user in aud["userIds"]
        if aud["kind"] == "room":
            return user in rooms[aud["roomId"]]["members"]
        return True
    if audience["kind"] == "room":
        return aud["kind"] == "workspace" or (aud["kind"] == "room" and aud["roomId"] == audience["roomId"])
    return aud["kind"] == "workspace"


class FakeBackend:
    def __init__(self, fixture: dict) -> None:
        self.rooms = {r["id"]: r for r in fixture["rooms"]}
        self.tasks = {t["taskId"]: t for t in fixture["tasks"]}
        names = {p["id"]: p["name"] for p in fixture["people"]}
        self.turns, self.cards = [], []
        for m in fixture["messages"]:
            room = self.rooms[m["roomId"]]
            if room["kind"] == "channel":
                lane = None
            elif room["kind"] == "dm" and "lightbulb" in room["members"]:
                lane = next(u for u in room["members"] if u != "lightbulb")
            else:
                continue  # people-only DMs and group DMs are never written
            text = f"{names.get(m['author'], 'Lightbulb')}: {m['text']}"
            self.turns.append({"lane": lane, "text": text, "taskId": m["taskId"], "words": _words(text)})
        for t in fixture["tasks"]:
            card = f"{t['identifier']} {t['title']} — {t['statusAtIndex']} · {t['specialist']}\n{t['description']}"
            lane = t["ownerUserId"] if t["lane"] == "private" else None
            self.cards.append({"lane": lane, "text": card, "taskId": t["taskId"], "words": _words(card)})

    @staticmethod
    def _top(rows: list[dict], audience: dict, query: set[str], k: int) -> list[dict]:
        member = audience.get("userId") if audience["kind"] == "person" else None
        readable = [r for r in rows if r["lane"] is None or r["lane"] == member]
        scored = sorted(((len(query & r["words"]), i) for i, r in enumerate(readable)), reverse=True)
        return [readable[i] for s, i in scored[:k] if s]

    def _link(self, task: dict) -> dict:
        return {
            "kind": "task",
            "id": task["taskId"],
            "identifier": task["identifier"],
            "title": task["title"],
            "status": task["status"],  # the live row, never the card
            "specialist": task["specialist"],
        }

    async def recall(self, query: str, audience: dict, *, links: bool = True) -> dict:
        words = _words(query)
        turns = self._top(self.turns, audience, words, 8)
        lines = ["Turns:", *(f"- {t['text']}" for t in turns)]
        page: dict = {"results": [{"kind": "turn", "text": t["text"], "taskId": t["taskId"]} for t in turns]}
        if links:
            ids = [c["taskId"] for c in self._top(self.cards, audience, words, 10)]
            ids += [t["taskId"] for t in turns if t["taskId"]]
            seen = list(dict.fromkeys(ids))[:10]
            live = [self.tasks[i] for i in seen if link_visible(self.tasks[i], audience, self.rooms)]
            page["links"] = [self._link(t) for t in live]
            lines += ["Linked:"] + [
                f"- {lk['identifier']} {lk['title']} — {lk['status']} · {lk['specialist']} · task:{lk['id']}"
                for lk in page["links"]
            ]
        page["text"] = "\n".join(lines)
        return page

    async def read_link(self, task_id: str, audience: dict) -> dict | None:
        task = self.tasks.get(task_id)
        if not task or not link_visible(task, audience, self.rooms):
            return None
        return {
            "task": self._link(task),
            "outputs": [task["output"]],
            "lastResult": f"Update on {task['title']}: {task['fact']}.",
        }
