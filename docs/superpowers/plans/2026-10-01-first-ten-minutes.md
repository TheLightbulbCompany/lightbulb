# The First Ten Minutes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A fresh trial workspace shows the chat immediately, with the CEO's one-question first message streaming in while the runtime sets up, three pre-typed replies that prefill the composer, and a first message that carries a URL steering the brief onto the "grow" path.

**Architecture:** Backend: one message text change in the #general seed (workspace name interpolated via Clerk) and a path/you-pick detection in the first-message intake. Frontend: a chips component above the composer, a focus token on the composer, a typing-reveal wrapper in RoomPanel, and one branch in ProvisioningStepper that renders the existing "starting" banner plus the chat for a trial workspace in the container phase. Nothing is held; a reply during setup rides the normal send path and the work consumer's existing retry.

**Tech Stack:** FastAPI + DynamoDB (moto in tests), pytest; Next.js 16 / React 19, vitest + Testing Library; `@lightbulb/shared` for types.

**Spec:** `docs/superpowers/specs/2026-10-01-first-ten-minutes-design.md`

## Global Constraints

- Owner rulings: one static first message; zero tokens until a human acts; no hold; no returner acknowledgement; chips prefill, never send.
- Copy is exact (spec): the message text and the three chips below. Only `{Workspace}` is interpolated.
- CI is the test gate (owner rule). Run only the test files a task touches locally; never whole suites.
- Codex is the only reviewer; merge on green CI with its findings handled. Delete the spec and this plan in the shipping PR (CLAUDE.md: planning docs are disposable).
- Frontend colours/classes: reuse existing classes (`room-composer`, `text-ui`, `text-muted-foreground`); no colour literals.
- Backend: no new tables, metrics or alarms. Server events already live (`room_message_sent{first_in_room}`, `onboarding_ready`, `room_opened`).

## Review Focus

- A workspace name containing `@` or `#` at the start could render as a mention or channel in the welcome; the seed strips leading `@`/`#` and collapses whitespace (Task 2 test).
- A first message that is only a bare domain (`acme.com`) must take the grow path with a clean domain as the goal input (Task 1 test).
- The "you pick" reply with extra words after it must still read as no-idea, not as the idea (Task 1 tests startswith, case-insensitive).
- `prefers-reduced-motion` must show the full message at once; the reveal must play once per room, not on every visit (Task 4 tests).
- Paid flows (phase `payment`/`provider`) must still get the full stepper; only `trial_local` + `container` gets the banner-and-chat (Task 5 tests both).

---

### Task 1: First message intake — URL means "grow", "Not sure yet" means no idea

**Files:**
- Modify: `apps/backend/core/services/onboarding_brief.py` (constants near line 42; `capture_first_workspace_message`, the `put_pending_brief(...)` call at ~line 330)
- Test: `apps/backend/tests/unit/services/test_onboarding_brief.py`

**Interfaces:**
- Consumes: `PaperclipRepo.put_pending_brief(owner_id, *, path, user_input, detail, brief_text, submitted_by, source_message_id, target_agent_id)` (existing).
- Produces: `first_message_intake(text: str) -> tuple[str, str | None]` returning `(path, user_input)`: `("grow", "<url token>")` when the text holds a URL or bare domain; `("new", None)` when it starts with "not sure yet"; else `("new", text)`.

- [ ] **Step 1: Write the failing tests** (append to `test_onboarding_brief.py`; the file already has `OWNER`, `AsyncMock`, `patch`, and a `message` fixture pattern in `test_first_canonical_human_message_captures_eligible_specialist`, lines 28-82 — copy its `repo`/`patch` setup for the capture test)

