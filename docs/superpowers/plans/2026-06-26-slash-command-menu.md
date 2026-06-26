# Slash-Command Menu Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Isol8 chat a discoverable `/` command menu — mirroring OpenClaw's control-ui menu — fed by the container's own `commands.list` RPC (built-ins + the active agent's skills + plugins), curated to a safe set, with the two output-rendering/session fixes the port needs.

**Architecture:** Isol8 already forwards raw chat text verbatim to the container's `chat.send`, and the OpenClaw runtime interprets any leading `/` as a command (verified: `chat.ts:3463`, `suppressCommandInterpretation` defaults false). So the menu is **discovery + dispatch only** — it fills the input and sends through the existing `agent_chat` path; it does NOT reimplement OpenClaw's client-side `executeLocal` RPC layer. The command list comes from the `commands.list` RPC over the existing gateway socket, scoped to the active agent. Curation uses two levers: the backend `commands` config block (privileged/flag-gated commands) and a client-side deny-list (always-on noise). Two genuinely UI-only commands (`/clear`, `/redirect`) and the `/btw` side-channel get explicit local handling.

**Tech Stack:** Next.js 16 / React 19 / Tailwind v4 / vitest (frontend); FastAPI / pytest / uv (backend). Existing hooks: `useGateway` (socket + `sendReq`/`onChatMessage`), `useGatewayRpc` (SWR-wrapped read RPC), `useAgentChat` (chat state + cache).

## Global Constraints

- **Theme (verbatim from `ChatInput.tsx`):** surface `#f3efe6`, borders `#e0dbd0`, muted text `#8a8578`/`#b5ae9e`, forest accent `#06402B` (hover `#0a5c3e`). Match this; do NOT recolor.
- **`/new` is reset-in-place for v1.** Multi-session/session-tabs is a deferred separate project. `/new` must NOT mint a new session key in the UI.
- **Commands dispatch through the existing send path** (`ChatInput.onSend` → `useAgentChat` → `useGateway.sendChat`, `{type:"agent_chat", agent_id, message}`). The menu never calls `chat.send` directly.
- **Never run full `pnpm test`** (~17.7GB RAM). Run targeted single-file vitest only, foreground: `npx vitest run <file>`.
- **Frontend CI runs `tsc --noEmit` over test files too.** Annotate any mock-builder return types; vitest strips types so type-incomplete mocks pass locally but fail CI.
- **`commands.list` entry shape** (from OpenClaw `commands-list-result.ts:130-155`): `{ name: string; nativeName?: string; textAliases?: string[]; description: string; category?: string; source: "native"|"skill"|"plugin"; scope: "native"|"text"|"both"; acceptsArgs: boolean; args?: SerializedArg[] }`. `SerializedArg = { name; description; type; required?: true; choices?: {value,label}[]; dynamic?: true }`. **No `tier` field is serialized.**
- **`commands` config gating** (OpenClaw `config/commands.flags.ts`): `isCommandFlagEnabled` returns true ONLY when `cfg.commands[key] === true`. Flag-gated (privileged) commands are off-by-default; `restart` is the exception (on unless `=== false`). Non-flag-gated commands are always present in `commands.list` and can only be hidden client-side.
- **Backend never deletes prod state.** All config changes go through `write_openclaw_config`; this plan only edits the static `commands` block builder.

---

## File Structure

**Frontend (new):**
- `apps/frontend/src/lib/slashCommands.ts` — types (`SlashCommand`, `CommandArg`), `parseSlashInput`, `filterAndGroupCommands`, the `HIDDEN_COMMANDS` deny-list. Pure, no React.
- `apps/frontend/src/lib/__tests__/slashCommands.test.ts` — unit tests for the above.
- `apps/frontend/src/hooks/useSlashCommands.ts` — fetch `commands.list` via `useGatewayRpc`, apply deny-list, return `{ commands, isLoading }`.
- `apps/frontend/src/components/chat/SlashCommandMenu.tsx` — the popover menu (grouped list + arg picker + keyboard nav). Presentational; driven by props.
- `apps/frontend/src/components/chat/__tests__/SlashCommandMenu.test.tsx` — component tests.

**Frontend (modify):**
- `apps/frontend/src/components/chat/ChatInput.tsx` — detect leading `/`, render `SlashCommandMenu`, route nav keys, fill-on-select.
- `apps/frontend/src/hooks/useGateway.tsx` — add `btw_result` to `ChatIncomingMessage` union + dispatch it through `onChatMessage`.
- `apps/frontend/src/hooks/useAgentChat.ts` — local `/clear` & `/redirect`; invalidate message cache + refetch history on `/reset|/compact|/new`; surface `btw_result`.
- `apps/frontend/src/components/chat/MessageList.tsx` — render a `btw_result` as a distinct "BTW" side card.

**Backend (modify):**
- `apps/backend/core/containers/config.py` — curate the `commands` flag block (drop `mcp`/`plugins`, add `restart: False`).
- `apps/backend/core/gateway/connection_pool.py` — recognize inbound `chat.side_result` and forward it as a `btw_result` frame.

**Backend (test):**
- `apps/backend/tests/containers/test_config_commands.py` — asserts the curated `commands` block.
- `apps/backend/tests/gateway/test_side_result_forward.py` — asserts `chat.side_result` → `btw_result`.

---

## Task 1: Curate the backend `commands` flag block

