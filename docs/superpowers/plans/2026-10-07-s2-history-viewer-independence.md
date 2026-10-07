# S2 History and Viewer Independence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A viewer of a shared session gets the holder's recent output as scrollable history before the live screen, a quiet session never looks stale, and nothing a viewer does reaches the owner's terminal.

**Architecture:** Two new `local.session.screen` kinds ride the unchanged relay: `history` (log bytes from the holder, fetched by the Mac on a short-lived `watch` connection with the holder's existing REPLAY, sent before a snapshot) and `ping` (no data, no place in the seq order, sent every 10 s instead of a redraw while nothing was output). The backend validates and forwards both like output. The viewer keeps xterm scrollback (5000), starts over only on a new relay run (`epoch`), and redraws only the visible screen on later snapshots; the ordering module moves to `packages/shared` so the phone can reuse it (S3). The desktop panel uses the same history fetch, so no holder process changes (running holders keep working).

**Tech Stack:** Rust (Tauri app, `std::os::unix::net`), Python 3 / Pydantic v2 (FastAPI backend), TypeScript / React 19 / xterm.js / vitest.

**Spec:** `docs/superpowers/specs/2026-10-07-terminal-everywhere-design.md` (§1, §3 decisions 5 and 6, §5.2, §5.3, §5.6, §11); slice: `docs/superpowers/plans/2026-10-07-terminal-everywhere.md` "S2".

## Global Constraints

- History is at most `BURST` = 256 KiB (`screen.rs:43`) per burst, out of the holder's 1 MiB log (`holder/run.rs:18` `LOG_CAP`); the panel may take the whole `LOG_CAP` (loopback, no budget).
- Frames carry at most 11 KiB raw (`CHUNK`, `screen.rs:29`; `MAX_RAW_BYTES`, `session_screen.py:150`); the golden frame keys (`packages/shared/fixtures/local-session-frames/session-screen.json`) do not change.
- Per-session budget stays 2 MiB raw a minute, Mac bucket burst 256 KiB (`screen.rs:41-43`, `session_screen.py:151`). History never takes the snapshot's place: it is sized to the budget left minus a 64 KiB reserve.
- Viewer scrollback is 5000 lines (`terminal.js:5`). Stale = no frame and no ping for 30 s (`SessionTerminalView.tsx:12`).
- Nothing is stored: history is read from the holder's memory and forwarded (spec §8).
- A viewer never resizes, scrolls or selects on the Mac (spec §3.5): no new outbound call from the viewer.
- Do not touch `local_sessions/transport.rs`, `lifecycle.rs`, `codex.rs`, `local_sessions.rs`, or `holder/run.rs` (shipped today: #3799 `holder/identity.rs`, #3802 transport.rs, #3790 End). Nothing in this plan needs them.
- Rollout order: backend and web (Tasks 1-3) reach prod before a desktop build carrying Tasks 5-6. An older web viewer drops `history`/`ping` at the event guard and would fall stale on a quiet session.
- Rust test runs use `TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}'` as CI does (`.github/workflows/desktop-build.yml:80`).

## Review Focus

- History frames that arrive after their snapshot (AppSync does not keep order): expected no history and a correct screen, never old bytes over the live screen. Pinned in Task 2 ("history arriving after its snapshot…").
- A snapshot larger than 11 KiB continues as `output` frames: an in-sync snapshot must not draw its tail alone. The viewer therefore writes every snapshot (visible-screen redraw, scrollback kept) instead of skipping in-sync ones. Pinned in Task 2 ("a snapshot that spans frames…") and Task 3.
- A Mac whose relay socket is down while a history burst goes out: the pump must not block for more than `PACE` (3 s), and the burst counts as lost. Pinned in Task 5 ("a history burst that never finds room…").
- A holder with no output yet, or one older than watch clients: no history, the relay and the panel still start. Pinned in Task 4.
- A second viewer joining a running relay while the first is synced: the first writes nothing new, keeps its seq order, and the second gets history then the screen. Pinned in Task 2 ("a viewer that has the run's screen…") and Task 5 ("a grown viewer set…").

---

### Task 1: Backend accepts `history` and `ping` screen frames

**Files:**
- Modify: `apps/backend/core/services/session_screen.py:24-27` (imports), `:157-175` (`Screen`)
- Test: `apps/backend/tests/unit/services/test_session_screen.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `Screen.kind: Literal["snapshot", "output", "history", "ping"]`; a `ping` must have `data == ""`. `forward` (L197-238) is unchanged: it forwards every valid kind to current viewers and spends `len(raw)` (0 for a ping).

- [ ] **Step 1: Write the failing tests** (append to `test_session_screen.py`)

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["history", "ping"])
async def test_history_and_ping_frames_reach_current_viewers_like_output(transport, kind):
    sid = await start(transport)
    await messaging_repo.ensure_user_stream(OWNER, BOB.user_id)
    await session_screen.watch(BOB, sid)
    transport[4].clear()
    body = frame(sid, kind=kind, data="" if kind == "ping" else "G1tI")
    await session_screen.forward(relay(transport), "conn-1", body)
    published = await _published(transport)
    assert [(e["type"], e["payload"]["kind"]) for _, e in published] == [("workspace.session.screen", kind)]


def test_a_ping_carries_no_data_and_other_kinds_are_refused():
    golden = fixture("session-screen")
    session_screen.Screen.model_validate({**golden, "kind": "ping", "data": ""})
    for bad in ({"kind": "ping", "data": "G1tI"}, {"kind": "input"}):
        with pytest.raises(ValidationError):
            session_screen.Screen.model_validate({**golden, **bad})
```

Add `from pydantic import ValidationError` to the test file's imports (after `import pytest`).

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_screen.py -q -k "history_and_ping or carries_no_data"`
Expected: FAIL (kind `history`/`ping` refused, so nothing is published; `ping` validation raises).

- [ ] **Step 3: Implement**

In `session_screen.py`, change the pydantic import to `from pydantic import Field, field_validator, model_validator` and replace the `kind` line of `Screen` (L165) and add a validator after `_base64`:

```python
    # history: the holder's recent output, before a snapshot, for viewers with no screen yet;
    # ping: no data, proves a quiet stream is alive (spec §5.2).
    kind: Literal["snapshot", "output", "history", "ping"]
```

```python
    @model_validator(mode="after")
    def _ping_is_empty(self) -> Screen:
        if self.kind == "ping" and self.data:
            raise ValueError("a ping carries no data")
        return self
```

- [ ] **Step 4: Run the whole file**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_session_screen.py -q`
Expected: PASS (all, including the golden-frame test).

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/services/session_screen.py apps/backend/tests/unit/services/test_session_screen.py
git commit -m "feat(sessions): screen frames may be history or ping"
```

---

### Task 2: Shared screen ordering with history and ping

**Files:**
- Create: `packages/shared/src/live/sessionScreen.ts` (moved from `apps/frontend/src/components/messaging/sessionScreen.ts`, extended)
- Modify: `packages/shared/src/live/types.ts:226-229` (`SessionScreen.kind`), `:410-416` (guard); `packages/shared/src/index.ts:15` (export)
- Modify: `packages/shared/src/live/sessionScreen.test.ts` (extend guard test; append ordering tests moved from the web)
- Delete: `apps/frontend/src/components/messaging/sessionScreen.ts`, `apps/frontend/src/components/messaging/__tests__/sessionScreen.test.ts`
- Modify: `apps/frontend/src/components/messaging/SessionTerminalView.tsx:4,7` (import from `@lightbulb/shared`)

**Interfaces:**
- Consumes: `SessionScreen` from `./types`.
- Produces (exported from `@lightbulb/shared`): `type ScreenOrder = { epoch: number; next: number; synced: boolean; held: SessionScreen[]; history: SessionScreen[] }`, `NEW_SCREEN: ScreenOrder`, `receiveScreen(order: ScreenOrder, frame: SessionScreen): { order: ScreenOrder; write: SessionScreen[] }`. `SessionScreen.kind: "snapshot" | "output" | "history" | "ping"`.
- Ordering rule (the contract Task 5 emits against): within one `epoch`, every frame but `ping` takes one `seq`. A history burst takes consecutive seqs below the snapshot that follows it (output frames may sit between them). A viewer with nothing drawn in that epoch writes the newest unbroken run of that epoch's history frames below the snapshot, then the snapshot, then frames in seq order. A viewer already synced in the epoch consumes history frames in seq order and writes none. A `ping` carries the next seq without taking it and is never ordered or written.

- [ ] **Step 1: Write the failing tests** (`packages/shared/src/live/sessionScreen.test.ts`; keep the existing guard test and change its `bad` list, then append the rest)

```ts
import { expect, it, test } from "vitest";
import { NEW_SCREEN, receiveScreen, type ScreenOrder } from "./sessionScreen";
import { LIVE_EVENT_TYPES, parseLiveEvent, type SessionScreen } from "./types";
```

In the existing test, after `expect(parseLiveEvent(event)).toEqual(event);` add:

```ts
  for (const good of [{ kind: "history" }, { kind: "ping", data: "" }]) {
    expect(parseLiveEvent({ ...event, payload: { ...event.payload, ...good } })).not.toBeNull();
  }
```

and extend its `bad` list with `{ kind: "ping" }` (a ping with data is refused).

Append:

```ts
const f = (seq: number, kind: SessionScreen["kind"] = "output", epoch = 1): SessionScreen =>
  ({ sessionId: "s", generation: "g", epoch, seq, cols: 80, rows: 24, kind, data: "" });
function feed(frames: SessionScreen[], order: ScreenOrder = NEW_SCREEN) {
  const written: string[] = [];
  for (const frame of frames) { const step = receiveScreen(order, frame); order = step.order; written.push(...step.write.map((w) => `${w.kind[0]}${w.seq}`)); }
  return { order, written };
}

it("writes nothing before a snapshot, then everything in seq order", () => {
  expect(feed([f(5), f(3, "snapshot"), f(4)]).written).toEqual(["s3", "o4", "o5"]);
});
it("drops duplicates and frames from an older relay run", () => {
  expect(feed([f(0, "snapshot", 2), f(1, "output", 2), f(1, "output", 2), f(7, "output", 1)]).written).toEqual(["s0", "o1"]);
});
it("stops writing after a gap it cannot fill, until the next snapshot, and takes no history for a run it has drawn", () => {
  const gap = [f(0, "snapshot"), ...Array.from({ length: 17 }, (_, i) => f(i + 2))];
  const { order, written } = feed(gap);
  expect(written).toEqual(["s0"]);
  expect(feed([f(29, "history"), f(30, "snapshot"), f(31)], order).written).toEqual(["s30", "o31"]);
});
it("a new viewer writes the run's history, then the snapshot, then output; output between them is covered by the snapshot", () => {
  expect(feed([f(1, "history"), f(0, "history"), f(2), f(3, "snapshot"), f(4)]).written).toEqual(["h0", "h1", "s3", "o4"]);
});
it("a viewer that has the run's screen consumes history in order and writes none of it", () => {
  expect(feed([f(0, "snapshot"), f(2, "history"), f(1, "history"), f(3, "snapshot"), f(4)]).written).toEqual(["s0", "s3", "o4"]);
});
it("history arriving after its snapshot is never written", () => {
  expect(feed([f(2, "snapshot"), f(0, "history"), f(1, "history"), f(3)]).written).toEqual(["s2", "o3"]);
});
it("a lost history chunk costs only the older part", () => {
  expect(feed([f(0, "history"), f(2, "history"), f(3, "snapshot")]).written).toEqual(["h2", "s3"]);
});
it("a new relay run brings its own history; the old run's is ignored", () => {
  const { order } = feed([f(0, "snapshot", 1)]);
  expect(feed([f(5, "history", 1), f(0, "history", 2), f(1, "snapshot", 2)], order).written).toEqual(["h0", "s1"]);
});
it("a ping has no place in the order", () => {
  expect(feed([f(0, "snapshot"), f(1, "ping"), f(1), f(2, "ping")]).written).toEqual(["s0", "o1"]);
});
it("a snapshot that spans frames is written whole, in sync or not", () => {
  expect(feed([f(0, "snapshot"), f(1), f(2, "snapshot"), f(3)]).written).toEqual(["s0", "o1", "s2", "o3"]);
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd packages/shared && pnpm vitest run src/live/sessionScreen.test.ts`
Expected: FAIL (`./sessionScreen` does not exist; guard refuses `history`).

- [ ] **Step 3: Implement**

`packages/shared/src/live/types.ts` L226-229:

```ts
/** One piece of a running session's live terminal, for a person watching it (spec §5.2). `epoch`
 *  is one relay run on the Mac and `seq` orders frames within it; a "snapshot" redraws the
 *  screen, "history" is the holder's recent output sent before a snapshot, "ping" (no data, no
 *  seq of its own) proves a quiet stream alive. `data` is base64 terminal output. Nothing stores it. */
export type SessionScreen = {
  sessionId: string; generation: string; epoch: number; seq: number;
  cols: number; rows: number; kind: "snapshot" | "output" | "history" | "ping"; data: string;
};
```

Guard (L416, the last line of the `workspace.session.screen` case) becomes:

```ts
        && (payload.kind === "ping" ? payload.data === "" : SCREEN_KINDS.has(payload.kind as string) && text(payload.data));
```

with, next to `SESSION_ATTENTION_STATES` (L222): `const SCREEN_KINDS = new Set<string>(["snapshot", "output", "history"]);`

`packages/shared/src/live/sessionScreen.ts`:

```ts
import type { SessionScreen } from "./types";

/** A read-only terminal's place in the Mac's frame stream (spec §5.2-5.3). Within one relay run
 *  (`epoch`) every frame but a ping takes one `seq`. A viewer writes from that run's snapshot on,
 *  in seq order; a viewer with nothing drawn in the run first writes the newest unbroken run of its
 *  `history` frames below the snapshot. A viewer that already draws the run consumes history in
 *  order and writes none. Frames can arrive out of order or never: up to 16 early ones are held;
 *  past that the view waits for the next snapshot. */
export type ScreenOrder = { epoch: number; next: number; synced: boolean; held: SessionScreen[]; history: SessionScreen[] };
export const NEW_SCREEN: ScreenOrder = { epoch: -1, next: 0, synced: false, held: [], history: [] };
const HELD = 16;
/** ceil(256 KiB / 11 KiB): one history burst at most (the Mac's BURST over CHUNK). */
const HISTORY = 24;

export function receiveScreen(order: ScreenOrder, frame: SessionScreen): { order: ScreenOrder; write: SessionScreen[] } {
  if (frame.kind === "ping") return { order, write: [] };
  if (frame.kind === "snapshot") {
    const old = frame.epoch < order.epoch || (frame.epoch === order.epoch && order.synced && frame.seq < order.next);
    if (old) return { order, write: [] };
    // History only for a run this viewer has drawn nothing of: scrollback it has stays as it is.
    const before = frame.epoch === order.epoch ? [] : newestRun(order.history, frame);
    return drain({ epoch: frame.epoch, next: frame.seq + 1, synced: true, history: [],
      held: order.held.filter((f) => f.epoch === frame.epoch && f.seq > frame.seq) }, [...before, frame]);
  }
  if (order.synced && frame.epoch === order.epoch) {
    if (frame.seq < order.next) return { order, write: [] };
    if (frame.seq === order.next) return drain({ ...order, next: order.next + 1 }, frame.kind === "history" ? [] : [frame]);
  }
  if (frame.kind === "history" && frame.epoch > order.epoch) {
    const kept = order.history.filter((f) => f.epoch >= frame.epoch);
    if (kept.some((f) => f.epoch > frame.epoch || f.seq === frame.seq)) return { order, write: [] };
    return { order: { ...order, history: [...kept, frame].slice(-HISTORY) }, write: [] };
  }
  const held = [...order.held, frame];
  return held.length > HELD ? { order: { ...order, synced: false, held: [] }, write: [] } : { order: { ...order, held }, write: [] };
}

/** The snapshot's history frames below it, newest unbroken run only: a lost chunk costs the older part. */
function newestRun(history: SessionScreen[], snapshot: SessionScreen) {
  const mine = history.filter((f) => f.epoch === snapshot.epoch && f.seq < snapshot.seq).sort((a, b) => a.seq - b.seq);
  let start = mine.length - 1;
  while (start > 0 && mine[start - 1].seq === mine[start].seq - 1) start -= 1;
  return mine.slice(Math.max(start, 0));
}

function drain(order: ScreenOrder, write: SessionScreen[]) {
  let { next, held } = order;
  for (;;) {
    const i = held.findIndex((f) => f.epoch === order.epoch && f.seq === next);
    if (i < 0) break;
    // A history frame in a run this viewer already draws: its place in the order, nothing to write.
    if (held[i].kind !== "history") write.push(held[i]);
    held = held.filter((_, j) => j !== i); next += 1;
  }
  return { order: { ...order, next, held }, write };
}
```

`packages/shared/src/index.ts`, after L15: `export { NEW_SCREEN, receiveScreen, type ScreenOrder } from "./live/sessionScreen";`

`SessionTerminalView.tsx`: L4 becomes `import { ApiError, NEW_SCREEN, receiveScreen, type LiveEvent, type LocalSession, type SessionScreen } from "@lightbulb/shared";` and L7 is removed. Then `git rm apps/frontend/src/components/messaging/sessionScreen.ts apps/frontend/src/components/messaging/__tests__/sessionScreen.test.ts`.

- [ ] **Step 4: Run tests**

Run: `cd packages/shared && pnpm vitest run src/live/sessionScreen.test.ts && pnpm exec tsc --noEmit`
Expected: PASS.
Run: `cd apps/frontend && pnpm vitest run src/components/messaging/__tests__/SessionTerminalView.test.tsx src/components/messaging/__tests__/LocalSessionPanel.liveTerminal.test.tsx`
Expected: PASS (behaviour unchanged for snapshot/output streams).

- [ ] **Step 5: Commit**

```bash
git add -A packages/shared/src apps/frontend/src/components/messaging
git commit -m "feat(sessions): shared screen ordering with history and ping"
```

---

### Task 3: Web viewer keeps scrollback and starts over only on a new run

**Files:**
- Modify: `apps/frontend/src/components/messaging/SessionTerminalView.tsx:11-12` (stale comment), `:54-60` (locals), `:81-88` (`apply`), `:93` (`scrollback`)
- Test: `apps/frontend/src/components/messaging/__tests__/SessionTerminalView.test.tsx`

**Interfaces:**
- Consumes: `NEW_SCREEN`, `receiveScreen` from `@lightbulb/shared` (Task 2).
- Produces: no new props. Behaviour: `scrollback: 5000`; `term.reset()` only when a frame of a new `epoch` is written; every snapshot is written as `ESC[2J ESC[H` + its bytes in one `write`; a `ping` resets the stale timer (as every frame already does) and writes nothing.

- [ ] **Step 1: Write the failing tests**

Change the mock and the shared hooks at the top of the test file (L8-17):

```ts
const writes: Uint8Array[] = [];
const hooks: { data?: (d: string) => void; key?: (e: unknown) => boolean; term?: { cols: number; rows: number };
  options?: { scrollback?: number }; resets: number } = { resets: 0 };
vi.mock("@xterm/xterm", () => ({ Terminal: class {
  cols = 80; rows = 24; options = { disableStdin: true };
  constructor(options: { scrollback?: number }) { hooks.term = this; hooks.options = options; }
  open() {} reset() { hooks.resets += 1; } dispose() {}
  onData(cb: (d: string) => void) { hooks.data = cb; }
  attachCustomKeyEventHandler(cb: (e: unknown) => boolean) { hooks.key = cb; }
  resize(c: number, r: number) { this.cols = c; this.rows = r; }
  write(data: Uint8Array, done?: () => void) { writes.push(data); done?.(); }
} }));
afterEach(() => { writes.length = 0; hooks.resets = 0; vi.useRealTimers(); });
const CLEAR = [0x1b, 0x5b, 0x32, 0x4a, 0x1b, 0x5b, 0x48];
```

Make `screenEvent` take an epoch and data: `const screenEvent = (sessionId: string, kind = "snapshot", seq = 0, epoch = 1, data = "G1tI") => ({ …payload: { sessionId, generation: "g1", epoch, seq, cols: 100, rows: 30, kind, data } }) as LiveEvent;` (rest unchanged). In the first test, the expectation at L30 becomes `expect(writes.map((w) => Array.from(w))).toEqual([[...CLEAR, 0x1b, 0x5b, 0x48]]);`.

Append:

```ts
const mountView = async (props: Partial<Parameters<typeof SessionTerminalView>[0]> = {}) => {
  let emit: (e: LiveEvent) => void = () => {};
  const onLive = vi.fn();
  const watch = vi.fn(async () => undefined);
  render(<SessionTerminalView session={{ sessionId: "s1", generation: "g1" }} onEvent={(h) => { emit = h; return () => {}; }}
    watch={watch} onLive={onLive} {...props} />);
  await act(async () => { await vi.dynamicImportSettled(); });
  return { emit: (e: LiveEvent) => act(() => { emit(e); }), onLive, watch };
};

it("writes history under the first snapshot, keeps it when later snapshots redraw, and starts over on a new run", async () => {
  const { emit } = await mountView();
  expect(hooks.options?.scrollback).toBe(5000);
  emit(screenEvent("s1", "history", 0, 1, btoa("old\r\n")));
  emit(screenEvent("s1", "snapshot", 1));
  expect(writes.map((w) => Array.from(w))).toEqual([Array.from(new TextEncoder().encode("old\r\n")), [...CLEAR, 0x1b, 0x5b, 0x48]]);
  emit(screenEvent("s1", "snapshot", 2));
  expect(hooks.resets).toBe(1);
  emit(screenEvent("s1", "snapshot", 0, 2));
  expect(hooks.resets).toBe(2);
});

it("a ping keeps a quiet terminal live and draws nothing", async () => {
  vi.useFakeTimers();
  const { emit, onLive } = await mountView();
  emit(screenEvent("s1"));
  for (let i = 0; i < 3; i += 1) {
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    emit(screenEvent("s1", "ping", 1, 1, ""));
  }
  expect(onLive).toHaveBeenLastCalledWith(true);
  expect(writes).toHaveLength(1);
});

it("the viewer's own size never reaches the Mac or changes the terminal's size", async () => {
  let resized: () => void = () => {};
  vi.stubGlobal("ResizeObserver", class { constructor(cb: () => void) { resized = cb; } observe() {} disconnect() {} });
  const input = vi.fn(async () => undefined);
  const { emit, watch } = await mountView({ input, admitted: true });
  emit(screenEvent("s1"));
  act(() => { resized(); resized(); });
  expect(input).not.toHaveBeenCalled();
  expect(watch).toHaveBeenCalledTimes(1);
  expect([hooks.term?.cols, hooks.term?.rows]).toEqual([100, 30]);
  vi.unstubAllGlobals();
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd apps/frontend && pnpm vitest run src/components/messaging/__tests__/SessionTerminalView.test.tsx`
Expected: FAIL (scrollback is 0; no CLEAR prefix; a second snapshot resets).

- [ ] **Step 3: Implement** (`SessionTerminalView.tsx`)

L11-12:

```ts
/** A snapshot or a ping comes at least every 10 s; this long with neither and the picture is frozen. */
const STALE_AFTER_MS = 30_000;
```

After `const decode = …` (L16):

```ts
/** Clears the visible screen only: xterm's erase never pushes it into scrollback, so history stays. */
const CLEAR = new TextEncoder().encode("\x1b[2J\x1b[H");
```

In the effect, next to `let order = NEW_SCREEN;` (L55): `let drawn = -1; // the relay run on screen; a new one starts the terminal over`

Replace `apply` (L81-88):

```ts
    const apply = (frames: SessionScreen[]) => {
      if (!term) { queued.push(...frames); return; }
      for (const frame of frames) {
        if (frame.epoch !== drawn) { term.reset(); drawn = frame.epoch; }
        if (term.cols !== frame.cols || term.rows !== frame.rows) term.resize(frame.cols, frame.rows);
        let bytes = decode(frame.data);
        if (frame.kind === "snapshot") {
          const redraw = new Uint8Array(CLEAR.length + bytes.length); redraw.set(CLEAR); redraw.set(bytes, CLEAR.length);
          bytes = redraw; setLive(true); onLive(true);
        }
        term.write(bytes, fit);
      }
    };
```

L93: `scrollback: 0` becomes `scrollback: 5000`.

- [ ] **Step 4: Run tests**

Run: `cd apps/frontend && pnpm vitest run src/components/messaging/__tests__/SessionTerminalView.test.tsx src/components/messaging/__tests__/LocalSessionPanel.liveTerminal.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/messaging/SessionTerminalView.tsx apps/frontend/src/components/messaging/__tests__/SessionTerminalView.test.tsx
git commit -m "feat(sessions): viewers keep 5000 lines of scrollback and start over only on a new run"
```

---

### Task 4: Fetch a holder's recent output on a side connection

**Files:**
- Modify: `apps/desktop/src-tauri/src/holder/manager.rs:4` (imports), add `history`/`history_on` after `hello` (L265)
- Test: `apps/desktop/src-tauri/src/holder/manager.rs` `mod tests` (L286+)

**Interfaces:**
- Consumes: holder protocol (`HELLO`, `SNAPSHOT`, `OUTPUT`, `REPLAY`, `split_offset`, `with_offset`); the holder's watch HELLO answer (`run.rs:358-364`: facts, then `SNAPSHOT(total, screen)`) and REPLAY answer (`run.rs:395-406`: optional `GAP`, then `OUTPUT(max(from, oldest), log[..total])`).
- Produces: `pub fn history(socket: &Path, max: usize) -> Option<Vec<u8>>`: the last ≤ `max` bytes of output up to about now, starting after the first `\n` unless they begin at session offset 0; `Some(empty)` when nothing was printed; `None` when the holder does not answer (or predates watch clients). No holder change: REPLAY is as old as the holder.

Why a side connection: on the relay's own connection the REPLAY answer interleaves with live output and the holder's first snapshot is already queued, so history could not go before it. A short `watch` connection answers in order (facts, snapshot, live output, the answer), and the answer is the one OUTPUT whose offset does not continue the live stream: it starts at `max(from, oldest) < total` and live frames start exactly where the previous ended.

- [ ] **Step 1: Write the failing test** (in `manager.rs` tests)

```rust
    /// A holder that answers a watch HELLO with its screen at `end`, sends `live` output, then
    /// answers a REPLAY with `answer`; returns the `from` it was asked for.
    fn replaying(end: u64, live: &'static [u8], answer: Option<(u64, &'static [u8])>) -> (UnixStream, std::thread::JoinHandle<Option<u64>>) {
        use super::super::protocol::{split_offset, with_offset, OUTPUT, REPLAY, SNAPSHOT};
        let (mac, mut holder) = UnixStream::pair().unwrap();
        let fake = std::thread::spawn(move || {
            assert_eq!(read_frame(&mut holder).unwrap(), (HELLO, br#"{"role":"watch"}"#.to_vec()));
            holder.write_all(&encode(HELLO, b"{}")).unwrap();
            holder.write_all(&encode(SNAPSHOT, &with_offset(end, b"screen"))).unwrap();
            holder.write_all(&encode(OUTPUT, &with_offset(end, live))).unwrap();
            let (from, bytes) = answer?;
            let (kind, ask) = read_frame(&mut holder).unwrap();
            assert_eq!(kind, REPLAY);
            holder.write_all(&encode(OUTPUT, &with_offset(from, bytes))).unwrap();
            Some(split_offset(&ask).unwrap().0)
        });
        (mac, fake)
    }

    #[test]
    fn history_is_the_log_tail_before_the_screen_from_a_line_and_never_live_output() {
        // Log "one\ntwo\nthree\nfour\n" is offsets 0..19; the answer runs to the live end (23).
        let (mut mac, fake) = replaying(19, b"live", Some((9, b"hree\nfour\nlive")));
        assert_eq!(history_on(&mut mac, 10).as_deref(), Some(&b"four\nlive"[..]));
        assert_eq!(fake.join().unwrap(), Some(9), "asked from max bytes under the screen");
        let (mut mac, fake) = replaying(5, b"", Some((0, b"hi\nyo")));
        assert_eq!(history_on(&mut mac, 100).as_deref(), Some(&b"hi\nyo"[..]), "the session's start is kept whole");
        assert_eq!(fake.join().unwrap(), Some(0));
        let (mut mac, fake) = replaying(0, b"", None);
        assert_eq!(history_on(&mut mac, 100).as_deref(), Some(&b""[..]));
        assert_eq!(fake.join().unwrap(), None, "nothing printed yet: nothing asked");
        let (mut mac, mut old) = UnixStream::pair().unwrap();
        old.write_all(&encode(HELLO, b"{}")).unwrap();
        old.write_all(&encode(super::super::protocol::OUTPUT, &super::super::protocol::with_offset(0, b"x"))).unwrap();
        assert_eq!(history_on(&mut mac, 100), None, "a holder older than watch clients sends no screen first");
    }
```

- [ ] **Step 2: Run to verify it fails**

Run: `TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --lib holder::manager`
Expected: FAIL to compile (`history_on` not found).

- [ ] **Step 3: Implement** (`manager.rs`)

L4: `use super::protocol::{encode, read_frame, split_offset, with_offset, HELLO, OUTPUT, REPLAY, SNAPSHOT};`

After `hello` (L265):

```rust
/// The holder's last `max` bytes of output for a viewer's scrollback (spec §5.2 `history`, §5.6),
/// from a line start unless they start the session. Asked on a short-lived `watch` connection, so
/// it never mixes with a relay's own stream. Empty when nothing was printed; None when the holder
/// does not answer.
pub fn history(socket: &Path, max: usize) -> Option<Vec<u8>> {
    let mut conn = UnixStream::connect(socket).ok()?;
    conn.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    history_on(&mut conn, max)
}

fn history_on(conn: &mut UnixStream, max: usize) -> Option<Vec<u8>> {
    conn.write_all(&encode(HELLO, br#"{"role":"watch"}"#)).ok()?;
    if read_frame(conn).ok()?.0 != HELLO {
        return None;
    }
    // A watch client's first frame is the screen, at the log's current end.
    let (kind, screen) = read_frame(conn).ok()?;
    let (end, _) = split_offset(&screen).filter(|_| kind == SNAPSHOT)?;
    if end == 0 || max == 0 {
        return Some(vec![]);
    }
    conn.write_all(&encode(REPLAY, &with_offset(end - end.min(max as u64), b""))).ok()?;
    // Live output goes on from `end`, each frame where the last ended; the answer is the one that
    // does not (it starts below and runs to where the live output is now).
    let mut next = end;
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        let (kind, payload) = read_frame(conn).ok()?;
        if kind != OUTPUT {
            continue; // GAP: the older part is gone, the answer follows.
        }
        let (from, bytes) = split_offset(&payload)?;
        if from == next {
            next += bytes.len() as u64;
            continue;
        }
        let cut = bytes.len().saturating_sub(max);
        let kept = &bytes[cut..];
        if from + cut as u64 == 0 {
            return Some(kept.to_vec());
        }
        // Mid-session: start at a line, not inside one or inside an escape sequence.
        let line = kept.iter().position(|b| *b == b'\n').map_or(0, |i| i + 1);
        return Some(kept[line..].to_vec());
    }
    None
}
```

- [ ] **Step 4: Run tests**

Run: `TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --lib holder::`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src/holder/manager.rs
git commit -m "feat(desktop): read a holder's recent output on a side watch connection"
```

---

### Task 5: Relay sends history before snapshots and pings while quiet

**Files:**
- Modify: `apps/desktop/src-tauri/src/local_sessions/screen.rs`: module doc L1-12; constants L29-43; `Ctl` L55-72; `settle` L328-332; `nudge_relay` L370-374; `run` L396-406; `watch` L408-426; `Budget` L436-456; `Out` L458-493; `pump` L505-559; `frames_for` L561-575; tests L579+
- Test: same file, `mod tests`

**Interfaces:**
- Consumes: `manager::history(socket: &Path, max: usize) -> Option<Vec<u8>>` (Task 4); the ordering contract of Task 2 (history seqs below the snapshot after them; ping reuses the next seq).
- Produces: frames `{"type":"local.session.screen", …, "kind": "history"|"ping", …}` with the golden keys. `pump(…, budget: Budget, history: &dyn Fn(usize) -> Option<Vec<u8>>)`, `watch(…, send, history: &dyn Fn(usize) -> Option<Vec<u8>>)`. `Ctl.history: AtomicBool` (viewer set grew), `Ctl.beat: Duration` (10 s; tests shorten it).

Budget: a burst asks for at most `room()` = budget left − 64 KiB (≤ `BURST`), so on a fresh relay history is ≤ 192 KiB and the holder's snapshot (usually ≤ 20 KiB) still fits; a short budget trims history from its old end. Snapshots keep their overdraw rule (`Budget::take`, L448-455).

- [ ] **Step 1: Write the failing tests** (append to `mod tests`; and add `, &|_| None` as the last argument of the three existing `pump(…)` calls at L622, L665 and in `output_over_the_budget_is_dropped_and_a_snapshot_follows_once_the_budget_is_back`, and of the `watch(…)` call in `a_nudge_streams_the_holders_snapshot_before_any_ack`)

```rust
    fn kinds(sent: &[Value]) -> Vec<(String, u64)> {
        sent.iter().map(|f| (f["kind"].as_str().unwrap().into(), f["seq"].as_u64().unwrap())).collect()
    }

    #[test]
    fn a_new_run_sends_history_before_the_holders_first_snapshot_and_leaves_room_for_it() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(30_720, b"\x1b[Hscreen"))).unwrap();
        tx.send((EXIT, b"{}".to_vec())).unwrap();
        let asked = std::cell::Cell::new(0);
        let sent = std::cell::RefCell::new(vec![]);
        pump(&rx, &mut Vec::<u8>::new(), ("sid", "gen"), (120, 40), &Ctl::new(0), &|f| { sent.borrow_mut().push(f); true }, Budget::new(),
            &|max| { asked.set(max); Some(vec![b'h'; 30 * 1024]) }).unwrap();
        assert_eq!(asked.get(), BURST - HISTORY_RESERVE);
        assert_eq!(kinds(&sent.borrow()), [("history".into(), 0), ("history".into(), 1), ("history".into(), 2), ("snapshot".into(), 3)]);
    }

    #[test]
    fn a_grown_viewer_set_gets_history_then_a_fresh_snapshot_and_a_short_budget_trims_history_from_its_old_end() {
        let (tx, rx) = std::sync::mpsc::channel();
        tx.send((SNAPSHOT, with_offset(10, b"screen"))).unwrap();
        let ctl = Arc::new(leased(Duration::from_millis(1500)));
        let later = ctl.clone();
        std::thread::spawn(move || { std::thread::sleep(Duration::from_millis(200)); later.history.store(true, SeqCst); });
        let mut holder = Answering { tx, answer: vec![(SNAPSHOT, with_offset(10, b"fresh"))], asked: vec![] };
        // No refill: 20 bytes of room at the start, 4 left when the viewer arrives.
        let budget = Budget { tokens: (HISTORY_RESERVE + 20) as f64, cap: BURST as f64, per_s: 0.0, at: Instant::now() };
        let sent = std::cell::RefCell::new(vec![]);
        pump(&rx, &mut holder, ("sid", "gen"), (80, 24), &ctl, &|f| { sent.borrow_mut().push(f); true }, budget,
            &|_| Some(b"0123456789".to_vec())).unwrap();
        let sent = sent.into_inner();
        assert_eq!(kinds(&sent), [("history".into(), 0), ("snapshot".into(), 1), ("history".into(), 2), ("snapshot".into(), 3)]);
        let data = |f: &Value| base64::engine::general_purpose::STANDARD.decode(f["data"].as_str().unwrap()).unwrap();
        assert_eq!((data(&sent[0]), data(&sent[2])), (b"0123456789".to_vec(), b"6789".to_vec()));
        assert_eq!(holder.asked.len(), 1, "one fresh snapshot after the history");
    }

    #[test]
    fn a_quiet_relay_pings_instead_of_redrawing_and_output_brings_the_snapshot_back() {
        let run = |output: bool| {
            let (tx, rx) = std::sync::mpsc::channel();
            tx.send((SNAPSHOT, with_offset(0, b"screen"))).unwrap();
            if output {
                tx.send((OUTPUT, with_offset(6, b"x"))).unwrap();
            }
            let mut ctl = leased(Duration::from_millis(1300));
            ctl.beat = Duration::from_millis(400);
            let mut holder = Answering { tx, answer: vec![(SNAPSHOT, with_offset(7, b"screen"))], asked: vec![] };
            let sent = std::cell::RefCell::new(vec![]);
            pump(&rx, &mut holder, ("sid", "gen"), (80, 24), &ctl, &|f| { sent.borrow_mut().push(f); true }, Budget::new(), &|_| None).unwrap();
            (kinds(&sent.into_inner()), holder.asked.len())
        };
        let (quiet, asked) = run(false);
        assert_eq!(asked, 0, "a quiet screen is never redrawn");
        assert_eq!(quiet[0], ("snapshot".into(), 0));
        assert!(quiet.len() >= 3 && quiet[1..].iter().all(|k| *k == ("ping".into(), 1)), "{quiet:?}");
        let (busy, asked) = run(true);
        assert_eq!(asked, 1);
        assert_eq!(busy[..4], [("snapshot".into(), 0), ("output".into(), 1), ("snapshot".into(), 2), ("ping".into(), 3)]);
    }

    #[test]
    fn a_history_burst_waits_for_room_in_the_relay_queue() {
        let ctl = Ctl::new(0);
        let calls = std::cell::Cell::new(0);
        let sent = std::cell::RefCell::new(vec![]);
        let send = |f: Value| { calls.set(calls.get() + 1); calls.get() % 2 == 0 && { sent.borrow_mut().push(f); true } };
        let mut out = Out { ids: ("sid", "gen"), epoch: 1, seq: 0, ctl: &ctl, send: &send, budget: Budget::new(), stale: false, dirty: false };
        out.history(&|_| Some(vec![b'h'; 3 * CHUNK]), (80, 24));
        assert_eq!(kinds(&sent.borrow()), [("history".into(), 0), ("history".into(), 1), ("history".into(), 2)]);
        assert!(!out.stale, "a full queue for a moment is not a lost frame");
    }

    #[test]
    fn a_history_burst_that_never_finds_room_is_lost_within_its_pace() {
        let ctl = Ctl::new(0);
        let mut out = Out { ids: ("sid", "gen"), epoch: 1, seq: 0, ctl: &ctl, send: &|_| false, budget: Budget::new(), stale: false, dirty: false };
        let started = Instant::now();
        out.history(&|_| Some(vec![b'h'; 3 * CHUNK]), (80, 24));
        assert!(started.elapsed() < PACE + Duration::from_millis(500));
        assert!(out.stale && ctl.snapshot.load(SeqCst) && out.seq == 3, "lost: viewers skip to the next snapshot");
    }
```

In `a_nudged_relay_survives_an_ack_without_it_inside_the_grace_and_stops_after`: after the "first ack" block add

```rust
        assert!(!relays[&sid].history.load(SeqCst));
        settle(&mut relays, &BTreeMap::from([(sid.clone(), 1_760_000_001)]), &fake);
        assert!(relays[&sid].history.load(SeqCst), "a new viewer arrived: history, then a snapshot");
```

and change the last block's `assert!(relays[&sid].snapshot.load(SeqCst) && relays[&sid].kept());` to `assert!(relays[&sid].history.load(SeqCst) && relays[&sid].kept());`.

- [ ] **Step 2: Run to verify they fail**

Run: `TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --lib local_sessions::screen`
Expected: FAIL to compile (`HISTORY_RESERVE`, `Ctl.history`, `Ctl.beat`, `Out.dirty`, `Out::history` missing; `pump` arity).

- [ ] **Step 3: Implement** (`screen.rs`)

Module doc, replace the sentence "a fresh snapshot every 10 s, when the viewer set grows, after a resize and after a lost frame (at most one a second)." with:

```rust
//! the holder's recent output as `history` frames before the snapshot of a new or grown viewer
//! set; a fresh snapshot after a resize and after a lost frame (at most one a second); and every
//! 10 s a snapshot when output went out since the last one, else a `ping` (spec §5.2).
```

Constants, after `BURST` (L43):

```rust
/// Budget a history burst leaves for the snapshot and output after it.
const HISTORY_RESERVE: usize = 64 * 1024;
/// How long a history burst may wait for room in the shallow relay queue before it counts as lost.
const PACE: Duration = Duration::from_secs(3);
```

`Ctl` fields (after `snapshot: AtomicBool,`) and `Ctl::new`:

```rust
    /// The viewer set grew (an ack's `since` moved, or a nudge): history, then a snapshot.
    history: AtomicBool,
    /// How often a relay's viewers hear from it (SNAPSHOT_EVERY; tests shorten it).
    beat: Duration,
```

```rust
        Self { since: AtomicU64::new(since), epoch: AtomicU64::new(0), typist: Mutex::new(None), snapshot: AtomicBool::new(false),
            history: AtomicBool::new(false), beat: SNAPSHOT_EVERY, stop: AtomicBool::new(false),
            until: Mutex::new(Instant::now() + LEASE), keep: Mutex::new(Instant::now()) }
```

`settle` L328-332 and `nudge_relay` L370-374: each `ctl.snapshot.store(true, SeqCst);` becomes `ctl.history.store(true, SeqCst);` (the pump asks for the snapshot after the history). The nudge comment becomes `// Running already: the new viewer needs history and a screen now, not in up to 10 s.`

`run` L402-403:

```rust
    let socket = holder.socket.clone();
    let history = move |max: usize| manager::history(&socket, max);
    let result = watch(&mut conn, sid, &generation, ctl, &send_out, &history);
```

`watch` signature gains `history: &dyn Fn(usize) -> Option<Vec<u8>>` as its last parameter, and its last line becomes `pump(&frames, conn, (sid, generation), size, ctl, send, Budget::new(), history)`.

`Budget`, after `take`:

```rust
    /// Bytes a history burst may use now: what is left above the reserve, at most BURST.
    fn room(&mut self) -> usize {
        (self.left() - HISTORY_RESERVE as f64).clamp(0.0, BURST as f64) as usize
    }
```

`Out`: add the field `/// Output went out since the last snapshot: the beat redraws (it heals frames lost past this Mac) instead of pinging.\n    dirty: bool,`; in `emit`, after `self.stale = false;` add `self.dirty = !snapshot;`; then add:

```rust
    /// The holder's recent output for viewers with no screen in this run yet (spec §5.2), sized to
    /// what the budget allows now so the snapshot after it always fits: a short budget trims it
    /// from its old end. Paced into the shallow relay queue (a burst is up to 24 frames); a frame
    /// that finds no room within PACE is lost like any other.
    fn history(&mut self, fetch: &dyn Fn(usize) -> Option<Vec<u8>>, size: (u16, u16)) {
        let room = self.budget.room();
        let Some(bytes) = (room > 0).then(|| fetch(room)).flatten().filter(|b| !b.is_empty()) else { return };
        let bytes = &bytes[bytes.len().saturating_sub(room)..];
        self.budget.take(bytes.len(), true);
        let deadline = Instant::now() + PACE;
        for frame in frames_for(self.ids, self.epoch, &mut self.seq, size, "history", bytes) {
            while !(self.send)(frame.clone()) {
                if Instant::now() >= deadline {
                    self.lost();
                    return;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        }
    }
    /// Proof of life on a quiet screen: no data, and no place in the order (it carries the next
    /// seq without taking it), so a lost one costs nothing.
    fn ping(&mut self, size: (u16, u16)) {
        let mut seq = self.seq;
        for frame in frames_for(self.ids, self.epoch, &mut seq, size, "ping", b"") {
            let _ = (self.send)(frame);
        }
    }
```

`pump`: add `history: &dyn Fn(usize) -> Option<Vec<u8>>` as the last parameter; construct `Out { …, stale: false, dirty: false }`; replace L509-525 (from `let mut pending` through the snapshot request) with:

```rust
    // A new run's viewers get history first; the holder's own first snapshot is queued behind it.
    ctl.history.store(false, SeqCst);
    out.history(history, size);
    let mut pending: Vec<u8> = Vec::new();
    let mut held_since: Option<Instant> = None;
    // The holder sends a watch client its first snapshot unasked; a request may follow at once
    // (a resize, a lost frame).
    let mut last_snapshot = Instant::now().checked_sub(SNAPSHOT_GAP).unwrap_or_else(Instant::now);
    // Viewers hear from the relay every `beat`: a snapshot when output went out since the last one
    // (it heals frames lost past this Mac), else a ping. A quiet screen is never redrawn.
    let mut last_beat = Instant::now();
    loop {
        if ctl.stopped() {
            return Ok(());
        }
        if ctl.history.swap(false, SeqCst) {
            out.history(history, size);
            ctl.snapshot.store(true, SeqCst);
        }
        if last_beat.elapsed() >= ctl.beat {
            last_beat = Instant::now();
            if out.dirty { ctl.snapshot.store(true, SeqCst) } else { out.ping(size) }
        }
        if ctl.snapshot.load(SeqCst) && last_snapshot.elapsed() >= SNAPSHOT_GAP && out.budget.left() > 0.0 {
            ctl.snapshot.store(false, SeqCst);
            to_holder.write_all(&encode(SNAPSHOT, b"")).map_err(|e| e.to_string())?;
            last_snapshot = Instant::now();
        }
```

(the `match frames.recv_timeout(TICK)` block and the flush after it are unchanged).

`frames_for`: doc becomes `/// A snapshot that does not fit one frame continues as "output" frames with the next seqs; history stays "history".` and the kind line becomes `"kind": if i == 0 || kind == "history" { kind } else { "output" },`.

- [ ] **Step 4: Run tests**

Run: `TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --lib local_sessions::`
Expected: PASS (new tests plus the existing relay, budget, nudge and golden-shape tests).

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src/local_sessions/screen.rs
git commit -m "feat(desktop): relay sends history before snapshots and pings while the screen is quiet"
```

---

### Task 6: The app's own panel opens with full scrollback

**Files:**
- Modify: `apps/desktop/src-tauri/src/holder/panel.rs:13` (imports), `:157-169` (`relay` after HELLO), add `history_frame`
- Test: `apps/desktop/src-tauri/src/holder/panel.rs` `mod tests`

**Interfaces:**
- Consumes: `manager::history` (Task 4); `run::LOG_CAP` (1 MiB, `run.rs:18`); `terminal.js` writes `OUTPUT` payloads after their 8-byte offset (`terminal.js` `ws.onmessage`), with `scrollback: 5000` (L5).
- Produces: `fn history_frame(bytes: Option<Vec<u8>>) -> Option<Vec<u8>>`. Page order on open: the holder's HELLO facts (a joined page takes the session's size from them), then one `OUTPUT` with up to 1 MiB of history, then everything else in order (the snapshot first).

Decision (spec §5.6 says "asks for `replayFrom`"): `replayFrom` does not fit. In the holder it means "raw output instead of a snapshot" for a terminal that started the session (`cli.rs:476-478`, `run.rs:344-356`); it is not read for a `join` client at all (`run.rs:330-338`), and teaching the holder to clamp a too-small `from` changes only holders started after the update, while running sessions keep their old holder process. The side-connection fetch of Task 4 works with every running holder, gives history plus the snapshot, and changes no holder code. `terminal.js` needs no change.

- [ ] **Step 1: Write the failing test**

```rust
    #[test]
    fn the_panel_gets_history_as_one_output_frame_and_nothing_when_there_is_none() {
        use crate::holder::protocol::{split_offset, OUTPUT};
        let frame = history_frame(Some(b"old\nlines".to_vec())).unwrap();
        let (kind, payload) = read_frame(&mut frame.as_slice()).unwrap();
        assert_eq!((kind, split_offset(&payload).unwrap().1), (OUTPUT, &b"old\nlines"[..]));
        assert!(history_frame(Some(vec![])).is_none() && history_frame(None).is_none());
    }
```

- [ ] **Step 2: Run to verify it fails**

Run: `TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --lib holder::panel`
Expected: FAIL to compile (`history_frame` not found).

- [ ] **Step 3: Implement** (`panel.rs`)

L13: `use super::protocol::{encode, read_frame, with_offset, DETACHED, EXIT, HELLO, INPUT, OUTPUT, RESIZE};`

Above `fn relay`:

```rust
/// The holder's recent output as one frame for the page (spec §5.6), which writes an OUTPUT
/// payload after its offset: the panel's scrollback is full on open. None when there is none.
fn history_frame(bytes: Option<Vec<u8>>) -> Option<Vec<u8>> {
    bytes.filter(|b| !b.is_empty()).map(|b| encode(OUTPUT, &with_offset(0, &b)))
}
```

In `relay`, between `let mut to_page = …;` (L160) and `std::thread::spawn` (L161):

```rust
    // The facts first (a joined page takes the session's size from them), then the recent output
    // as scrollback, then everything else in order: the screen is next.
    let (kind, facts) = read_frame(&mut from_holder)?;
    to_page.send(Message::Binary(encode(kind, &facts).into())).map_err(std::io::Error::other)?;
    if let Some(frame) = history_frame(manager::history(&socket, super::run::LOG_CAP)) {
        to_page.send(Message::Binary(frame.into())).map_err(std::io::Error::other)?;
    }
```

Update the `relay` comment at L148-149 to end with: `The page then gets the holder's facts, its recent output for scrollback, and the screen.`

- [ ] **Step 4: Run tests**

Run: `TAURI_CONFIG='{"bundle":{"externalBin":[],"resources":[]}}' cargo test --manifest-path apps/desktop/src-tauri/Cargo.toml --lib holder::`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/desktop/src-tauri/src/holder/panel.rs
git commit -m "feat(desktop): the session panel opens with the holder's recent output as scrollback"
```

---

### Task 7: Acceptance on prod (manual, after a desktop build)

**Files:** none. Record receipts (screenshots, relay log lines) in the PR body.

Preconditions: the PR is merged; a prod dispatch carried the backend and web (Tasks 1-3); then a desktop build carrying Tasks 4-6 is installed on the founder's Mac (no dev desktop build exists). The founder's Mac runs a held Claude Code session with several screens of output; Isol8 Admin watches it in Chrome.

- [ ] **Step 1:** Viewer opens the running session: the last screens of history appear above the live screen, and the viewer can scroll up through them.
- [ ] **Step 2:** In the viewer, scroll, select text and resize the browser window: the owner's terminal on the Mac is unchanged (compare a screenshot of the Mac terminal before and after; same cols×rows, same content).
- [ ] **Step 3:** Hide the viewer tab for at least 60 s while the session prints, then bring it back: the picture is current within 2 s and the history above it is still there.
- [ ] **Step 4:** Leave the session idle for 2 minutes with the viewer open: the view never shows the stale state ("Reconnecting…" once S1 ships; today the transcript fallback) and never redraws.
- [ ] **Step 5:** Open the session in the Mac app's own panel: scrolling up shows earlier output, not only the current screen.
- [ ] **Step 6:** Relay log on the Mac (`relay_log`): no lost or over-budget lines during the idle period of Step 4; backend logs show no `session screen frame dropped` for the session.
- [ ] **Step 7:** Commit nothing; paste the receipts into the PR description and request Codex review.