```python
import pytest

from core.services.onboarding_brief import first_message_intake


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Here's our website: https://acme.com/pricing", ("grow", "https://acme.com/pricing")),
        ("Here's our website: acme.com", ("grow", "acme.com")),
        ("acme.co", ("grow", "acme.co")),
        ("I'm starting a dog-walking service in Austin", ("new", "I'm starting a dog-walking service in Austin")),
        ("Not sure yet. Pick something useful and show me how you work.", ("new", None)),
        ("not sure yet, you choose", ("new", None)),
        ("We ship 3.5 tonnes a week", ("new", "We ship 3.5 tonnes a week")),
    ],
)
def test_first_message_intake_picks_the_path_and_the_goal_input(text, expected):
    assert first_message_intake(text) == expected


@pytest.mark.asyncio
async def test_a_url_in_the_first_message_lands_as_grow_with_the_url_as_input():
    # Same harness as test_first_canonical_human_message_captures_eligible_specialist,
    # with the message text carrying a link.
    repo = AsyncMock()
    repo.resolve_owner_row = AsyncMock(return_value=SimpleNamespace(status="active", funnel_state="not_started"))
    repo.put_pending_brief = AsyncMock(return_value=True)
    message = {
        "roomId": default_room_id(OWNER), "messageSeq": 1, "messageId": "m1",
        "author": {"kind": "human", "id": "user_1"},
        "text": "Here's our website: https://acme.com",
        "dispatchTargets": [{"agentId": "agent-ceo"}],
    }
    with (
        patch("core.repositories.paperclip_repo.PaperclipRepo", return_value=repo),
        patch("core.services.onboarding_brief.reconcile_pending_brief", new=AsyncMock()),
        patch("core.repositories.messaging_repo.list_messages_between", new=AsyncMock(return_value=[])),
    ):
        assert await capture_first_workspace_message(OWNER, message) is True
    kwargs = repo.put_pending_brief.await_args.kwargs
    assert kwargs["path"] == "grow"
    assert kwargs["user_input"] == "https://acme.com"
    assert kwargs["brief_text"] == "Here's our website: https://acme.com"
```

Adjust the imports at the top of the test file to include `SimpleNamespace` from `types` and `default_room_id` from `core.services.messaging` if the existing test does not already (check lines 1-27 and mirror how the existing capture test builds `message`; if it uses helper names, use those instead of the literal dict).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/backend && .venv/bin/python -m pytest -q tests/unit/services/test_onboarding_brief.py -k "intake or lands_as_grow"`
Expected: FAIL with `ImportError: cannot import name 'first_message_intake'`.

- [ ] **Step 3: Implement**

In `onboarding_brief.py`, near the `_NO_IDEA_BLOCK` constant (line ~42), add:

```python
import re

# A link or a bare domain anywhere in the first message: the owner has an existing
# business, so the brief takes the "grow" path and the goal title derives from the domain.
_URL_RE = re.compile(
    r"(https?://\S+|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|co|io|ai|net|org|app|dev|shop|me|us|uk|ca)\b(?:/\S*)?)",
    re.IGNORECASE,
)
#: The third pre-typed reply (apps/frontend/src/components/messaging/FirstReplyChips.tsx)
#: starts with these words. Matching the prefix, not the whole sentence, survives copy tweaks.
_YOU_PICK_PREFIX = "not sure yet"


def first_message_intake(text: str) -> tuple[str, str | None]:
    """(path, user_input) for the brief from the owner's first message.

    A URL or domain -> ("grow", that token); "Not sure yet…" -> ("new", None), which the
    brief template renders as the owner asking the CEO to pick; anything else -> ("new", text).
    """
    stripped = text.strip()
    if stripped.lower().startswith(_YOU_PICK_PREFIX):
        return "new", None
    match = _URL_RE.search(stripped)
    if match:
        return "grow", match.group(0).rstrip(".,;:)")
    return "new", stripped
```

In `capture_first_workspace_message`, replace the `put_pending_brief` call's two lines:

```python
    path, user_input = first_message_intake(text)
    landed = await repo.put_pending_brief(
        owner_id,
        path=path,
        user_input=user_input,
        detail=None,
        brief_text=text,
        submitted_by=str(author["id"]),
        source_message_id=str(message["messageId"]),
        target_agent_id=target_agent_id,
    )