**Files:**
- Modify: `apps/backend/core/containers/config.py:1216-1220`
- Test: `apps/backend/tests/containers/test_config_commands.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the `commands` dict emitted into `openclaw.json` by `write_openclaw_config`. Downstream: `commands.list` will no longer surface `/mcp`/`/plugins` (GUI owns those); `/restart` is disabled.

- [ ] **Step 1: Write the failing test**

Find the config-builder entry point first: `grep -n "def write_openclaw_config\|\"commands\":" apps/backend/core/containers/config.py`. The builder is a pure function that returns the config dict (or writes it). Call it with minimal args in the test and assert the `commands` sub-dict.

```python
# apps/backend/tests/containers/test_config_commands.py
from core.containers.config import build_openclaw_config  # adjust to the actual builder name

def test_commands_block_is_curated():
    cfg = build_openclaw_config(owner_id="org_test", provider_choice="bedrock")  # match real signature
    commands = cfg["commands"]
    # Privileged surfaces the GUI owns must NOT be enabled from chat.
    assert "mcp" not in commands or commands["mcp"] is False
    assert "plugins" not in commands or commands["plugins"] is False
    # Host/lifecycle footguns stay off.
    assert commands.get("restart") is False
    assert "bash" not in commands or commands["bash"] is False
    assert "config" not in commands or commands["config"] is False
    # debug stays enabled (runtime-only, not persisted).
    assert commands.get("debug") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/containers/test_config_commands.py -v`
Expected: FAIL (`mcp`/`plugins` currently `True`, `restart` absent).

- [ ] **Step 3: Edit the `commands` block**

Replace `config.py:1216-1220` and update the comment above it:

```python
        # Chat-command surfaces. isCommandFlagEnabled (OpenClaw
        # config/commands.flags.ts) enables a privileged command only when its
        # flag is `True`; `restart` is the exception (on unless `False`).
        # Always-on commands (help/status/reset/model/…) are not gated here —
        # the chat menu hides the irrelevant ones client-side (see
        # lib/slashCommands.ts HIDDEN_COMMANDS).
        #
        # Curation: the control panel owns MCP/plugins/config writes, so those
        # must not be mutable from chat; host-shell (/bash) and gateway
        # /restart widen the trust boundary too far for chat.
        "commands": {
            "debug": True,  # /debug — runtime-only overrides for diagnostics (not persisted)
            "restart": False,  # /restart — gateway restart is operator-only, never from chat
        },
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/containers/test_config_commands.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/containers/config.py apps/backend/tests/containers/test_config_commands.py
git commit -m "feat(backend): curate chat commands block — drop /mcp /plugins, disable /restart"
```

---

## Task 2: Forward `chat.side_result` as a `btw_result` frame

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (the `event` routing in `_handle_message`, ~1088-1412; add a branch before the raw-forward `else` at ~1410)
- Test: `apps/backend/tests/gateway/test_side_result_forward.py`

**Interfaces:**
- Consumes: inbound gateway frame `{type:"event", event:"chat.side_result", payload:{kind, runId, sessionKey, agentId?, question, text, isError?, ts}}` (OpenClaw `chat.ts:2307-2328`).
- Produces: a forwarded frame `{type:"btw_result", question, text, isError?, agent_id?, runId}` delivered to `onChatMessage` subscribers. Task 8 (frontend) renders it.

- [ ] **Step 1: Discovery — confirm the inbound envelope**

The `chat.side_result` payload shape is from OpenClaw source; confirm how it arrives on the backend socket before coding. Add a temporary log OR inspect an existing test fixture:

Run: `grep -n "side_result\|chat.side_result\|_transform_agent_event\|_forward_to_frontends" apps/backend/core/gateway/connection_pool.py`
Expected: confirms there is NO existing `chat.side_result` branch (it currently hits the raw-forward `else` at ~1410-1412). Note the exact line of the `else` and the helper used to push to frontends (e.g. `_forward_to_frontends` / the per-member router). Use that helper in Step 3.

- [ ] **Step 2: Write the failing test**

```python
# apps/backend/tests/gateway/test_side_result_forward.py
import pytest
from core.gateway.connection_pool import GatewayConnectionPool  # adjust import/fixture to match existing gateway tests