```

(Previously `path="new", user_input=text`.) Check `put_pending_brief`'s signature in `core/repositories/paperclip_repo.py` accepts `user_input=None`; if it is typed `str`, widen it to `str | None` there — the brief renderer already handles `None` via `_NO_IDEA_BLOCK`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/backend && .venv/bin/python -m pytest -q tests/unit/services/test_onboarding_brief.py tests/unit/services/test_onboarding_brief_reconcile.py`
Expected: all pass. Then `.venv/bin/ruff check core/services/onboarding_brief.py tests/unit/services/test_onboarding_brief.py && .venv/bin/ruff format --check` the same files.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/onboarding_brief.py apps/backend/core/repositories/paperclip_repo.py apps/backend/tests/unit/services/test_onboarding_brief.py
git commit -m "feat(onboarding): a link in the first message takes the grow path; 'Not sure yet' asks the CEO to pick"
```

---

### Task 2: The CEO's first message, with the workspace name

**Files:**
- Modify: `apps/backend/core/services/messaging.py` (`DEFAULT_ROOM_WELCOME` at line 112; `ensure_default_room` seed at lines ~1812-1830)
- Test: `apps/backend/tests/unit/services/test_listening_for_all.py` (`_welcome_text` helper, `test_a_trial_workspace_gets_the_plain_welcome` at line 194), `apps/backend/tests/unit/repositories/test_messaging_repo.py` (line 341 uses the constant)

**Interfaces:**
- Consumes: `clerk_admin.get_organization(org_id) -> dict | None` (existing; `name` key).
- Produces: `default_room_welcome(workspace_name: str | None) -> str` and `FIRST_REPLIES: tuple[str, str, str]` (the exact chip texts, mirrored by the frontend).

- [ ] **Step 1: Write the failing tests**

In `test_listening_for_all.py`, replace the body of `test_a_trial_workspace_gets_the_plain_welcome` and add two tests:

```python
@pytest.mark.asyncio
async def test_a_trial_workspace_gets_the_first_message_with_its_name(monkeypatch):
    _seed_fixture(monkeypatch)
    _billing(monkeypatch, TRIAL)
    monkeypatch.setattr(messaging.clerk_admin, "get_organization", AsyncMock(return_value={"name": "Anna's Frozen Treats"}))

    await messaging.ensure_default_room(OWNER, USER)

    text = _welcome_text(monkeypatch)
    assert text == messaging.default_room_welcome("Anna's Frozen Treats")
    assert text.startswith("Welcome to Anna's Frozen Treats. I'm your CEO.")
    for reply in messaging.FIRST_REPLIES:
        assert reply.rstrip(" ") in text


@pytest.mark.asyncio
async def test_the_first_message_falls_back_when_clerk_has_no_name(monkeypatch):
    _seed_fixture(monkeypatch)
    _billing(monkeypatch, TRIAL)
    monkeypatch.setattr(messaging.clerk_admin, "get_organization", AsyncMock(return_value=None))

    await messaging.ensure_default_room(OWNER, USER)

    assert _welcome_text(monkeypatch).startswith("Welcome to your workspace. I'm your CEO.")


def test_the_first_message_never_starts_a_mention_or_channel_with_the_name():
    assert messaging.default_room_welcome("@growth  team").startswith("Welcome to growth team.")
    assert messaging.default_room_welcome("#general").startswith("Welcome to general.")
```

Add `from unittest.mock import AsyncMock` to the test imports if missing. In `test_messaging_repo.py` line 341, replace `messaging.DEFAULT_ROOM_WELCOME` with `messaging.default_room_welcome("Acme")` (read the surrounding lines first; the assertion only needs the same text on both sides).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/backend && .venv/bin/python -m pytest -q tests/unit/services/test_listening_for_all.py -k "first_message" tests/unit/repositories/test_messaging_repo.py`
Expected: FAIL with `AttributeError: module ... has no attribute 'default_room_welcome'` (and `clerk_admin` missing on `messaging` if not imported).

- [ ] **Step 3: Implement**

In `messaging.py`, add `clerk_admin` to the existing `from core.services import hermes_profiles, run_analytics` import (line 35) and replace the `DEFAULT_ROOM_WELCOME` constant (line 112) with:

```python
#: The three pre-typed replies. The frontend's FirstReplyChips.tsx carries the same strings;
#: the third's prefix is what onboarding_brief.first_message_intake recognises as "you pick".
FIRST_REPLIES: tuple[str, str, str] = (
    "Here's our website: ",
    "I'm starting ",
    "Not sure yet. Pick something useful and show me how you work.",
)

_FIRST_MESSAGE = (
    "Welcome to {workspace}. I'm your CEO. Growth, Engineering, Finance, Analytics and "
    "Customer Ops are in this room with me.\n\n"
    "One thing gets us started: what's the business? A website or a sentence is plenty.\n\n"
    "→ Here's our website: …\n"
    "→ I'm starting …\n"
    "→ Not sure yet. Pick something useful and show me how you work.\n\n"
    "I'll turn your answer into a plan and hand the first pieces to the team. "
    "You'll watch them work here."
)


def default_room_welcome(workspace_name: str | None) -> str:
    """The CEO's first message. Only the workspace name is interpolated; a leading @ or #
    is stripped so the name cannot render as a mention or a channel link."""
    name = " ".join((workspace_name or "").lstrip("@#").split()) or "your workspace"
    return _FIRST_MESSAGE.format(workspace=name)


# Kept for the one caller that compares against the unnamed text.
DEFAULT_ROOM_WELCOME = default_room_welcome(None)
```

In `ensure_default_room`, before the `create_message` call for the welcome (line ~1812), resolve the name and use it:

```python
    org = await clerk_admin.get_organization(owner_id) or {}
    welcome = default_room_welcome(org.get("name"))
```

and in the call replace `req_hash=messaging_repo.request_hash(DEFAULT_ROOM_WELCOME, ceo_id)` with `request_hash(welcome, ceo_id)` and `text=DEFAULT_ROOM_WELCOME` with `text=welcome`. The idempotency key (`f"{client_room_id}:welcome"`) stays as it is.

Then grep for other readers of `DEFAULT_ROOM_WELCOME` (`git grep -n DEFAULT_ROOM_WELCOME apps/backend`) and point any remaining ones at `default_room_welcome(...)`; delete the constant if none remain.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/backend && .venv/bin/python -m pytest -q tests/unit/services/test_listening_for_all.py tests/unit/repositories/test_messaging_repo.py` then ruff check/format on the touched files.
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/messaging.py apps/backend/tests/unit/services/test_listening_for_all.py apps/backend/tests/unit/repositories/test_messaging_repo.py
git commit -m "feat(onboarding): the CEO's first message asks the one question and names the workspace"
```

---

### Task 3: Pre-typed replies that prefill and focus the composer

**Files:**
- Create: `apps/frontend/src/components/messaging/FirstReplyChips.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomComposer.tsx` (props at lines 14-40; the textarea element inside `ComposerAttempt`)
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx` (composer render at line ~747)
- Test: `apps/frontend/src/components/messaging/__tests__/FirstReplyChips.test.tsx` (new), `apps/frontend/src/components/messaging/__tests__/RoomComposer.focus.test.tsx`, `apps/frontend/src/components/messaging/__tests__/RoomPanel.test.tsx`

**Interfaces:**
- Produces: `FIRST_REPLIES: readonly [string, string, string]` (same strings as the backend's `FIRST_REPLIES`), `<FirstReplyChips onPick={(text: string) => void} />`, and a new optional `RoomComposer` prop `focusToken?: number` — each change focuses the textarea with the cursor at the end.
- RoomPanel shows the chips when `room?.isDefault && !room.archivedAt && !messages.some(m => m.author.kind === "human")`.

- [ ] **Step 1: Write the failing tests**

`FirstReplyChips.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { FIRST_REPLIES, FirstReplyChips } from "../FirstReplyChips";