@pytest.mark.asyncio
async def test_side_result_forwarded_as_btw_result(gateway_conn, captured_frontend_frames):
    # gateway_conn / captured_frontend_frames: reuse the fixtures the existing
    # connection_pool tests use to assert forwarded frames. If none exist,
    # build a minimal NodeConnection with a stub _forward_to_frontends that
    # appends to a list.
    await gateway_conn._handle_message({
        "type": "event",
        "event": "chat.side_result",
        "payload": {
            "kind": "btw", "runId": "run_1",
            "sessionKey": "agent:a1:user_1",
            "agentId": "a1", "question": "what time is it?",
            "text": "It is 3pm.", "ts": 1,
        },
    })
    btw = [f for f in captured_frontend_frames if f.get("type") == "btw_result"]
    assert len(btw) == 1
    assert btw[0]["text"] == "It is 3pm."
    assert btw[0]["question"] == "what time is it?"
    assert btw[0]["runId"] == "run_1"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/gateway/test_side_result_forward.py -v`
Expected: FAIL — currently forwarded raw as `{type:"event",...}`, no `btw_result`.

- [ ] **Step 4: Add the branch**

In `_handle_message`, inside the `if msg_type == "event":` block, BEFORE the final raw-forward `else`, add (use the same per-member routing the chat branch uses; `chat.side_result` carries `sessionKey` so the existing `target_member` logic applies):

```python
            if event_name == "chat.side_result":
                # /btw answers ride a distinct channel OpenClaw's web UI renders
                # as a side card; map it to a frontend chat-union frame so it
                # surfaces instead of being silently dropped by the raw-forward
                # path. Side-channel only — never mutates main-thread state.
                if is_channel_session or is_cron_session or is_isolated_session:
                    return
                frame = {
                    "type": "btw_result",
                    "question": payload.get("question", ""),
                    "text": payload.get("text", ""),
                    "runId": payload.get("runId"),
                }
                if payload.get("isError"):
                    frame["isError"] = True
                agent_id = _parse_session_key(session_key).get("agent_id")
                if agent_id:
                    frame["agent_id"] = agent_id
                await self._forward_to_frontends(frame, target_member=target_member)  # match real helper signature
                return
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/gateway/test_side_result_forward.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/gateway/test_side_result_forward.py
git commit -m "fix(gateway): forward chat.side_result as a btw_result frame so /btw answers render"
```

---

## Task 3: `slashCommands.ts` — types, parse, filter/group, deny-list

**Files:**
- Create: `apps/frontend/src/lib/slashCommands.ts`
- Test: `apps/frontend/src/lib/__tests__/slashCommands.test.ts`

**Interfaces:**
- Produces:
  - `type CommandArg = { name: string; description: string; type: string; required?: boolean; choices?: { value: string; label: string }[]; dynamic?: boolean }`
  - `type SlashCommand = { name: string; description: string; category: string; source: "native"|"skill"|"plugin"; acceptsArgs: boolean; args?: CommandArg[]; aliases?: string[] }`
  - `const HIDDEN_COMMANDS: Set<string>` — always-on commands hidden from the menu.
  - `parseSlashInput(text: string): { isCommand: boolean; query: string; commandName?: string; argText?: string }`
  - `filterAndGroupCommands(commands: SlashCommand[], query: string): { category: string; items: SlashCommand[] }[]`

- [ ] **Step 1: Write the failing test**

```typescript
// apps/frontend/src/lib/__tests__/slashCommands.test.ts
import { describe, it, expect } from "vitest";
import { parseSlashInput, filterAndGroupCommands, type SlashCommand } from "@/lib/slashCommands";

const CMDS: SlashCommand[] = [
  { name: "help", description: "List commands", category: "tools", source: "native", acceptsArgs: false },
  { name: "model", description: "Switch model", category: "model", source: "native", acceptsArgs: true,
    args: [{ name: "model", description: "id", type: "string", choices: [{ value: "opus", label: "Opus" }] }] },
  { name: "reset", description: "Reset session", category: "session", source: "native", acceptsArgs: true },
  { name: "deep-research", description: "Research report", category: "tools", source: "skill", acceptsArgs: true },
];

describe("parseSlashInput", () => {
  it("detects a bare slash as command mode with empty query", () => {
    expect(parseSlashInput("/")).toEqual({ isCommand: true, query: "" });
  });
  it("detects a partial command name", () => {
    expect(parseSlashInput("/mo")).toMatchObject({ isCommand: true, query: "mo" });
  });
  it("enters arg mode once a space follows the command", () => {
    expect(parseSlashInput("/model op")).toMatchObject({ isCommand: true, commandName: "model", argText: "op" });
  });
  it("is NOT command mode for normal text", () => {
    expect(parseSlashInput("hello").isCommand).toBe(false);
  });
  it("is NOT command mode for a slash mid-message", () => {
    expect(parseSlashInput("a/b").isCommand).toBe(false);
  });
});