describe("FirstReplyChips", () => {
  it("offers the three pre-typed replies and prefills, never sends", async () => {
    const onPick = vi.fn();
    render(<FirstReplyChips onPick={onPick} />);
    const chips = screen.getAllByRole("button");
    expect(chips.map((c) => c.textContent)).toEqual([...FIRST_REPLIES].map((t) => t.trim()));
    await userEvent.click(chips[0]);
    expect(onPick).toHaveBeenCalledWith("Here's our website: ");
    await userEvent.click(chips[2]);
    expect(onPick).toHaveBeenLastCalledWith("Not sure yet. Pick something useful and show me how you work.");
  });
});
```

Append to `RoomComposer.focus.test.tsx` (reuse its `baseProps`):

```tsx
it("focuses the field with the cursor at the end when focusToken changes", () => {
  const { rerender } = render(<RoomComposer {...baseProps} text="Here's our website: " focusToken={0} />);
  const field = screen.getByRole("textbox") as HTMLTextAreaElement;
  expect(document.activeElement).not.toBe(field);
  rerender(<RoomComposer {...baseProps} text="Here's our website: " focusToken={1} />);
  expect(document.activeElement).toBe(field);
  expect(field.selectionStart).toBe("Here's our website: ".length);
});
```

Append to `RoomPanel.test.tsx` (reuse `props` and `message`; `message.author.kind` is `"agent"`):

```tsx
it("shows the pre-typed replies in the starter channel until a human has written", async () => {
  const onTextChange = vi.fn();
  const panelProps = { ...props, room: { ...props.room!, isDefault: true }, composer: { ...props.composer, onTextChange } };
  const view = render(<RoomPanel {...panelProps} messages={[message]} />);
  await userEvent.click(screen.getByRole("button", { name: "I'm starting" }));
  expect(onTextChange).toHaveBeenCalledWith("I'm starting ");
  const human = { ...message, messageId: "h1", messageSeq: 2, author: { kind: "human" as const, id: "owner", displayName: "Owner" } };
  view.rerender(<RoomPanel {...panelProps} messages={[message, human]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});

it("never shows the pre-typed replies outside the starter channel", () => {
  render(<RoomPanel {...props} room={{ ...props.room!, isDefault: false }} messages={[message]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/frontend && pnpm exec vitest run src/components/messaging/__tests__/FirstReplyChips.test.tsx src/components/messaging/__tests__/RoomComposer.focus.test.tsx src/components/messaging/__tests__/RoomPanel.test.tsx`
Expected: FirstReplyChips fails to resolve the module; the focus test fails on `activeElement`; the RoomPanel tests fail to find the button.

- [ ] **Step 3: Implement**

`FirstReplyChips.tsx`:

```tsx
"use client";

import { Button } from "@/components/ui/button";

/** The three pre-typed replies. The backend's messaging.FIRST_REPLIES carries the same strings;
 *  onboarding_brief.first_message_intake recognises the third by its "Not sure yet" prefix. */
export const FIRST_REPLIES = [
  "Here's our website: ",
  "I'm starting ",
  "Not sure yet. Pick something useful and show me how you work.",
] as const;

/** Suggested replies above the composer in a fresh starter channel. A tap PREFILLS the
 *  composer; the person completes the sentence and sends. It never sends on its own. */
export function FirstReplyChips({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="room-composer flex flex-wrap gap-2 pb-0" role="group" aria-label="Suggested replies">
      {FIRST_REPLIES.map((text) => (
        <Button key={text} type="button" variant="outline" size="sm" onClick={() => onPick(text)}>
          {text.trim()}
        </Button>
      ))}
    </div>
  );
}
```

`RoomComposer.tsx`: add `focusToken?: number;` to the props type (next to `disabled?: boolean;`) and inside `ComposerAttempt`, where the textarea ref already exists, add:

```tsx
  useEffect(() => {
    if (focusToken === undefined) return;
    const field = textareaRef.current;
    if (!field) return;
    field.focus();
    const end = field.value.length;
    field.setSelectionRange(end, end);
  }, [focusToken]);
```

(Use the file's existing textarea ref name; if the textarea has no ref, add `const textareaRef = useRef<HTMLTextAreaElement>(null)` and attach it. Destructure `focusToken` with the other props at line ~111.) Skip the effect on first mount when the token is `0`: the test renders with `focusToken={0}` and expects no focus — so guard with a ref holding the last seen token and return when it equals the current one.

`RoomPanel.tsx`: near the other `useState` hooks in `RoomPanel`, add `const [focusToken, setFocusToken] = useState(0);` and `const showFirstReplies = Boolean(room?.isDefault) && !room?.archivedAt && !messages.some((m) => m.author.kind === "human");`. Directly above the `<RoomComposer {...composer}` at line ~747 render:

```tsx
      {showFirstReplies && <FirstReplyChips onPick={(text) => { composer.onTextChange(text); setFocusToken((t) => t + 1); }} />}
```

and pass `focusToken={focusToken}` to that `RoomComposer`. Import `FirstReplyChips` at the top.

- [ ] **Step 4: Run the tests to verify they pass**

Run the same three files. Expected: all pass. Then `pnpm exec eslint` on the four touched files and `pnpm exec tsc --noEmit -p tsconfig.json`.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/messaging/FirstReplyChips.tsx apps/frontend/src/components/messaging/RoomComposer.tsx apps/frontend/src/components/messaging/RoomPanel.tsx apps/frontend/src/components/messaging/__tests__/
git commit -m "feat(onboarding): three pre-typed replies prefill the composer in a fresh starter channel"
```

---

### Task 4: The first message streams in

**Files:**
- Create: `apps/frontend/src/components/messaging/TypingReveal.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx` (`renderMessage`, line ~558-565)
- Test: `apps/frontend/src/components/messaging/__tests__/TypingReveal.test.tsx` (new), `apps/frontend/src/components/messaging/__tests__/RoomPanel.test.tsx`

**Interfaces:**
- Produces: `<TypingReveal text={string} playKey={string}>{(shown: string) => ReactNode}</TypingReveal>`: reveals `text` at 40 characters per 100 ms tick, once per `playKey` (sessionStorage `lightbulb.reveal.<playKey>`), full text at once under `prefers-reduced-motion` or when already played.

- [ ] **Step 1: Write the failing tests**

`TypingReveal.test.tsx`:

```tsx
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TypingReveal } from "../TypingReveal";

const text = "Welcome to Acme. I'm your CEO. ".repeat(4).trim();

describe("TypingReveal", () => {
  beforeEach(() => { vi.useFakeTimers(); window.sessionStorage.clear(); });
  afterEach(() => { vi.useRealTimers(); });

  it("reveals the text over time, then remembers it played", () => {
    render(<TypingReveal text={text} playKey="room-1">{(shown) => <p>{shown}</p>}</TypingReveal>);
    expect(screen.getByText(/./).textContent!.length).toBeLessThan(text.length);
    act(() => { vi.advanceTimersByTime(5000); });
    expect(screen.getByText(text)).toBeInTheDocument();
    expect(window.sessionStorage.getItem("lightbulb.reveal.room-1")).toBe("1");
  });

  it("shows everything at once when it already played", () => {
    window.sessionStorage.setItem("lightbulb.reveal.room-1", "1");
    render(<TypingReveal text={text} playKey="room-1">{(shown) => <p>{shown}</p>}</TypingReveal>);
    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it("shows everything at once under reduced motion", () => {
    const original = window.matchMedia;
    window.matchMedia = ((q: string) => ({ matches: q.includes("reduce"), media: q, addEventListener() {}, removeEventListener() {} })) as never;
    render(<TypingReveal text={text} playKey="room-2">{(shown) => <p>{shown}</p>}</TypingReveal>);
    expect(screen.getByText(text)).toBeInTheDocument();
    window.matchMedia = original;
  });
});
```

Append to `RoomPanel.test.tsx`:

```tsx
it("streams the CEO's first message in the starter channel until a human has written", () => {
  window.sessionStorage.clear();
  vi.useFakeTimers();
  const long = { ...message, text: "Welcome to Acme. I'm your CEO. ".repeat(6).trim() };
  render(<RoomPanel {...props} room={{ ...props.room!, isDefault: true }} messages={[long]} />);
  expect(screen.queryByText(long.text)).toBeNull();
  act(() => { vi.advanceTimersByTime(8000); });
  expect(screen.getByText(long.text)).toBeInTheDocument();
  vi.useRealTimers();
});
```

(Import `act` from Testing Library in that file if missing.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/frontend && pnpm exec vitest run src/components/messaging/__tests__/TypingReveal.test.tsx src/components/messaging/__tests__/RoomPanel.test.tsx`
Expected: module not found; the RoomPanel test finds the full text immediately and fails on `toBeNull()`.

- [ ] **Step 3: Implement**

`TypingReveal.tsx`:

```tsx
"use client";

import { useEffect, useState, type ReactNode } from "react";

const CHARS_PER_TICK = 40;
const TICK_MS = 100;
const key = (playKey: string) => `lightbulb.reveal.${playKey}`;

function played(playKey: string): boolean {
  try { return window.sessionStorage.getItem(key(playKey)) === "1"; } catch { return true; }
}
function markPlayed(playKey: string): void {
  try { window.sessionStorage.setItem(key(playKey), "1"); } catch { /* storage blocked: play once per mount */ }
}
function reducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Reveals `text` once per `playKey`, 40 characters per 100 ms; whole text at once under
 *  reduced motion or after it has played. Render-prop so the caller keeps its own markup. */
export function TypingReveal({ text, playKey, children }: {
  text: string; playKey: string; children: (shown: string) => ReactNode;
}) {
  const [count, setCount] = useState(() => (played(playKey) || reducedMotion() ? text.length : 0));
  useEffect(() => {
    if (count >= text.length) { markPlayed(playKey); return; }
    const timer = setTimeout(() => setCount((c) => Math.min(text.length, c + CHARS_PER_TICK)), TICK_MS);
    return () => clearTimeout(timer);
  }, [count, text.length, playKey]);
  return <>{children(text.slice(0, count))}</>;
}
```

`RoomPanel.tsx`, in `renderMessage` (line ~558): the body is built as `<RoomMessageContent message={message} text={linkify…(message.text, …)} …>`. Wrap only the seeded first message:

```tsx
    const revealFirst = Boolean(room?.isDefault) && !inThread && message.messageSeq === 1
      && message.author.kind === "agent" && !messages.some((m) => m.author.kind === "human");
    const body = (text: string) => <RoomMessageContent message={message} text={linkifySessionMentions(linkifyMentions(text, message.mentions, participants), message.sessionSends)} /* …existing props unchanged… */ />;
    if (revealFirst) return <TypingReveal text={message.text} playKey={message.roomId}>{body}</TypingReveal>;
    return body(message.text);
```

Keep every other prop on `RoomMessageContent` exactly as it is today; only the `text` becomes a parameter. Add `messages` to the `useCallback` dependency list if it is not already there (it is used at line 429, so the array exists; check line 601).

- [ ] **Step 4: Run the tests to verify they pass**

Run the two files; then eslint on the touched files and `tsc --noEmit`.
Expected: all pass, no new warnings.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/messaging/TypingReveal.tsx apps/frontend/src/components/messaging/RoomPanel.tsx apps/frontend/src/components/messaging/__tests__/
git commit -m "feat(onboarding): the CEO's first message streams in, once, in a fresh starter channel"
```

---

### Task 5: Chat on screen during setup for a trial workspace

**Files:**
- Modify: `apps/frontend/src/components/chat/ProvisioningStepper.tsx` (the `isTrialLocal` const at line 218; the branch before `// Provisioning stepper — animated 4-step flow` at line ~1242; `AgentsBanner` at line ~1670)
- Test: `apps/frontend/src/components/chat/__tests__/ProvisioningStepper.readiness.test.tsx` (it already mocks `useProvisioningState` and `/billing/account`; mirror its setup)

**Interfaces:**
- Consumes: `useProvisioningState().phase` (`"container"` while the runtime is being created), `account?.subscription_status === "trial_local"`, the existing `AgentsBanner` component (`state`, `phase`, `onCheck`, `onSupport`, `isChecking`, `checkFailed`).
- Produces: for `isTrialLocal && phase === "container"`, the stepper renders `<AgentsBanner state="starting" …/>` followed by `{children}` instead of the provisioning card.

- [ ] **Step 1: Write the failing tests** (in `ProvisioningStepper.readiness.test.tsx`, using its existing helpers to set the mocked phase and the billing account; read its first 80 lines and reuse the helper names)

```tsx
it("shows the chat under a status line while a trial workspace's runtime is being created", () => {
  setProvisioning({ phase: "container", container: { runtime_phase: "provisioning" } });
  setBilling({ subscription_status: "trial_local" });
  render(<ProvisioningStepper><div data-testid="chat">chat</div></ProvisioningStepper>);
  expect(screen.getByTestId("chat")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent(/getting ready/i);
  expect(document.querySelector(".provision-card")).toBeNull();
});

it("keeps the full stepper for a paid workspace in the container phase", () => {
  setProvisioning({ phase: "container", container: { runtime_phase: "provisioning" } });
  setBilling({ subscription_status: "active" });
  render(<ProvisioningStepper><div data-testid="chat">chat</div></ProvisioningStepper>);
  expect(screen.queryByTestId("chat")).toBeNull();
  expect(document.querySelector(".provision-card")).not.toBeNull();
});

it("keeps the full stepper for a trial workspace in the payment or provider phases", () => {
  setProvisioning({ phase: "provider", container: null });
  setBilling({ subscription_status: "trial_local" });
  render(<ProvisioningStepper><div data-testid="chat">chat</div></ProvisioningStepper>);
  expect(screen.queryByTestId("chat")).toBeNull();
});
```

If the file's helpers have different names, adapt the two calls; the assertions stay.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/frontend && pnpm exec vitest run src/components/chat/__tests__/ProvisioningStepper.readiness.test.tsx`
Expected: the first test fails (`chat` not found; `.provision-card` present).

- [ ] **Step 3: Implement**

In `ProvisioningStepper.tsx`, immediately before the comment `// Provisioning stepper — animated 4-step flow` (line ~1242), add:

```tsx
  // A free workspace while its runtime is being created: the chat is already usable
  // (the room and the CEO's first message exist before the runtime is READY, and a reply
  // sent now is stored and delivered when it is), so show it under a one-line status
  // instead of behind the stepper. Paid flows keep the stepper: payment and provider
  // choices happen there.
  if (isTrialLocal && phase === "container") {
    return (
      <>
        <AgentsBanner
          state="starting"
          phase={phase}
          onCheck={handleAgentsBannerCheck}
          onSupport={() => capture("support_contact_clicked", { source: "agents_banner", phase, stuck })}
          isChecking={false}
          checkFailed={false}
        />
        {children}
      </>
    );
  }
```

`handleAgentsBannerCheck`, `capture`, and `stuck` are already in scope at the existing `AgentsBanner` render (lines 1228-1240); if `stuck` is defined below this point, move the new branch below its definition or compute it the same way. `AgentsBanner` is defined later in the module as a function declaration, so hoisting makes it available.

- [ ] **Step 4: Run the tests to verify they pass**

Run the same file plus `src/components/chat/__tests__/ProvisioningStepper.test.tsx`; then eslint on the file and `tsc --noEmit`.
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/chat/ProvisioningStepper.tsx apps/frontend/src/components/chat/__tests__/ProvisioningStepper.readiness.test.tsx
git commit -m "feat(onboarding): a trial workspace sees the chat under a status line while its runtime is created"
```

---

### Task 6: Confirm a reply during setup survives the run clock, then ship

**Files:**
- Read: `apps/backend/core/services/run_ledger.py`, `apps/backend/core/services/openclaw_work.py:430-450`
- Delete: `docs/superpowers/specs/2026-10-01-first-ten-minutes-design.md`, `docs/superpowers/plans/2026-10-01-first-ten-minutes.md`

**Interfaces:**
- Consumes: the never-started close timeout in `run_ledger.py` and the point at which a RUN ledger row is created relative to `WorkRetry("runtime is not provisioned yet")`.

- [ ] **Step 1: Read the clock**

Run: `grep -nE 'START|never|_S\s*=' apps/backend/core/services/run_ledger.py` and `sed -n 425,450p apps/backend/core/services/openclaw_work.py`.
Decide: if the RUN row is written only after the invoke is admitted (the retry happens before a row exists), nothing can be closed early and no change is needed. If the row is written before the retry, the never-started timeout must be at least 120 s (provisioning is ~50 s end to end; a reply at second ten leaves ~40 s). If it is shorter, raise it to 120 s in `run_ledger.py` and add a one-line test in its test file asserting the constant; do not add a hold.

- [ ] **Step 2: Record the finding**

Write the answer as a one-paragraph comment above the new branch in `ProvisioningStepper.tsx` ("A reply during setup is safe because …") so the next reader does not redo the trace.

- [ ] **Step 3: Delete the planning docs and commit**

```bash
git rm docs/superpowers/specs/2026-10-01-first-ten-minutes-design.md docs/superpowers/plans/2026-10-01-first-ten-minutes.md
git commit -m "chore(docs): first-ten-minutes spec and plan shipped; PR body is the record"
```

- [ ] **Step 4: Open the PR**

Push the branch and open one PR with: why (the Shafil trace, 37/42 solo), what (the five changes), the exact message copy, the run-clock finding, and the testing done (targeted files; CI runs the suites). End the body with the attribution line from the repo rules. Codex reviews; merge on green with its findings handled; prod ships on the next `deploy.yml` dispatch.