describe("filterAndGroupCommands", () => {
  it("groups by category, ordered session→model→tools", () => {
    const groups = filterAndGroupCommands(CMDS, "");
    expect(groups.map((g) => g.category)).toEqual(["session", "model", "tools"]);
  });
  it("prefix-filters by name and floats exact-prefix first", () => {
    const groups = filterAndGroupCommands(CMDS, "mo");
    const flat = groups.flatMap((g) => g.items.map((i) => i.name));
    expect(flat).toEqual(["model"]);
  });
  it("matches description substrings too", () => {
    const flat = filterAndGroupCommands(CMDS, "research").flatMap((g) => g.items.map((i) => i.name));
    expect(flat).toContain("deep-research");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && npx vitest run src/lib/__tests__/slashCommands.test.ts`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `slashCommands.ts`**

```typescript
// apps/frontend/src/lib/slashCommands.ts
export type CommandArg = {
  name: string;
  description: string;
  type: string;
  required?: boolean;
  choices?: { value: string; label: string }[];
  dynamic?: boolean;
};

export type SlashCommand = {
  name: string;
  description: string;
  category: string;
  source: "native" | "skill" | "plugin";
  acceptsArgs: boolean;
  args?: CommandArg[];
  aliases?: string[];
};

// Always-on OpenClaw commands that are irrelevant to the Isol8 web surface
// (channel-routing, voice, ACP, privileged noise) or that the GUI owns. The
// backend `commands` flag block already blocks the privileged ones from
// running; this hides the always-on remainder from the menu. Tunable.
export const HIDDEN_COMMANDS = new Set<string>([
  "focus", "unfocus", "activation", "send", "acp", "tts",
  "elevated", "trace", "queue", "exec", "allowlist", "approve", "crestodian",
  // `/new` folds into `/reset` for v1 (no session tabs yet).
  "new",
]);

const CATEGORY_ORDER = ["session", "model", "tools", "agents"];
const SOURCE_ORDER = { native: 0, skill: 1, plugin: 2 } as const;

export function parseSlashInput(text: string): {
  isCommand: boolean;
  query: string;
  commandName?: string;
  argText?: string;
} {
  // Command mode only when the whole input starts with "/" (mirrors OpenClaw
  // chat.ts updateSlashMenu: ^/(\S*)$ for command, ^/(\S+)\s(.*)$ for args).
  const argMatch = /^\/(\S+)\s([\s\S]*)$/.exec(text);
  if (argMatch) {
    return { isCommand: true, query: argMatch[1], commandName: argMatch[1], argText: argMatch[2] };
  }
  const cmdMatch = /^\/(\S*)$/.exec(text);
  if (cmdMatch) {
    return { isCommand: true, query: cmdMatch[1] };
  }
  return { isCommand: false, query: "" };
}

export function filterAndGroupCommands(
  commands: SlashCommand[],
  query: string,
): { category: string; items: SlashCommand[] }[] {
  const q = query.toLowerCase();
  const matched = commands
    .filter((c) => !HIDDEN_COMMANDS.has(c.name))
    .filter((c) => {
      if (!q) return true;
      return (
        c.name.toLowerCase().startsWith(q) ||
        (c.aliases ?? []).some((a) => a.toLowerCase().replace(/^\//, "").startsWith(q)) ||
        c.description.toLowerCase().includes(q)
      );
    });

  const catRank = (cat: string) => {
    const i = CATEGORY_ORDER.indexOf(cat);
    return i === -1 ? CATEGORY_ORDER.length : i;
  };
  matched.sort((a, b) => {
    if (q) {
      const ax = a.name.toLowerCase().startsWith(q) ? 0 : 1;
      const bx = b.name.toLowerCase().startsWith(q) ? 0 : 1;
      if (ax !== bx) return ax - bx;
    }
    const c = catRank(a.category) - catRank(b.category);
    if (c !== 0) return c;
    const s = SOURCE_ORDER[a.source] - SOURCE_ORDER[b.source];
    if (s !== 0) return s;
    return a.name.localeCompare(b.name);
  });

  const groups: { category: string; items: SlashCommand[] }[] = [];
  for (const cmd of matched) {
    const cat = CATEGORY_ORDER.includes(cmd.category) ? cmd.category : "tools";
    let group = groups.find((g) => g.category === cat);
    if (!group) {
      group = { category: cat, items: [] };
      groups.push(group);
    }
    group.items.push(cmd);
  }
  groups.sort((a, b) => catRank(a.category) - catRank(b.category));
  return groups;
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && npx vitest run src/lib/__tests__/slashCommands.test.ts`
Expected: PASS

- [ ] **Step 5: Typecheck + commit**

Run: `cd apps/frontend && npx tsc --noEmit 2>&1 | grep slashCommands || echo "clean"`
Expected: `clean`

```bash
git add apps/frontend/src/lib/slashCommands.ts apps/frontend/src/lib/__tests__/slashCommands.test.ts
git commit -m "feat(frontend): slash-command parse/filter/group lib + deny-list"
```

---

## Task 4: `useSlashCommands` data hook

**Files:**
- Create: `apps/frontend/src/hooks/useSlashCommands.ts`
- Test: `apps/frontend/src/hooks/__tests__/useSlashCommands.test.tsx`

**Interfaces:**
- Consumes: `useGatewayRpc<CommandsListResult>("commands.list", { agentId, includeArgs: true, scope: "text" })`; `SlashCommand` from Task 3.
- Produces: `useSlashCommands(agentId: string | null): { commands: SlashCommand[]; isLoading: boolean }`.

- [ ] **Step 1: Discovery — confirm the live `commands.list` payload**

Before mapping, confirm the actual payload your container returns (field names/casing can drift from upstream source). In a dev `/chat` browser console:

```js
// after Clerk + gateway connected — uses the page's gateway socket if exposed,
// else call commands.list through the backend RPC proxy with the active agentId.
```
Expected: an object `{ commands: [{ name, description, category?, source, acceptsArgs, args?, textAliases? }, ...] }`. Confirm `name` has NO leading slash (scope:"text" → `resolvePrimaryTextName` strips it) and note whether `category` is present per entry. Adjust the mapper in Step 3 to the observed shape.

- [ ] **Step 2: Write the failing test**

```typescript
// apps/frontend/src/hooks/__tests__/useSlashCommands.test.tsx
import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { useSlashCommands } from "@/hooks/useSlashCommands";

// Mock useGatewayRpc to return a fixed commands.list payload.
vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: (): { data: unknown; isLoading: boolean } => ({
    data: {
      commands: [
        { name: "help", description: "List", category: "tools", source: "native", acceptsArgs: false },
        { name: "focus", description: "bind thread", category: "agents", source: "native", acceptsArgs: false },
      ],
    },
    isLoading: false,
  }),
}));

describe("useSlashCommands", () => {
  it("maps payload and drops deny-listed commands", () => {
    const { result } = renderHook(() => useSlashCommands("a1"));
    const names = result.current.commands.map((c) => c.name);
    expect(names).toContain("help");
    expect(names).not.toContain("focus"); // in HIDDEN_COMMANDS
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/frontend && npx vitest run src/hooks/__tests__/useSlashCommands.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 4: Implement the hook**

```typescript
// apps/frontend/src/hooks/useSlashCommands.ts
"use client";

import { useMemo } from "react";
import { useGatewayRpc } from "@/hooks/useGatewayRpc";
import { HIDDEN_COMMANDS, type SlashCommand } from "@/lib/slashCommands";

type CommandsListEntry = {
  name: string;
  description: string;
  category?: string;
  source: "native" | "skill" | "plugin";
  acceptsArgs?: boolean;
  args?: SlashCommand["args"];
  textAliases?: string[];
};
type CommandsListResult = { commands: CommandsListEntry[] };

export function useSlashCommands(agentId: string | null): {
  commands: SlashCommand[];
  isLoading: boolean;
} {
  const { data, isLoading } = useGatewayRpc<CommandsListResult>(
    agentId ? "commands.list" : null,
    agentId ? { agentId, includeArgs: true, scope: "text" } : undefined,
    { dedupingInterval: 60_000 },
  );

  const commands = useMemo<SlashCommand[]>(() => {
    const raw = data?.commands ?? [];
    return raw
      .map((e) => ({
        name: e.name.replace(/^\//, ""),
        description: e.description,
        category: e.category ?? "tools",
        source: e.source,
        acceptsArgs: Boolean(e.acceptsArgs),
        args: e.args,
        aliases: e.textAliases,
      }))
      .filter((c) => !HIDDEN_COMMANDS.has(c.name));
  }, [data]);

  return { commands, isLoading };
}
```

- [ ] **Step 5: Run test + typecheck**

Run: `cd apps/frontend && npx vitest run src/hooks/__tests__/useSlashCommands.test.tsx`
Expected: PASS
Run: `cd apps/frontend && npx tsc --noEmit 2>&1 | grep -E "useSlashCommands" || echo clean`
Expected: `clean`

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/hooks/useSlashCommands.ts apps/frontend/src/hooks/__tests__/useSlashCommands.test.tsx
git commit -m "feat(frontend): useSlashCommands hook over commands.list"
```

---

## Task 5: `SlashCommandMenu` component (grouped list + arg picker + keyboard)

**Files:**
- Create: `apps/frontend/src/components/chat/SlashCommandMenu.tsx`
- Test: `apps/frontend/src/components/chat/__tests__/SlashCommandMenu.test.tsx`

**Interfaces:**
- Consumes: `SlashCommand`, `filterAndGroupCommands` (Task 3).
- Produces: a controlled, presentational component:
  ```typescript
  type SlashCommandMenuProps = {
    commands: SlashCommand[];
    query: string;            // the partial command name after "/"
    activeIndex: number;      // selected row, controlled by ChatInput
    onSelect: (cmd: SlashCommand) => void;
    onActiveIndexChange: (n: number) => void;
  };
  ```
  Exposes `flattenVisible(commands, query): SlashCommand[]` for ChatInput's keyboard math.

- [ ] **Step 1: Write the failing test**

```typescript
// apps/frontend/src/components/chat/__tests__/SlashCommandMenu.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { SlashCommandMenu, flattenVisible } from "@/components/chat/SlashCommandMenu";
import type { SlashCommand } from "@/lib/slashCommands";

const CMDS: SlashCommand[] = [
  { name: "reset", description: "Reset session", category: "session", source: "native", acceptsArgs: true },
  { name: "model", description: "Switch model", category: "model", source: "native", acceptsArgs: true },
  { name: "deep-research", description: "Research", category: "tools", source: "skill", acceptsArgs: true },
];

describe("SlashCommandMenu", () => {
  it("renders category headers and command rows", () => {
    render(<SlashCommandMenu commands={CMDS} query="" activeIndex={0} onSelect={vi.fn()} onActiveIndexChange={vi.fn()} />);
    expect(screen.getByText("/reset")).toBeInTheDocument();
    expect(screen.getByText(/Research/)).toBeInTheDocument();
  });
  it("flattenVisible returns rows in display order for keyboard nav", () => {
    expect(flattenVisible(CMDS, "").map((c) => c.name)).toEqual(["reset", "model", "deep-research"]);
  });
  it("marks a skill-sourced row with its source", () => {
    render(<SlashCommandMenu commands={CMDS} query="" activeIndex={0} onSelect={vi.fn()} onActiveIndexChange={vi.fn()} />);
    expect(screen.getByText("Skills")).toBeInTheDocument(); // group label for source:"skill"
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && npx vitest run src/components/chat/__tests__/SlashCommandMenu.test.tsx`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement the component**

Build with: `filterAndGroupCommands` for the built-ins (category groups), and a separate "Skills"/"Plugins" group for `source !== "native"` (so skills/plugins read as a labeled tail, per the agreed design). Theme tokens from Global Constraints. Each row: `/<name>` + description, plus a `▸` affordance when `acceptsArgs && args?.[0]?.choices`. `flattenVisible` MUST return rows in the exact visual order so ChatInput's `activeIndex` math lines up.

```typescript
// apps/frontend/src/components/chat/SlashCommandMenu.tsx
"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import { filterAndGroupCommands, type SlashCommand } from "@/lib/slashCommands";

const CATEGORY_LABELS: Record<string, string> = {
  session: "Session", model: "Model & reasoning", tools: "Info", agents: "Agents",
};

// Built-ins grouped by category; skills + plugins collected into one labeled
// tail (commands.list has no tier, so we lean on source + the curated list).
function buildSections(commands: SlashCommand[], query: string) {
  const natives = commands.filter((c) => c.source === "native");
  const extras = commands.filter((c) => c.source !== "native");
  const sections: { label: string; items: SlashCommand[] }[] = [];
  for (const g of filterAndGroupCommands(natives, query)) {
    if (g.items.length) sections.push({ label: CATEGORY_LABELS[g.category] ?? "Info", items: g.items });
  }
  const extraGroups = filterAndGroupCommands(extras, query).flatMap((g) => g.items);
  if (extraGroups.length) sections.push({ label: "Skills", items: extraGroups });
  return sections;
}

export function flattenVisible(commands: SlashCommand[], query: string): SlashCommand[] {
  return buildSections(commands, query).flatMap((s) => s.items);
}

type SlashCommandMenuProps = {
  commands: SlashCommand[];
  query: string;
  activeIndex: number;
  onSelect: (cmd: SlashCommand) => void;
  onActiveIndexChange: (n: number) => void;
};

export function SlashCommandMenu({
  commands, query, activeIndex, onSelect, onActiveIndexChange,
}: SlashCommandMenuProps) {
  const sections = buildSections(commands, query);
  if (sections.length === 0) return null;
  let idx = -1;
  return (
    <div
      role="listbox"
      className="absolute bottom-full mb-2 left-0 right-0 max-h-80 overflow-y-auto rounded-2xl border border-[#e0dbd0] bg-white shadow-lg z-20"
    >
      {sections.map((section) => (
        <div key={section.label} className="py-1">
          <div className="px-3 py-1 text-[0.65rem] uppercase tracking-wide text-[#8a8578]">
            {section.label}
          </div>
          {section.items.map((cmd) => {
            idx += 1;
            const here = idx;
            const hasArgs = cmd.acceptsArgs && !!cmd.args?.[0]?.choices?.length;
            return (
              <button
                key={cmd.name}
                role="option"
                aria-selected={here === activeIndex}
                type="button"
                onMouseEnter={() => onActiveIndexChange(here)}
                onClick={() => onSelect(cmd)}
                className={cn(
                  "w-full flex items-center gap-2 px-3 py-1.5 text-left text-sm",
                  here === activeIndex ? "bg-[#06402B]/8 text-[#1a1a1a]" : "text-[#5a5549] hover:bg-[#f3efe6]",
                )}
              >
                <span className="font-medium text-[#06402B]">/{cmd.name}</span>
                <span className="flex-1 truncate text-[#8a8578]">{cmd.description}</span>
                {hasArgs && <span className="text-[#b5ae9e]">▸</span>}
              </button>
            );
          })}
        </div>
      ))}
      <div className="px-3 py-1 text-[0.65rem] text-[#b5ae9e] border-t border-[#f0ece3]">
        ↑↓ navigate · Tab fill · ↵ run · esc close
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && npx vitest run src/components/chat/__tests__/SlashCommandMenu.test.tsx`
Expected: PASS

- [ ] **Step 5: Typecheck + commit**

Run: `cd apps/frontend && npx tsc --noEmit 2>&1 | grep -E "SlashCommandMenu" || echo clean`
Expected: `clean`

```bash
git add apps/frontend/src/components/chat/SlashCommandMenu.tsx apps/frontend/src/components/chat/__tests__/SlashCommandMenu.test.tsx
git commit -m "feat(frontend): SlashCommandMenu component (grouped list + skills tail)"
```

> **Arg-picker note (v1 scope):** Step 3 surfaces the `▸` affordance but the second-level choice list is deferred — selecting an arg-bearing command fills `/<name> ` and lets the user type the value (the server resolves choices). A full drill-in picker is a fast follow once the menu ships; do NOT block v1 on it. If time allows, add a `mode: "command" | "args"` branch rendering `cmd.args[0].choices` as rows.

---

## Task 6: Wire the menu into `ChatInput`

**Files:**
- Modify: `apps/frontend/src/components/chat/ChatInput.tsx`
- Test: `apps/frontend/src/components/chat/__tests__/ChatInput.slash.test.tsx`

**Interfaces:**
- Consumes: `useSlashCommands` (Task 4), `SlashCommandMenu` + `flattenVisible` (Task 5), `parseSlashInput` (Task 3).
- Adds a prop `agentId?: string | null` to `ChatInputProps` (the active agent, already available where `ChatInput` is rendered in `AgentChatWindow.tsx`). Selecting a command fills the input; pressing ↵ on a no-arg command sends immediately via existing `handleSend`.

- [ ] **Step 1: Write the failing test**

```typescript
// apps/frontend/src/components/chat/__tests__/ChatInput.slash.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ChatInput } from "@/components/chat/ChatInput";

vi.mock("@/hooks/useSlashCommands", () => ({
  useSlashCommands: (): { commands: unknown[]; isLoading: boolean } => ({
    commands: [{ name: "help", description: "List", category: "tools", source: "native", acceptsArgs: false }],
    isLoading: false,
  }),
}));

describe("ChatInput slash menu", () => {
  it("opens the menu when the input starts with '/'", () => {
    render(<ChatInput onSend={vi.fn()} agentId="a1" />);
    const ta = screen.getByРlaceholderText?.("Ask anything") ?? screen.getByRole("textbox");
    fireEvent.change(ta, { target: { value: "/" } });
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(screen.getByText("/help")).toBeInTheDocument();
  });
  it("does not open the menu for normal text", () => {
    render(<ChatInput onSend={vi.fn()} agentId="a1" />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "hello" } });
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});
```

(Fix the typo `getByPlaceholderText` when writing — shown here verbatim only to mark intent; use `screen.getByRole("textbox")`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && npx vitest run src/components/chat/__tests__/ChatInput.slash.test.tsx`
Expected: FAIL (no listbox).

- [ ] **Step 3: Integrate into ChatInput**

Add `agentId` to `ChatInputProps`. Add state `const [activeIndex, setActiveIndex] = React.useState(0)`. Derive `const slash = parseSlashInput(input)` and `const { commands } = useSlashCommands(agentId ?? null)`. Compute `const visible = slash.isCommand ? flattenVisible(commands, slash.query) : []` and `const menuOpen = slash.isCommand && visible.length > 0 && !slash.argText`.

In `handleKeyDown`, BEFORE the existing Tab/Enter handling, when `menuOpen`:
```typescript
    if (menuOpen) {
      if (e.key === "ArrowDown") { e.preventDefault(); setActiveIndex((i) => (i + 1) % visible.length); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); setActiveIndex((i) => (i - 1 + visible.length) % visible.length); return; }
      if (e.key === "Escape") { e.preventDefault(); setInput(""); return; }
      if (e.key === "Tab" || e.key === "Enter") {
        e.preventDefault();
        const cmd = visible[activeIndex];
        if (!cmd) return;
        if (cmd.acceptsArgs) { setInput(`/${cmd.name} `); setActiveIndex(0); }   // fill, await arg
        else if (e.key === "Enter") { onSend(`/${cmd.name}`); setInput(""); }     // run no-arg now
        else { setInput(`/${cmd.name}`); }                                        // Tab fills
        return;
      }
    }
```
Reset `activeIndex` to 0 whenever `slash.query` changes (a `useEffect` on `slash.query`). Render `{menuOpen && <SlashCommandMenu commands={commands} query={slash.query} activeIndex={activeIndex} onSelect={(c) => { if (c.acceptsArgs) setInput(`/${c.name} `); else { onSend(`/${c.name}`); setInput(""); } }} onActiveIndexChange={setActiveIndex} />}` inside the `relative flex-1` wrapper (line ~340) so it positions above the input.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && npx vitest run src/components/chat/__tests__/ChatInput.slash.test.tsx`
Expected: PASS

- [ ] **Step 5: Pass `agentId` from the parent**

In `AgentChatWindow.tsx` (where `<ChatInput .../>` is rendered, ~line with `onSend={handleSend}`), add `agentId={agentId}`.

- [ ] **Step 6: Typecheck + commit**

Run: `cd apps/frontend && npx tsc --noEmit 2>&1 | grep -E "ChatInput|AgentChatWindow" || echo clean`
Expected: `clean`

```bash
git add apps/frontend/src/components/chat/ChatInput.tsx apps/frontend/src/components/chat/AgentChatWindow.tsx apps/frontend/src/components/chat/__tests__/ChatInput.slash.test.tsx
git commit -m "feat(frontend): wire slash-command menu into ChatInput"
```

---

## Task 7: Local `/clear` & `/redirect`; cache invalidation on `/reset|/compact`

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentChat.ts`
- Test: `apps/frontend/src/hooks/__tests__/useAgentChat.slash.test.tsx`

**Interfaces:**
- Consumes: existing `useAgentChat` internals — the module-level `_messageCache` keyed `${agentId}:${sessionName}`, `clearMessages`, `historyLoadedRef`/history refetch (see `useAgentChat.ts:147,242,596-627,1634-1664`), and the `sendMessage` entry (`:1437-1523`, which already special-cases `/btw`/`/side` at `:1475`).
- Produces: `sendMessage("/clear")` clears the local transcript without hitting the server registry (no server `/clear` handler exists); `sendMessage("/redirect <msg>")` aborts + resends; `/reset`, `/compact`, `/new` send to the server AND invalidate the local cache + refetch history so the UI doesn't diverge.

- [ ] **Step 1: Write the failing test**

```typescript
// apps/frontend/src/hooks/__tests__/useAgentChat.slash.test.tsx
// Mirror the existing useAgentChat.*.test.tsx harness (renderHook + mocked useGateway).
import { describe, it, expect, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useAgentChat } from "@/hooks/useAgentChat";

describe("useAgentChat slash handling", () => {
  it("clears the transcript locally on /clear without sending to the server", async () => {
    const { result } = renderHook(() => useAgentChat("agent-X", "session-1"));
    // seed a message via the harness, then:
    await act(async () => { await result.current.sendMessage("/clear"); });
    expect(result.current.messages).toHaveLength(0);
    // assert the gateway send spy was NOT called with a "/clear" agent_chat
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && npx vitest run src/hooks/__tests__/useAgentChat.slash.test.tsx`
Expected: FAIL (`/clear` currently dispatched to server, transcript not cleared).

- [ ] **Step 3: Implement in `sendMessage`**

Adjacent to the existing `/btw`/`/side` short-circuit (`useAgentChat.ts:1475`), add before `dispatchNow`:

```typescript
    const trimmedLower = trimmed.toLowerCase();
    // /clear and /redirect are control-ui-only in OpenClaw (no server handler);
    // implement them locally. /clear wipes the local transcript; /redirect
    // aborts the active run and resends the remainder as a fresh message.
    if (trimmedLower === "/clear") {
      clearMessages();
      return;
    }
    if (/^\/redirect\s+/i.test(trimmed)) {
      const rest = trimmed.replace(/^\/redirect\s+/i, "");
      abort();                 // existing cancel path
      if (rest) await dispatchNow(rest);
      return;
    }
    // /reset, /compact, /new mutate server-side session state; let them go to
    // the server, then invalidate our local cache + refetch history so the
    // displayed transcript matches the (reset/compacted) server session.
    if (/^\/(reset|compact|new)(\s|$)/i.test(trimmed)) {
      await dispatchNow(trimmed);
      invalidateHistoryAndRefetch();   // helper added below
      return;
    }
```

Add an `invalidateHistoryAndRefetch` helper that deletes the `_messageCache` entry for the current `cacheKey`, resets `historyLoadedRef.current = false`, and triggers the existing history fetch (the effect at `:596-627`). Keep it minimal — reuse the cache-key + fetch code already in the hook.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && npx vitest run src/hooks/__tests__/useAgentChat.slash.test.tsx`
Expected: PASS. Also re-run the existing suite for this hook to catch regressions:
Run: `npx vitest run src/hooks/__tests__/useAgentChat.btw.test.tsx`
Expected: PASS (the `/btw` path is untouched).

- [ ] **Step 5: Typecheck + commit**

Run: `cd apps/frontend && npx tsc --noEmit 2>&1 | grep -E "useAgentChat" || echo clean`
Expected: `clean`

```bash
git add apps/frontend/src/hooks/useAgentChat.ts apps/frontend/src/hooks/__tests__/useAgentChat.slash.test.tsx
git commit -m "feat(frontend): local /clear & /redirect; invalidate cache on /reset /compact /new"
```

---

## Task 8: Render `btw_result` as a side card

**Files:**
- Modify: `apps/frontend/src/hooks/useGateway.tsx` (union + dispatch)
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (consume `btw_result`)
- Modify: `apps/frontend/src/components/chat/MessageList.tsx` (render the card)
- Test: `apps/frontend/src/components/chat/__tests__/MessageList.btw.test.tsx`

**Interfaces:**
- Consumes: the `{type:"btw_result", question, text, isError?, agent_id?, runId}` frame from Task 2.
- Produces: a distinct, non-thread "BTW" card in the message list (not saved to history — mirrors OpenClaw's side card).

- [ ] **Step 1: Add the union member + dispatch (useGateway.tsx)**

In `ChatIncomingMessage` (`useGateway.tsx:37-105`), add:
```typescript
  | { type: "btw_result"; question: string; text: string; isError?: boolean; agent_id?: string; runId?: string }
```
In the chat-union dispatch (`:269-293`), add `"btw_result"` to the set of `type`s routed to `onChatMessage`.

- [ ] **Step 2: Write the failing render test**

```typescript
// apps/frontend/src/components/chat/__tests__/MessageList.btw.test.tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MessageList } from "@/components/chat/MessageList";
// Build the minimal props MessageList needs (see existing MessageList tests for
// the shape) with one side-result entry, and assert it renders a labeled card.
describe("MessageList /btw", () => {
  it("renders a BTW side card with the answer text", () => {
    // ...render MessageList with a messages array containing a btw side-result item...
    expect(screen.getByText(/BTW/i)).toBeInTheDocument();
    expect(screen.getByText("It is 3pm.")).toBeInTheDocument();
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/frontend && npx vitest run src/components/chat/__tests__/MessageList.btw.test.tsx`
Expected: FAIL.

- [ ] **Step 4: Implement**

In `useAgentChat.ts`, in the `onChatMessage` handler, on `msg.type === "btw_result"` append a side-result entry to the rendered list (a lightweight item shape `{ kind: "btw", question, text, isError }`, NOT pushed into `_messageCache`/history — it's ephemeral, matching OpenClaw "Not saved to chat history"). In `MessageList.tsx`, render that entry as a bordered card labeled "BTW · not saved", question in muted text, `text` as the body, error styling when `isError`. Theme tokens from Global Constraints.

- [ ] **Step 5: Run test + typecheck**

Run: `cd apps/frontend && npx vitest run src/components/chat/__tests__/MessageList.btw.test.tsx`
Expected: PASS
Run: `cd apps/frontend && npx tsc --noEmit 2>&1 | grep -E "MessageList|useGateway|useAgentChat" || echo clean`
Expected: `clean`

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/hooks/useGateway.tsx apps/frontend/src/hooks/useAgentChat.ts apps/frontend/src/components/chat/MessageList.tsx apps/frontend/src/components/chat/__tests__/MessageList.btw.test.tsx
git commit -m "feat(frontend): render /btw answers as a side card (btw_result)"
```

---

## Manual verification (after all tasks)

On dev `/chat` against a freshly-reprovisioned container (so the curated `commands` block is live):
1. Type `/` → menu opens, grouped, with the active agent's skills under "Skills".
2. `/help`, `/status`, `/usage` → run and render as chat replies.
3. `/btw what time is it` → answer renders as a BTW side card (not in the thread).
4. `/clear` → transcript clears locally; `/reset` → server resets and the UI follows.
5. `/mcp` is ABSENT from the menu (curated out); typing `/mcp` does nothing useful.
6. Skill commands (e.g. `/deep-research`) appear and launch a run.

> Reprovision is required for the `commands` curation (Task 1) to take effect on an existing container — it's written by `write_openclaw_config` at provision time.

---

## Self-Review notes

- **Tier gating intentionally dropped** — `commands.list` omits `tier`; the curated/deny-listed set keeps the menu short without it. Documented in Task 5 and Global Constraints.
- **Curation is two levers** — backend flags (Task 1) for privileged commands, client deny-list (Task 3 `HIDDEN_COMMANDS`) for always-on noise. Neither alone is sufficient; both are in scope.
- **Two discovery steps** (Task 2 Step 1, Task 4 Step 1) resolve container-runtime shapes before their code steps — these are verification actions with expected output, not placeholders.
- **`/new` = reset-in-place** is enforced two ways: it's in `HIDDEN_COMMANDS` (not shown) and, if typed, handled by Task 7's reset-family branch. Session tabs remain a separate deferred project.
- **Arg-picker drill-in deferred** to a fast-follow (Task 5 note) — v1 fills `/<name> ` and lets the user type the value.
