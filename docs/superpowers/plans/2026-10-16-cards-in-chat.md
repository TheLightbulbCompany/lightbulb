# Cards in Chat (Track A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An agent's room reply can carry one ```` ```openui-lang ```` fence that web, desktop and phone render as a card built from twenty fixed components, and a click on it posts the person's answer as a message that wakes the agent.

**Architecture:** One component vocabulary lives in `packages/shared` as OpenUI `lang-core` definitions (names, zod props, descriptions, no renderers). Web binds those definitions to shadcn-based React renderers and mounts OpenUI's `Renderer` from a react-markdown `code` override; the phone binds the same names to React Native views from its markdown `fence` rule. A click calls the room's normal send with the agent mentioned and `replyToMessageId` set to the card's message, which is also how a later render knows the card was answered. The agent learns the vocabulary from a skill reference whose signature block is generated from the shared library and pinned by a test.

**Tech Stack:** `@openuidev/lang-core` 1.0.1, `@openuidev/react-lang` 1.0.1, `zod` 4.3.6 (`zod/v4`), react-markdown 10 (web), react-native-markdown-display 7 (phone), recharts 3.8 via `components/ui/chart.tsx`, vitest (shared, web), jest-expo (phone), pytest (skill build check).

**Spec:** `docs/superpowers/specs/2026-10-16-generative-ui-and-artifacts-design.md` (sections A1–A5 and the A mechanisms). Tracks B and C get their own plans; A6 (cards inside documents) lands with Track C's document pane, which mounts the same markdown override this plan builds.

## Global Constraints

- pnpm is `9.15.0` and runs dependency postinstall scripts; the four OpenUI packages go in the root `package.json` `pnpm.neverBuiltDependencies` so lang-core's install telemetry never runs. Exact versions: `@openuidev/lang-core` `1.0.1`, `@openuidev/react-lang` `1.0.1`, `zod` `4.3.6`. Import zod as `zod/v4`.
- The fence tag is exactly `openui-lang`. A fence over `16 * 1024` bytes renders as code. `isStreaming` is always `false` (replies arrive whole).
- Props are positional in zod key order. Never reorder keys in `packages/shared/src/ui/library.ts`; add new optional keys at the end.
- `packages/shared` stays framework-neutral: no `next/*`, no `react-native`, no DOM globals. The library file imports only `@openuidev/lang-core` and `zod/v4`.
- No colour literals outside the scanner's exemptions (`src/__tests__/brandTokens.test.ts` fails the build): use `var(--plane-raised)`, `var(--edge)`, `var(--fg-soft)`, `var(--fg-mute)`, `var(--link)`, `var(--alert)`, `var(--live)` and the `text-ui` / `text-caption` classes. The phone uses `ChatPalette` fields only.
- A click posts only visible text, with `mentions: [{ kind: "agent", id: <card author> }]`, `replyToMessageId: <card message id>`, `threadRootMessageId: <the card's own>`. No hidden context, no record mutation.
- `Image` resolves only against the message's own `attachments`; a URL shows the alt text.
- Skill wording: Hermes names (`terminal`, not `exec`; `/app/skills/` paths, which the build swaps). `SKILL.md` is already 12.6k chars against a 10k budget: Task 8 adds at most 900 chars to it; everything longer goes in the reference.
- Dev acceptance is a Chrome run against dev with receipts on the PR (no Playwright). Phone flows are driven in the simulator.
- Each PR: CI green and Codex reviewed before merge. Dev deploys on merge; prod on the owner's train.
- Branch each PR from `origin/main` in its own worktree under `lightbulb.nosync/.claude/worktrees/` (never on another agent's branch, never outside `.nosync`). `feat/generative-ui-spec` carries only the spec and this plan.

## Review Focus

1. **A fence from a human.** A person pastes a ```` ```openui-lang ```` block into the composer. It must render as a plain code block, never a card with live buttons (Task 4 test "a human's fence stays code").
2. **A Choice with no options, or with a hundred.** Zero options renders the question and nothing to press; more than eight renders the first eight and a "+N more" line, never an unbounded column (Task 7 test "options are bounded").
3. **A click while the send is in flight or after the room went read-only.** A second press before the first resolves must not post twice; a card in an archived room has no `onReply` and renders disabled (Task 4 test "one press, one message"; Task 5 test "no reply function means disabled").
4. **A card inside a thread.** The answer must land in the same thread (`threadRootMessageId` copied from the card), not at top level (Task 5 test "thread cards answer in the thread").
5. **A program with a forward reference to a component that never arrives, or an unknown component.** The renderer must draw what parsed and report codes to analytics without content (Task 4 test "a bogus line still renders the rest, and reports").

---

### Task 1: Dependencies and the postinstall guard

**Files:**
- Modify: `package.json` (root, the `pnpm` block at line 15)
- Modify: `packages/shared/package.json`
- Modify: `apps/frontend/package.json`
- Modify: `apps/mobile/package.json`
- Modify: `pnpm-lock.yaml` (by `pnpm install`)

**Interfaces:**
- Produces: `@openuidev/lang-core` importable from `packages/shared`; `@openuidev/react-lang` and `zod` importable from `apps/frontend` and `apps/mobile`.

- [ ] **Step 1: Add the packages**

In `packages/shared/package.json`, under `"dependencies"`:

```json
"dependencies": {
  "@openuidev/lang-core": "1.0.1",
  "swr": "^2.3.8",
  "zod": "4.6.7"
}
```

In `apps/frontend/package.json`, under `"dependencies"` (alphabetical, near `"@lightbulb/shared"`):

```json
"@openuidev/react-lang": "1.0.1",
"zod": "4.6.7",
```

In `apps/mobile/package.json`, under `"dependencies"`:

```json
"@openuidev/react-lang": "1.0.1",
"zod": "4.6.7",
```

- [ ] **Step 2: Block the install scripts**

In the root `package.json`, inside the existing `"pnpm": { ... }` object, add before `"overrides"`:

```json
"neverBuiltDependencies": [
  "@openuidev/lang-core",
  "@openuidev/react-lang",
  "@openuidev/devtools",
  "@openuidev/observability"
],
```

- [ ] **Step 3: Install and check nothing ran**

Run: `pnpm install`
Expected: lockfile updated; the output contains no line mentioning `postinstall` for `@openuidev/*`.

Run: `pnpm why @openuidev/lang-core && pnpm why zod | head -20`
Expected: lang-core resolved under `@lightbulb/shared` and `@openuidev/react-lang`; zod 4.6.7 everywhere (one version).

- [ ] **Step 4: Typecheck still passes**

Run: `pnpm -F @lightbulb/shared typecheck && pnpm -F @lightbulb/frontend exec tsc --noEmit -p tsconfig.json`
Expected: both exit 0.

- [ ] **Step 5: Commit**

```bash
git add package.json pnpm-lock.yaml packages/shared/package.json apps/frontend/package.json apps/mobile/package.json
git commit -m "deps: OpenUI lang-core and react-lang, zod 4, with install scripts blocked"
```

---

### Task 2: The shared component library

**Files:**
- Create: `packages/shared/src/ui/library.ts`
- Test: `packages/shared/src/ui/library.test.ts`
- Modify: `packages/shared/src/index.ts` (append exports)

**Interfaces:**
- Produces: `CARD_COMPONENTS` (readonly array of lang-core `DefinedComponent`s with `component: null`), `CARD_ROOT = "Stack"`, `CARD_FENCE = "openui-lang"`, `CARD_MAX_BYTES = 16384`, `CARD_MAX_OPTIONS = 8`, `cardLibrary` (a lang-core `Library`: `.prompt()`, `.toJSONSchema()`), `type CardName`.

- [ ] **Step 1: Write the failing test**

`packages/shared/src/ui/library.test.ts`:

```ts
import { createParser } from "@openuidev/lang-core";
import { describe, expect, it } from "vitest";
import { CARD_COMPONENTS, CARD_ROOT, cardLibrary } from "./library";

const parse = (program: string) => createParser(cardLibrary.toJSONSchema(), CARD_ROOT).parse(program);

describe("card library", () => {
  it("has the twenty names, Stack first", () => {
    expect(CARD_COMPONENTS.map((c) => c.name)).toEqual([
      "Stack", "Text", "Callout", "Metric", "Table", "Col", "EditableTable", "BarChart", "LineChart", "Steps",
      "Sources", "Image", "Choice", "Buttons", "Button", "FollowUps", "Form", "Input", "Select", "DatePicker",
    ]);
  });

  it("parses a question card and a table card", () => {
    const result = parse([
      'root = Stack([q, t, m])',
      'q = Choice("Which plan first?", ["Starter", "Studio"])',
      't = Table([Col("Plan", ["Starter", "Studio"]), Col("Price", [49, 149])])',
      'm = Metric("MRR", "$4,200", "+12%")',
    ].join("\n"));
    expect(result.meta.errors).toEqual([]);
    expect(result.root?.typeName).toBe("Stack");
    const children = result.root?.props.children as { typeName: string; props: Record<string, unknown> }[];
    expect(children.map((c) => c.typeName)).toEqual(["Choice", "Table", "Metric"]);
    expect(children[0].props).toMatchObject({ question: "Which plan first?", options: ["Starter", "Studio"] });
  });

  it("drops an unknown component and keeps the rest", () => {
    const result = parse('root = Stack([a, b])\na = Text("hi")\nb = Nope("x")');
    expect(result.root?.typeName).toBe("Stack");
    expect(result.meta.errors.map((e) => e.code)).toContain("unknown-component");
  });

  it("every component signature appears in the prompt", () => {
    const prompt = cardLibrary.prompt({});
    for (const c of CARD_COMPONENTS) expect(prompt).toContain(`${c.name}(`);
  });
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `pnpm -F @lightbulb/shared exec vitest run src/ui/library.test.ts`
Expected: FAIL, cannot find module `./library`.

- [ ] **Step 3: Write the library**

`packages/shared/src/ui/library.ts`:

```ts
/**
 * The card vocabulary an agent may write inside a ```openui-lang fence (spec A2).
 *
 * Framework-neutral: names, zod props and descriptions only, with `component: null`.
 * Web and the phone bind renderers to THESE objects (never redeclare props) so the
 * prompt the agent reads, the parser and both renderers agree.
 *
 * KEY ORDER IS THE WIRE FORMAT. OpenUI Lang passes arguments positionally in zod key
 * order, so reordering a key changes what every already-sent card means. Add new
 * optional keys at the END only.
 */
import { createLibrary, defineComponent } from "@openuidev/lang-core";
import { z } from "zod/v4";

export const CARD_FENCE = "openui-lang";
export const CARD_ROOT = "Stack";
/** A fence past this renders as code, never a card. */
export const CARD_MAX_BYTES = 16 * 1024;
/** A Choice shows at most this many options; the rest fold under "+N more". */
export const CARD_MAX_OPTIONS = 8;

export const Text = defineComponent({
  name: "Text", description: "A line or paragraph of prose.",
  props: z.object({ text: z.string(), size: z.enum(["body", "small", "heading"]).optional() }), component: null,
});
export const Callout = defineComponent({
  name: "Callout", description: "A short note that stands out: a caveat, a warning, a win.",
  props: z.object({ text: z.string(), tone: z.enum(["info", "warn", "good"]).optional() }), component: null,
});
export const Metric = defineComponent({
  name: "Metric", description: "One number with its label and an optional change, e.g. Metric(\"MRR\", \"$4,200\", \"+12%\").",
  props: z.object({ label: z.string(), value: z.string(), delta: z.string().optional() }), component: null,
});
export const Col = defineComponent({
  name: "Col", description: "One table column: its header and its cells, top to bottom.",
  props: z.object({ label: z.string(), data: z.array(z.union([z.string(), z.number()])) }), component: null,
});
export const Table = defineComponent({
  name: "Table", description: "A small table for a comparison; one Col per column, all the same length.",
  props: z.object({ columns: z.array(Col.ref) }), component: null,
});
export const EditableTable = defineComponent({
  name: "EditableTable", description: "A table the person edits in place; Submit posts the rows back as their reply.",
  props: z.object({ columns: z.array(z.string()), rows: z.array(z.array(z.string())), submit: z.string().optional() }), component: null,
});
export const BarChart = defineComponent({
  name: "BarChart", description: "A bar chart, one bar per label. Only when the shape of the numbers is the point.",
  props: z.object({ labels: z.array(z.string()), values: z.array(z.number()), title: z.string().optional() }), component: null,
});
export const LineChart = defineComponent({
  name: "LineChart", description: "A line over ordered labels (days, weeks, months).",
  props: z.object({ labels: z.array(z.string()), values: z.array(z.number()), title: z.string().optional() }), component: null,
});
export const Steps = defineComponent({
  name: "Steps", description: "A plan or progress list; each step carries a status.",
  props: z.object({ steps: z.array(z.object({ label: z.string(), status: z.enum(["todo", "doing", "done"]).optional() })) }), component: null,
});
export const Sources = defineComponent({
  name: "Sources", description: "The links a research answer rests on.",
  props: z.object({ sources: z.array(z.object({ title: z.string(), url: z.string() })) }), component: null,
});
export const Image = defineComponent({
  name: "Image", description: "A picture from THIS message's attachments, named by its file name (never a URL).",
  props: z.object({ fileName: z.string(), alt: z.string() }), component: null,
});
export const Choice = defineComponent({
  name: "Choice", description: "A question with options the person presses; the pick is posted as their reply. Not for sign-offs (those are approvals).",
  props: z.object({ question: z.string(), options: z.array(z.string()), multi: z.boolean().optional() }), component: null,
});
export const Button = defineComponent({
  name: "Button", description: "One labelled reply; pressing it posts `reply` (or the label) as the person's message.",
  props: z.object({ label: z.string(), reply: z.string().optional(), variant: z.enum(["primary", "secondary"]).optional() }), component: null,
});
export const Buttons = defineComponent({
  name: "Buttons", description: "A row of Buttons.",
  props: z.object({ buttons: z.array(Button.ref) }), component: null,
});
export const FollowUps = defineComponent({
  name: "FollowUps", description: "Suggested next replies, shown as small chips under the message.",
  props: z.object({ replies: z.array(z.string()) }), component: null,
});
export const Input = defineComponent({
  name: "Input", description: "A text field in a Form.",
  props: z.object({ name: z.string(), label: z.string(), placeholder: z.string().optional() }), component: null,
});
export const Select = defineComponent({
  name: "Select", description: "A pick-one field in a Form.",
  props: z.object({ name: z.string(), label: z.string(), options: z.array(z.string()) }), component: null,
});
export const DatePicker = defineComponent({
  name: "DatePicker", description: "A date field in a Form.",
  props: z.object({ name: z.string(), label: z.string() }), component: null,
});
export const Form = defineComponent({
  name: "Form", description: "Fields and a Submit; submitting posts one `Label: value` line per field as the person's reply.",
  props: z.object({ title: z.string(), fields: z.array(z.union([Input.ref, Select.ref, DatePicker.ref])), submit: z.string().optional() }), component: null,
});
export const Stack = defineComponent({
  name: "Stack", description: "The root of every card: its children, top to bottom.",
  props: z.object({
    children: z.array(z.union([
      Text.ref, Callout.ref, Metric.ref, Table.ref, EditableTable.ref, BarChart.ref, LineChart.ref,
      Steps.ref, Sources.ref, Image.ref, Choice.ref, Buttons.ref, FollowUps.ref, Form.ref,
    ])),
  }),
  component: null,
});

export const CARD_COMPONENTS = [
  Stack, Text, Callout, Metric, Table, Col, EditableTable, BarChart, LineChart, Steps,
  Sources, Image, Choice, Buttons, Button, FollowUps, Form, Input, Select, DatePicker,
] as const;

export type CardName = (typeof CARD_COMPONENTS)[number]["name"];

/** Prompt and parser source. Renderers come from the platform bindings, not from here. */
export const cardLibrary = createLibrary({ root: CARD_ROOT, components: [...CARD_COMPONENTS] });
```

- [ ] **Step 4: Export from the barrel**

Append to `packages/shared/src/index.ts`:

```ts
export {
  CARD_COMPONENTS, CARD_FENCE, CARD_MAX_BYTES, CARD_MAX_OPTIONS, CARD_ROOT, cardLibrary,
} from "./ui/library";
export type { CardName } from "./ui/library";
```

- [ ] **Step 5: Run the test**

Run: `pnpm -F @lightbulb/shared exec vitest run src/ui/library.test.ts`
Expected: PASS (4 tests). If `toJSONSchema` complains about a union of refs, the zod import is wrong: it must be `zod/v4` everywhere, including in `@openuidev/lang-core`'s resolution (one zod version, Task 1 Step 3).

- [ ] **Step 6: Commit**

```bash
git add packages/shared/src/ui/library.ts packages/shared/src/ui/library.test.ts packages/shared/src/index.ts
git commit -m "shared: the twenty-component card library (OpenUI lang-core)"
```

---

### Task 3: Reply text and answered-state helpers

**Files:**
- Create: `packages/shared/src/ui/reply.ts`
- Create: `packages/shared/src/ui/answer.ts`
- Test: `packages/shared/src/ui/reply.test.ts`
- Test: `packages/shared/src/ui/answer.test.ts`
- Modify: `packages/shared/src/index.ts`

**Interfaces:**
- Produces: `choiceReply(picked: readonly string[]): string`, `formReply(fields: readonly { label: string; value: string }[]): string`, `tableReply(columns: readonly string[], rows: readonly (readonly string[])[]): string`, `interface CardReply { text: string; agentId: string; messageId: string; threadRootMessageId: string | null }`, `cardAnswerFor(messages, messageId): string | null`.

- [ ] **Step 1: Write the failing tests**

`packages/shared/src/ui/reply.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { choiceReply, formReply, tableReply } from "./reply";

describe("card replies read as something the person would type", () => {
  it("joins picks with commas and trims", () => {
    expect(choiceReply(["Studio"])).toBe("Studio");
    expect(choiceReply([" Starter ", "Studio"])).toBe("Starter, Studio");
    expect(choiceReply([])).toBe("");
  });
  it("writes one Label: value line per field, skipping empty values", () => {
    expect(formReply([{ label: "Name", value: "Ada" }, { label: "Email", value: "" }, { label: "Start", value: "2026-11-01" }]))
      .toBe("Name: Ada\nStart: 2026-11-01");
  });
  it("writes a markdown table and escapes pipes", () => {
    expect(tableReply(["Plan", "Price"], [["Starter", "49"], ["Pro | Team", "149"]])).toBe(
      "| Plan | Price |\n| --- | --- |\n| Starter | 49 |\n| Pro \\| Team | 149 |",
    );
  });
});
```

`packages/shared/src/ui/answer.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import type { CanonicalRoomMessage } from "../messaging/types";
import { cardAnswerFor } from "./answer";

const msg = (over: Partial<CanonicalRoomMessage>): CanonicalRoomMessage => ({
  messageId: "m", roomId: "r", messageSeq: 1, roomRevision: 1, author: { kind: "agent", id: "a", displayName: "A" },
  text: "", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-10-16T00:00:00Z", runId: null, ...over,
});

describe("cardAnswerFor", () => {
  const card = msg({ messageId: "card" });
  it("is the first human reply to the card", () => {
    const messages = [card,
      msg({ messageId: "x", author: { kind: "agent", id: "b", displayName: "B" }, replyToMessageId: "card", text: "agent noise" }),
      msg({ messageId: "y", author: { kind: "human", id: "u", displayName: "U" }, replyToMessageId: "card", text: "Studio" }),
      msg({ messageId: "z", author: { kind: "human", id: "u", displayName: "U" }, replyToMessageId: "card", text: "Starter" })];
    expect(cardAnswerFor(messages, "card")).toBe("Studio");
  });
  it("ignores deleted replies and other messages", () => {
    const messages = [card, msg({ messageId: "y", author: { kind: "human", id: "u", displayName: "U" }, replyToMessageId: "card", text: "Studio", deletedAt: "2026-10-16T01:00:00Z" })];
    expect(cardAnswerFor(messages, "card")).toBeNull();
    expect(cardAnswerFor(messages, "other")).toBeNull();
  });
});
```

- [ ] **Step 2: Run them to see them fail**

Run: `pnpm -F @lightbulb/shared exec vitest run src/ui/reply.test.ts src/ui/answer.test.ts`
Expected: FAIL, modules not found.

- [ ] **Step 3: Write the helpers**

`packages/shared/src/ui/reply.ts`:

```ts
/** The text a card click posts as the person. Plain words: the agent reads it as any message. */

export interface CardReply {
  text: string;
  /** The card's author: mentioned so the reply wakes it as addressed. */
  agentId: string;
  /** The card's message: `replyToMessageId`, which is also how the card learns it was answered. */
  messageId: string;
  /** The card's own thread, copied so the answer lands where the card is. */
  threadRootMessageId: string | null;
}

export function choiceReply(picked: readonly string[]): string {
  return picked.map((p) => p.trim()).filter(Boolean).join(", ");
}

export function formReply(fields: readonly { label: string; value: string }[]): string {
  return fields
    .map((f) => ({ label: f.label.trim(), value: f.value.trim() }))
    .filter((f) => f.value)
    .map((f) => `${f.label}: ${f.value}`)
    .join("\n");
}

const cell = (value: string) => value.replace(/\|/g, "\\|").replace(/\n/g, " ").trim();

export function tableReply(columns: readonly string[], rows: readonly (readonly string[])[]): string {
  const line = (cells: readonly string[]) => `| ${cells.map(cell).join(" | ")} |`;
  return [line(columns), `| ${columns.map(() => "---").join(" | ")} |`, ...rows.map(line)].join("\n");
}
```

`packages/shared/src/ui/answer.ts`:

```ts
import type { CanonicalRoomMessage } from "../messaging/types";

/**
 * The text of the first human reply to a card, or null while unanswered. Both clients
 * hold the room's messages, so no server field is needed: a click posts with
 * `replyToMessageId` = the card's id (reply.ts), and this reads it back.
 */
export function cardAnswerFor(messages: readonly CanonicalRoomMessage[], messageId: string): string | null {
  const reply = messages.find((m) => m.author.kind === "human" && m.replyToMessageId === messageId && !m.deletedAt);
  return reply ? reply.text : null;
}
```

Append to `packages/shared/src/index.ts`:

```ts
export { choiceReply, formReply, tableReply } from "./ui/reply";
export type { CardReply } from "./ui/reply";
export { cardAnswerFor } from "./ui/answer";
```

- [ ] **Step 4: Run the tests**

Run: `pnpm -F @lightbulb/shared exec vitest run src/ui/`
Expected: PASS (all of `src/ui`).

- [ ] **Step 5: Commit**

```bash
git add packages/shared/src/ui/reply.ts packages/shared/src/ui/answer.ts packages/shared/src/ui/reply.test.ts packages/shared/src/ui/answer.test.ts packages/shared/src/index.ts
git commit -m "shared: card reply text builders and the answered-state reader"
```

---

### Task 4: The web card: context, renderer mount and the markdown hook

**Files:**
- Create: `apps/frontend/src/components/messaging/openui/CardContext.tsx`
- Create: `apps/frontend/src/components/messaging/openui/OpenUICard.tsx`
- Create: `apps/frontend/src/components/messaging/openui/library.tsx`
- Create: `apps/frontend/src/components/messaging/openui/renderers/shared.tsx` (the prop types every renderer uses)
- Create: `apps/frontend/src/components/messaging/openui/renderers/index.tsx` (the name → renderer map; `Stack` and `Text` now, Tasks 6 and 7 fill it)
- Modify: `apps/frontend/src/components/messaging/roomMarkdown.tsx` (add `code` and `pre`)
- Modify: `apps/frontend/src/components/teams/shared/components/MarkdownView.tsx` (export `markdownComponents`)
- Modify: `apps/frontend/src/components/messaging/messaging.css` (card styles)
- Test: `apps/frontend/src/components/messaging/__tests__/openuiCard.test.tsx`

**Interfaces:**
- Consumes: `CARD_COMPONENTS`, `CARD_ROOT`, `CARD_FENCE`, `CARD_MAX_BYTES` (Task 2).
- Produces: `CardContext` / `useCardContext(): CardContextValue | null` / `useCardLive(): boolean` with `interface CardContextValue { messageId: string; roomId: string; authorId: string; threadRootMessageId: string | null; attachments: readonly MessageAttachment[]; answered: string | null; onReply?: (text: string) => Promise<void>; attachmentUrl?: (roomId: string, attachmentId: string) => Promise<{ url: string }> }`; `OpenUICard({ program })`; `webCardLibrary`; `RenderProps<P>` and `SubComponent<P>` (renderers/shared.tsx); `RENDERERS` (renderers/index.tsx), which Tasks 6 and 7 extend.

- [ ] **Step 1: Export the default markdown components**

In `MarkdownView.tsx`, find the line that starts `const markdownComponents: Partial<Components> = {` and change it to `export const markdownComponents: Partial<Components> = {`.

- [ ] **Step 2: Write the failing test**

`apps/frontend/src/components/messaging/__tests__/openuiCard.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { CardContext, type CardContextValue } from "../openui/CardContext";
import { roomMarkdown } from "../roomMarkdown";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
import { capture } from "@/lib/analytics";

const ctx = (over: Partial<CardContextValue> = {}): CardContextValue => ({
  messageId: "card", roomId: "room", authorId: "agent-1", threadRootMessageId: null, attachments: [], answered: null, ...over,
});
const fence = (body: string) => "```openui-lang\n" + body + "\n```";

it("renders a Text card from an agent's fence, not a code block", () => {
  render(<CardContext.Provider value={ctx()}><MarkdownView components={roomMarkdown}>{fence('root = Stack([t])\nt = Text("Hello there")')}</MarkdownView></CardContext.Provider>);
  expect(screen.getByText("Hello there").closest("pre")).toBeNull();
  expect(screen.getByText("Hello there").closest(".openui-card")).not.toBeNull();
});

it("a human's fence stays code", () => {
  // No CardContext: the message is not an agent's. The fence must render as code, never live UI.
  render(<MarkdownView components={roomMarkdown}>{fence('root = Stack([t])\nt = Text("Hello there")')}</MarkdownView>);
  expect(screen.getByText(/root = Stack/).closest("pre")).not.toBeNull();
});

it("a bogus line still renders the rest, and reports", () => {
  render(<CardContext.Provider value={ctx()}><MarkdownView components={roomMarkdown}>{fence('root = Stack([t, n])\nt = Text("Kept")\nn = Nope("x")')}</MarkdownView></CardContext.Provider>);
  expect(screen.getByText("Kept")).toBeInTheDocument();
  expect(capture).toHaveBeenCalledWith("openui.parse_error", expect.objectContaining({ codes: expect.arrayContaining(["unknown-component"]) }));
});

it("a fence past 16 KB renders as code", () => {
  const big = 'root = Stack([t])\nt = Text("' + "x".repeat(17 * 1024) + '")';
  render(<CardContext.Provider value={ctx()}><MarkdownView components={roomMarkdown}>{fence(big)}</MarkdownView></CardContext.Provider>);
  expect(document.querySelector(".openui-card")).toBeNull();
  expect(document.querySelector("pre")).not.toBeNull();
});

it("other fences are untouched", () => {
  render(<CardContext.Provider value={ctx()}><MarkdownView components={roomMarkdown}>{"```js\nconsole.log(1)\n```"}</MarkdownView></CardContext.Provider>);
  expect(screen.getByText("console.log(1)").closest("pre")).not.toBeNull();
});
```

- [ ] **Step 3: Run it to see it fail**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__/openuiCard.test.tsx`
Expected: FAIL, `../openui/CardContext` not found.

- [ ] **Step 4: Write the context**

`apps/frontend/src/components/messaging/openui/CardContext.tsx`:

```tsx
"use client";

import { createContext, useContext } from "react";
import type { MessageAttachment } from "@lightbulb/shared";

/**
 * What a card needs from the message it sits in. Provided by RoomMessageItem for an
 * AGENT's message only: without a provider a fence is plain code (a person's paste can
 * never become live buttons), and a card shown outside a room (a session transcript, a
 * document) renders read-only.
 */
export interface CardContextValue {
  messageId: string;
  roomId: string;
  authorId: string;
  threadRootMessageId: string | null;
  attachments: readonly MessageAttachment[];
  /** The person's answer once one exists: controls disable and show it. */
  answered: string | null;
  /** Posts `text` as the person in reply to this card. Absent in a read-only place. */
  onReply?: (text: string) => Promise<void>;
  attachmentUrl?: (roomId: string, attachmentId: string) => Promise<{ url: string }>;
}

export const CardContext = createContext<CardContextValue | null>(null);

export function useCardContext(): CardContextValue | null {
  return useContext(CardContext);
}

/** True when a control may post: there is a reply path and nobody answered yet. */
export function useCardLive(): boolean {
  const ctx = useCardContext();
  return !!ctx?.onReply && ctx.answered === null;
}
```

- [ ] **Step 5: Write the shared renderer types, the first renderers and the binding**

`apps/frontend/src/components/messaging/openui/renderers/shared.tsx` (types only now; Task 5 adds `useCardPress` here):

```tsx
import type { ReactNode } from "react";

/** Props every renderer receives from OpenUI: the parsed props plus `renderNode` for children. */
export interface RenderProps<P = Record<string, unknown>> {
  props: P;
  renderNode: (value: unknown) => ReactNode;
}

/** A parsed child component as the parent sees it. */
export interface SubComponent<P = Record<string, unknown>> {
  type: "element";
  typeName: string;
  props: P;
  partial: boolean;
}
```

`apps/frontend/src/components/messaging/openui/renderers/index.tsx`:

```tsx
import type { ComponentType } from "react";
import type { CardName } from "@lightbulb/shared";
import type { RenderProps } from "./shared";

export type { RenderProps, SubComponent } from "./shared";

function TextView({ props }: RenderProps<{ text: string; size?: "body" | "small" | "heading" }>) {
  const cls = props.size === "heading" ? "text-base font-semibold" : props.size === "small" ? "text-caption text-muted-foreground" : "text-ui";
  return <p className={cls}>{props.text}</p>;
}

function StackView({ props, renderNode }: RenderProps<{ children: unknown }>) {
  return <div className="openui-stack">{renderNode(props.children)}</div>;
}

// Tasks 6 and 7 add the rest; until then an unbound name renders nothing (the parser
// still validates it, so the agent's prompt is unaffected).
export const RENDERERS: Partial<Record<CardName, ComponentType<RenderProps<never>>>> = {
  Stack: StackView as ComponentType<RenderProps<never>>,
  Text: TextView as ComponentType<RenderProps<never>>,
};
```

`apps/frontend/src/components/messaging/openui/library.tsx`:

```tsx
"use client";

import { createLibrary, defineComponent } from "@openuidev/react-lang";
import { CARD_COMPONENTS, CARD_ROOT, type CardName } from "@lightbulb/shared";
import { RENDERERS } from "./renderers";

const Empty = () => null;

/** The shared vocabulary bound to this app's renderers. Props come from shared, never redeclared. */
export const webCardLibrary = createLibrary({
  root: CARD_ROOT,
  components: CARD_COMPONENTS.map((c) =>
    defineComponent({
      name: c.name,
      description: c.description,
      props: c.props,
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- OpenUI types the renderer by each schema; the map is checked by name in renderers/index.tsx
      component: (RENDERERS[c.name as CardName] ?? Empty) as any,
    }),
  ),
});
```

- [ ] **Step 6: Write the card mount**

`apps/frontend/src/components/messaging/openui/OpenUICard.tsx`:

```tsx
"use client";

import { useCallback, useMemo } from "react";
import { Renderer, type ActionEvent, type OpenUIError } from "@openuidev/react-lang";
import { CARD_MAX_BYTES } from "@lightbulb/shared";
import { capture } from "@/lib/analytics";
import { useCardContext } from "./CardContext";
import { webCardLibrary } from "./library";

const bytes = (s: string) => new TextEncoder().encode(s).length;

/**
 * One ```openui-lang fence from an agent's reply, drawn with the shared vocabulary.
 * Replies arrive whole, so nothing streams. A control's click reaches `onAction` as a
 * `continue_conversation` whose message is the text to post; the context posts it.
 */
export function OpenUICard({ program }: { program: string }) {
  const ctx = useCardContext();
  const tooBig = useMemo(() => bytes(program) > CARD_MAX_BYTES, [program]);
  const onAction = useCallback((event: ActionEvent) => {
    if (event.type !== "continue_conversation" || !ctx?.onReply) return;
    const text = event.humanFriendlyMessage.trim();
    if (text) void ctx.onReply(text);
  }, [ctx]);
  const onError = useCallback((errors: OpenUIError[]) => {
    // Codes only, never the program: this is the one reliability signal the spec keeps.
    if (errors.length) capture("openui.parse_error", { codes: errors.map((e) => e.code), count: errors.length });
  }, []);
  if (tooBig) return <pre className="rounded-sm p-2 text-caption font-mono overflow-x-auto my-2" style={{ background: "var(--edge)" }}>{program}</pre>;
  return (
    <div className="openui-card" data-answered={ctx?.answered !== null && ctx?.answered !== undefined ? "" : undefined}>
      <Renderer response={program} library={webCardLibrary} isStreaming={false} onAction={onAction} onError={onError} />
    </div>
  );
}
```

- [ ] **Step 7: Hook the fence into the room markdown**

In `apps/frontend/src/components/messaging/roomMarkdown.tsx`, add imports and two overrides:

```tsx
import type { ReactNode } from "react";
import type { Components } from "react-markdown";
import { CARD_FENCE } from "@lightbulb/shared";
import { markdownComponents } from "@/components/teams/shared/components/MarkdownView";
import { OpenUICard } from "./openui/OpenUICard";
import { useCardContext } from "./openui/CardContext";
// (keep the existing imports)

const CARD_CLASS = `language-${CARD_FENCE}`;
const DefaultCode = markdownComponents.code!;
const DefaultPre = markdownComponents.pre!;

/** An agent's card fence. Without a CardContext (a person's message, a transcript) it stays code. */
function CodeOrCard(props: React.ComponentProps<typeof DefaultCode>) {
  const ctx = useCardContext();
  const { className, children } = props as { className?: string; children?: ReactNode };
  if (ctx && typeof className === "string" && className.split(" ").includes(CARD_CLASS)) {
    return <OpenUICard program={String(children ?? "").replace(/\n$/, "")} />;
  }
  return <DefaultCode {...props} />;
}

/** react-markdown wraps block code in <pre>; a card must not inherit the code box. */
function PreOrCard(props: React.ComponentProps<typeof DefaultPre>) {
  const ctx = useCardContext();
  const node = (props as { node?: { children?: { type: string; properties?: { className?: unknown } }[] } }).node;
  const first = node?.children?.find((c) => c.type === "element");
  const cls = Array.isArray(first?.properties?.className) ? (first!.properties!.className as unknown[]).join(" ") : "";
  if (ctx && cls.split(" ").includes(CARD_CLASS)) return <>{(props as { children?: ReactNode }).children}</>;
  return <DefaultPre {...props} />;
}
```

and inside the exported `roomMarkdown` object add, after `blockquote`:

```tsx
  code: CodeOrCard,
  pre: PreOrCard,
```

- [ ] **Step 8: Styles**

Append to `apps/frontend/src/components/messaging/messaging.css`:

```css
/* Agent cards (an ```openui-lang fence). Tokens only: the brand scanner rejects literals. */
.openui-card { margin-top: 0.5rem; border: 1px solid var(--edge); border-radius: 0.5rem; background: var(--plane-raised); padding: 0.75rem; max-width: 40rem; }
.openui-stack { display: grid; gap: 0.625rem; }
.openui-card[data-answered] .openui-live { opacity: 0.7; }
.openui-answer { color: var(--fg-soft); }
```

- [ ] **Step 9: Run the test**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__/openuiCard.test.tsx`
Expected: PASS (5 tests). If `node.children` is undefined in `PreOrCard`, react-markdown 10 passes `node` only when `components` entries are functions receiving the full props; keep `{...props}` spreads and do not destructure `node` away.

- [ ] **Step 10: Lint and the brand scanner**

Run: `pnpm -F @lightbulb/frontend lint && (cd apps/frontend && node scripts/brand-tokens-scan.mjs)`
Expected: clean.

- [ ] **Step 11: Commit**

```bash
git add apps/frontend/src/components/messaging/openui apps/frontend/src/components/messaging/roomMarkdown.tsx apps/frontend/src/components/teams/shared/components/MarkdownView.tsx apps/frontend/src/components/messaging/messaging.css apps/frontend/src/components/messaging/__tests__/openuiCard.test.tsx
git commit -m "web: render an agent's openui-lang fence as a card"
```

---

### Task 5: Click to reply and the answered state, wired through the room

**Files:**
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx` (props at line 60–124; the `<RoomMessageItem` call at line 645)
- Modify: `apps/frontend/src/components/messaging/RoomMessageItem.tsx` (props; wrap the `MarkdownView` at line 109)
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx` (the `roomProps` object at line 870)
- Test: `apps/frontend/src/components/messaging/__tests__/openuiReply.test.tsx`

**Interfaces:**
- Consumes: `CardReply`, `cardAnswerFor` (Task 3); `CardContext` (Task 4).
- Produces: `RoomPanelProps.onCardReply?: (reply: CardReply) => Promise<void>`; `RoomMessageItemProps.onCardReply?: (reply: CardReply) => Promise<void>` and `RoomMessageItemProps.cardAnswer?: string | null`.

- [ ] **Step 1: Write the failing test**

`apps/frontend/src/components/messaging/__tests__/openuiReply.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { CanonicalRoomMessage } from "@lightbulb/shared";
import { defaultUrlTransform } from "react-markdown";
import { RoomMessageItem } from "../RoomMessageItem";
import { roomMarkdown } from "../roomMarkdown";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

const program = "```openui-lang\nroot = Stack([b])\nb = Buttons([Button(\"Studio\"), Button(\"Starter\")])\n```";
const agentMessage = (over: Partial<CanonicalRoomMessage> = {}): CanonicalRoomMessage => ({
  messageId: "card", roomId: "room", messageSeq: 3, roomRevision: 1, author: { kind: "agent", id: "agent-1", displayName: "Friday" },
  text: program, mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-10-16T10:00:00Z", runId: null, ...over,
});
const itemProps = (message: CanonicalRoomMessage, extra: Record<string, unknown> = {}) => ({
  id: "m-card", message, text: message.text, currentUserId: "user-1", actionable: true, inThread: false, collapsed: false,
  replyCount: 0, highlighted: false, mentionsViewer: false, markdown: roomMarkdown, urlTransform: defaultUrlTransform,
  quickReactions: [], reactionPending: false, reactionError: false, ...extra,
});

it("a press posts the label as the person, mentioning the agent, in reply to the card", async () => {
  const onCardReply = vi.fn().mockResolvedValue(undefined);
  render(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: null })} />);
  await userEvent.click(screen.getByRole("button", { name: "Studio" }));
  expect(onCardReply).toHaveBeenCalledWith({ text: "Studio", agentId: "agent-1", messageId: "card", threadRootMessageId: null });
});

it("thread cards answer in the thread", async () => {
  const onCardReply = vi.fn().mockResolvedValue(undefined);
  render(<RoomMessageItem {...itemProps(agentMessage({ threadRootMessageId: "root-7" }), { onCardReply, cardAnswer: null, inThread: true })} />);
  await userEvent.click(screen.getByRole("button", { name: "Starter" }));
  expect(onCardReply).toHaveBeenCalledWith(expect.objectContaining({ threadRootMessageId: "root-7", messageId: "card" }));
});

it("one press, one message", async () => {
  let resolve!: () => void;
  const onCardReply = vi.fn(() => new Promise<void>((r) => { resolve = r; }));
  render(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply, cardAnswer: null })} />);
  const button = screen.getByRole("button", { name: "Studio" });
  await userEvent.click(button);
  await userEvent.click(button);
  expect(onCardReply).toHaveBeenCalledTimes(1);
  resolve();
});

it("an answered card shows the pick and disables its controls", () => {
  render(<RoomMessageItem {...itemProps(agentMessage(), { onCardReply: vi.fn(), cardAnswer: "Studio" })} />);
  expect(screen.getByRole("button", { name: "Studio" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Studio" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "Starter" })).toBeDisabled();
});

it("no reply function means disabled", () => {
  render(<RoomMessageItem {...itemProps(agentMessage(), { cardAnswer: null })} />);
  expect(screen.getByRole("button", { name: "Studio" })).toBeDisabled();
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__/openuiReply.test.tsx`
Expected: FAIL: no button named "Studio" (Buttons has no renderer yet) and `onCardReply` is not a prop.

- [ ] **Step 3: The press hook and a minimal Buttons renderer so the wiring is testable**

Append to `apps/frontend/src/components/messaging/openui/renderers/shared.tsx`:

```tsx
import { useState } from "react";
import { useTriggerAction } from "@openuidev/react-lang";
import { useCardContext, useCardLive } from "../CardContext";

/** A press posts once: the button locks while its reply is in flight or once the card is answered. */
export function useCardPress() {
  const ctx = useCardContext();
  const live = useCardLive();
  const trigger = useTriggerAction();
  const [busy, setBusy] = useState(false);
  const press = (text: string) => {
    if (!live || busy) return;
    setBusy(true);
    trigger(text);
  };
  return { press, disabled: !live || busy, answered: ctx?.answered ?? null };
}
```

Then in `renderers/index.tsx` add (Task 7 moves this into `ask.tsx` with the full asking set; keep the name):

```tsx
import { Button as UiButton } from "@/components/ui/button";
import { useCardPress, type SubComponent } from "./shared";

function ButtonsView({ props }: RenderProps<{ buttons: SubComponent<{ label: string; reply?: string; variant?: "primary" | "secondary" }>[] }>) {
  const { press, disabled, answered } = useCardPress();
  return (
    <div className="flex flex-wrap gap-2 openui-live" role="group">
      {props.buttons.map((b, i) => {
        const text = (b.props.reply ?? b.props.label).trim();
        const picked = answered !== null && answered === text;
        return <UiButton key={`${b.props.label}-${i}`} type="button" size="sm" variant={b.props.variant === "secondary" ? "outline" : "default"}
          disabled={disabled} aria-pressed={picked} onClick={() => press(text)}>{b.props.label}</UiButton>;
      })}
    </div>
  );
}
```

and register it: `Buttons: ButtonsView as ComponentType<RenderProps<never>>,` in `RENDERERS`. `useTriggerAction()(text)` with no action prop sends `continue_conversation` with `text` as the message, which `OpenUICard.onAction` posts.

- [ ] **Step 4: Thread the props**

`RoomMessageItem.tsx`: add to `RoomMessageItemProps`:

```ts
  /** Posts a card's answer as the viewer (absent in read-only places: the controls disable). */
  onCardReply?: (reply: CardReply) => Promise<void>;
  /** The first human reply to this message, when it carries a card. */
  cardAnswer?: string | null;
```

add imports `import { type CardReply } from "@lightbulb/shared";` and `import { CardContext, type CardContextValue } from "./openui/CardContext";` and `import { useMemo } from "react";`, add `onCardReply, cardAnswer = null` to the destructured props, then replace the `<MarkdownView skipHtml components={markdown} urlTransform={urlTransform}>{text}</MarkdownView>` line with:

```tsx
        <CardContext.Provider value={cardContext}>
          <MarkdownView skipHtml components={markdown} urlTransform={urlTransform}>{text}</MarkdownView>
        </CardContext.Provider>
```

and above the `return`, build the value (agent messages only; a person's fence never gets a provider):

```tsx
  const cardContext = useMemo<CardContextValue | null>(() => message.author.kind !== "agent" || message.deletedAt ? null : {
    messageId: message.messageId, roomId: message.roomId, authorId: message.author.id, threadRootMessageId: message.threadRootMessageId,
    attachments: message.attachments ?? [], answered: cardAnswer,
    onReply: onCardReply && actionable ? (replyText: string) => onCardReply({ text: replyText, agentId: message.author.id, messageId: message.messageId, threadRootMessageId: message.threadRootMessageId }) : undefined,
    attachmentUrl: onAttachmentUrl,
  }, [message, cardAnswer, onCardReply, actionable, onAttachmentUrl]);
```

`RoomPanel.tsx`: add to `RoomPanelProps` (after `onAttachmentUrl`):

```ts
  /** A card's click: posts the person's answer in reply to the card (MessagingWorkspaceContext builds it). */
  onCardReply?: (reply: CardReply) => Promise<void>;
```

import `cardAnswerFor` and `CardReply` from `@lightbulb/shared`, destructure `onCardReply` where the other handlers are, and at the `<RoomMessageItem` call (line 645) add:

```tsx
      onCardReply={onCardReply} cardAnswer={cardAnswerFor(messages, message.messageId)}
```

`MessagingWorkspaceContext.tsx`: in the `roomProps` object (line 870 block), add:

```ts
    onCardReply: async (reply: CardReply) => {
      await sendRoomMessage({
        clientMessageId: crypto.randomUUID(),
        text: reply.text,
        mentions: [{ kind: "agent", id: reply.agentId }],
        replyToMessageId: reply.messageId,
        threadRootMessageId: reply.threadRootMessageId,
      });
    },
```

with `import type { CardReply } from "@lightbulb/shared";` added to that file's shared import.

- [ ] **Step 5: Run the test**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__/openuiReply.test.tsx src/components/messaging/__tests__/openuiCard.test.tsx`
Expected: PASS (10 tests).

- [ ] **Step 6: Typecheck, lint, the existing room tests**

Run: `pnpm -F @lightbulb/frontend exec tsc --noEmit -p tsconfig.json && pnpm -F @lightbulb/frontend lint && pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/messaging
git commit -m "web: a card click posts the person's answer in reply to the card, agent mentioned"
```

---

### Task 6: Web renderers: prose, showing and charts

**Files:**
- Create: `apps/frontend/src/components/messaging/openui/renderers/show.tsx`
- Create: `apps/frontend/src/components/messaging/openui/renderers/charts.tsx`
- Modify: `apps/frontend/src/components/messaging/openui/renderers/index.tsx` (register)
- Test: `apps/frontend/src/components/messaging/__tests__/openuiShow.test.tsx`

**Interfaces:**
- Consumes: `RenderProps`, `SubComponent`, `useCardContext` (Task 4), `useCardPress` (Task 5), `tableReply` (Task 3).
- Produces: renderers for `Callout`, `Metric`, `Table`, `Col`, `EditableTable`, `Steps`, `Sources`, `Image`, `BarChart`, `LineChart`.

- [ ] **Step 1: Write the failing test**

`apps/frontend/src/components/messaging/__tests__/openuiShow.test.tsx`:

```tsx
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { CardContext } from "../openui/CardContext";
import { OpenUICard } from "../openui/OpenUICard";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
const ctx = (over = {}) => ({ messageId: "card", roomId: "room", authorId: "a", threadRootMessageId: null, attachments: [], answered: null, ...over });

it("Metric, Callout, Steps and Sources read as data, not prose", () => {
  render(<CardContext.Provider value={ctx()}><OpenUICard program={[
    'root = Stack([m, c, s, src])',
    'm = Metric("MRR", "$4,200", "+12%")',
    'c = Callout("Trial ends Friday", "warn")',
    's = Steps([{label: "Draft", status: "done"}, {label: "Review", status: "doing"}, {label: "Send"}])',
    'src = Sources([{title: "Stripe docs", url: "https://stripe.com/docs"}, {title: "bad", url: "javascript:alert(1)"}])',
  ].join("\n")} /></CardContext.Provider>);
  expect(screen.getByText("$4,200")).toBeInTheDocument();
  expect(screen.getByText("+12%")).toBeInTheDocument();
  expect(screen.getByRole("note")).toHaveTextContent("Trial ends Friday");
  const steps = screen.getByRole("list", { name: "Steps" });
  expect(within(steps).getAllByRole("listitem").map((li) => li.getAttribute("data-status"))).toEqual(["done", "doing", "todo"]);
  expect(screen.getByRole("link", { name: "Stripe docs" })).toHaveAttribute("href", "https://stripe.com/docs");
  expect(screen.queryByRole("link", { name: "bad" })).toBeNull();
});

it("Table draws columns as rows", () => {
  render(<CardContext.Provider value={ctx()}><OpenUICard program={'root = Stack([t])\nt = Table([Col("Plan", ["Starter", "Studio"]), Col("Price", [49, 149])])'} /></CardContext.Provider>);
  const rows = screen.getAllByRole("row");
  expect(rows).toHaveLength(3);
  expect(within(rows[0]).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Plan", "Price"]);
  expect(within(rows[2]).getAllByRole("cell").map((c) => c.textContent)).toEqual(["Studio", "149"]);
});

it("EditableTable submits the edited rows as a markdown table", async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  render(<CardContext.Provider value={ctx({ onReply })}><OpenUICard program={'root = Stack([t])\nt = EditableTable(["Plan", "Price"], [["Starter", "49"]], "Send prices")'} /></CardContext.Provider>);
  const cell = screen.getByRole("textbox", { name: "Price, row 1" });
  await userEvent.clear(cell);
  await userEvent.type(cell, "59");
  await userEvent.click(screen.getByRole("button", { name: "Send prices" }));
  expect(onReply).toHaveBeenCalledWith("| Plan | Price |\n| --- | --- |\n| Starter | 59 |");
});

it("Image shows only the message's own attachment", async () => {
  const attachmentUrl = vi.fn().mockResolvedValue({ url: "blob:one" });
  render(<CardContext.Provider value={ctx({ attachments: [{ id: "att-1", fileName: "chart.png", contentType: "image/png", size: 10 }], attachmentUrl })}>
    <OpenUICard program={'root = Stack([a, b])\na = Image("chart.png", "Q3 revenue")\nb = Image("https://evil.example/x.png", "Remote")'} /></CardContext.Provider>);
  expect(await screen.findByRole("img", { name: "Q3 revenue" })).toHaveAttribute("src", "blob:one");
  expect(screen.queryByRole("img", { name: "Remote" })).toBeNull();
  expect(screen.getByText("Remote")).toBeInTheDocument();
});

it("charts carry a title and an accessible summary", () => {
  render(<CardContext.Provider value={ctx()}><OpenUICard program={'root = Stack([c])\nc = BarChart(["Jan", "Feb"], [10, 20], "Signups")'} /></CardContext.Provider>);
  expect(screen.getByRole("figure", { name: "Signups" })).toBeInTheDocument();
  expect(screen.getByText("Jan 10, Feb 20")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__/openuiShow.test.tsx`
Expected: FAIL (no renderers for these names).

- [ ] **Step 3: Write the showing renderers**

`apps/frontend/src/components/messaging/openui/renderers/show.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import { tableReply } from "@lightbulb/shared";
import { Button as UiButton } from "@/components/ui/button";
import { Input as UiInput } from "@/components/ui/input";
import { Table as UiTable, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { useCardContext } from "../CardContext";
import { useCardPress } from "./shared";
import type { RenderProps, SubComponent } from "./shared";

export function CalloutView({ props }: RenderProps<{ text: string; tone?: "info" | "warn" | "good" }>) {
  const color = props.tone === "warn" ? "var(--alert)" : props.tone === "good" ? "var(--live)" : "var(--link)";
  return <p role="note" className="text-ui rounded-sm px-3 py-2" style={{ borderLeft: `3px solid ${color}`, background: "var(--plane-pane)" }}>{props.text}</p>;
}

export function MetricView({ props }: RenderProps<{ label: string; value: string; delta?: string }>) {
  return (
    <div className="flex flex-col">
      <span className="text-caption text-muted-foreground">{props.label}</span>
      <span className="text-xl font-semibold">{props.value}</span>
      {props.delta && <span className="text-caption" style={{ color: "var(--fg-soft)" }}>{props.delta}</span>}
    </div>
  );
}

type ColProps = { label: string; data: (string | number)[] };

export function TableView({ props }: RenderProps<{ columns: SubComponent<ColProps>[] }>) {
  const cols = props.columns.map((c) => c.props);
  const rows = Math.max(0, ...cols.map((c) => c.data.length));
  return (
    <UiTable>
      <TableHeader><TableRow>{cols.map((c, i) => <TableHead key={i}>{c.label}</TableHead>)}</TableRow></TableHeader>
      <TableBody>
        {Array.from({ length: rows }, (_, r) => (
          <TableRow key={r}>{cols.map((c, i) => <TableCell key={i}>{c.data[r] === undefined ? "" : String(c.data[r])}</TableCell>)}</TableRow>
        ))}
      </TableBody>
    </UiTable>
  );
}

/** Col never renders on its own: Table reads its props. */
export function ColView() { return null; }

export function EditableTableView({ props }: RenderProps<{ columns: string[]; rows: string[][]; submit?: string }>) {
  const { press, disabled, answered } = useCardPress();
  const [rows, setRows] = useState<string[][]>(() => props.rows.map((r) => props.columns.map((_, i) => r[i] ?? "")));
  const edit = (r: number, c: number, value: string) => setRows((cur) => cur.map((row, ri) => ri === r ? row.map((cell, ci) => ci === c ? value : cell) : row));
  return (
    <form className="grid gap-2 openui-live" onSubmit={(e) => { e.preventDefault(); press(tableReply(props.columns, rows)); }}>
      <UiTable>
        <TableHeader><TableRow>{props.columns.map((c) => <TableHead key={c}>{c}</TableHead>)}</TableRow></TableHeader>
        <TableBody>
          {rows.map((row, r) => <TableRow key={r}>{row.map((cell, c) => <TableCell key={c}>
            <UiInput aria-label={`${props.columns[c]}, row ${r + 1}`} value={cell} disabled={disabled} onChange={(e) => edit(r, c, e.target.value)} />
          </TableCell>)}</TableRow>)}
        </TableBody>
      </UiTable>
      {answered === null
        ? <UiButton type="submit" size="sm" disabled={disabled} className="justify-self-start">{props.submit ?? "Submit"}</UiButton>
        : <p className="text-caption openui-answer">Sent.</p>}
    </form>
  );
}

export function StepsView({ props }: RenderProps<{ steps: { label: string; status?: "todo" | "doing" | "done" }[] }>) {
  return (
    <ol className="grid gap-1 text-ui" aria-label="Steps">
      {props.steps.map((s, i) => {
        const status = s.status ?? "todo";
        return <li key={i} data-status={status} className="flex items-center gap-2">
          <span aria-hidden="true" className="inline-block size-2 rounded-full" style={{ background: status === "done" ? "var(--live)" : status === "doing" ? "var(--link)" : "var(--edge)" }} />
          <span className={status === "done" ? "text-muted-foreground line-through" : undefined}>{s.label}</span>
          <span className="sr-only">{status}</span>
        </li>;
      })}
    </ol>
  );
}

export function SourcesView({ props }: RenderProps<{ sources: { title: string; url: string }[] }>) {
  return (
    <ul className="grid gap-1 text-caption" aria-label="Sources">
      {props.sources.map((s, i) => {
        const href = safeHref(s.url);
        return <li key={i}>{href
          ? <a href={href} target="_blank" rel="noopener noreferrer" className="text-info underline underline-offset-4">{s.title}</a>
          : <span className="text-muted-foreground">{s.title}</span>}</li>;
      })}
    </ul>
  );
}

/** Only this message's own attachment, by file name. Remote images never load in a room. */
export function ImageView({ props }: RenderProps<{ fileName: string; alt: string }>) {
  const ctx = useCardContext();
  const attachment = ctx?.attachments.find((a) => a.fileName === props.fileName && a.contentType.startsWith("image/"));
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    if (attachment && ctx?.attachmentUrl) void ctx.attachmentUrl(ctx.roomId, attachment.id).then((r) => { if (live) setUrl(r.url); }).catch(() => {});
    return () => { live = false; };
  }, [attachment, ctx]);
  if (!attachment || !url) return <span className="text-muted-foreground text-caption">{props.alt}</span>;
  // eslint-disable-next-line @next/next/no-img-element -- a presigned attachment URL, not a page asset
  return <img src={url} alt={props.alt} className="max-w-full rounded-sm" />;
}
```

- [ ] **Step 4: Write the charts**

`apps/frontend/src/components/messaging/openui/renderers/charts.tsx`:

```tsx
"use client";

import { Bar, BarChart as ReBar, CartesianGrid, Line, LineChart as ReLine, XAxis, YAxis } from "recharts";
import { ChartContainer, ChartTooltip, ChartTooltipContent, type ChartConfig } from "@/components/ui/chart";
import type { RenderProps } from "./shared";

type SeriesProps = { labels: string[]; values: number[]; title?: string };

const config: ChartConfig = { value: { label: "Value", color: "var(--link)" } };
const data = (p: SeriesProps) => p.labels.map((label, i) => ({ label, value: p.values[i] ?? 0 }));
const summary = (p: SeriesProps) => p.labels.map((l, i) => `${l} ${p.values[i] ?? 0}`).join(", ");

function Figure({ p, children }: { p: SeriesProps; children: React.ReactNode }) {
  return (
    <figure aria-label={p.title ?? "Chart"} className="grid gap-1">
      {p.title && <figcaption className="text-caption text-muted-foreground">{p.title}</figcaption>}
      <ChartContainer config={config} className="h-40 w-full">{children as React.ReactElement}</ChartContainer>
      <p className="sr-only">{summary(p)}</p>
    </figure>
  );
}

export function BarChartView({ props }: RenderProps<SeriesProps>) {
  return (
    <Figure p={props}>
      <ReBar data={data(props)}>
        <CartesianGrid vertical={false} stroke="var(--edge)" />
        <XAxis dataKey="label" tickLine={false} axisLine={false} />
        <YAxis width={36} tickLine={false} axisLine={false} />
        <ChartTooltip content={<ChartTooltipContent />} />
        <Bar dataKey="value" fill="var(--color-value)" radius={2} />
      </ReBar>
    </Figure>
  );
}

export function LineChartView({ props }: RenderProps<SeriesProps>) {
  return (
    <Figure p={props}>
      <ReLine data={data(props)}>
        <CartesianGrid vertical={false} stroke="var(--edge)" />
        <XAxis dataKey="label" tickLine={false} axisLine={false} />
        <YAxis width={36} tickLine={false} axisLine={false} />
        <ChartTooltip content={<ChartTooltipContent />} />
        <Line type="monotone" dataKey="value" stroke="var(--color-value)" dot={false} strokeWidth={2} />
      </ReLine>
    </Figure>
  );
}
```

If `ChartTooltipContent` is not exported by `components/ui/chart.tsx` (check its `export {` block at line 367), drop the two `ChartTooltip` lines; the tooltip is a nicety.

- [ ] **Step 5: Register them**

In `renderers/index.tsx`, import and add to `RENDERERS`:

```tsx
import { CalloutView, ColView, EditableTableView, ImageView, MetricView, SourcesView, StepsView, TableView } from "./show";
import { BarChartView, LineChartView } from "./charts";
// ...
  Callout: CalloutView, Metric: MetricView, Table: TableView, Col: ColView, EditableTable: EditableTableView,
  Steps: StepsView, Sources: SourcesView, Image: ImageView, BarChart: BarChartView, LineChart: LineChartView,
```

(each cast `as ComponentType<RenderProps<never>>` like the existing entries). `show.tsx` and `charts.tsx` import only from `./shared`, never from `./index`, so the registry has no import cycle.

- [ ] **Step 6: Run the test**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__/openuiShow.test.tsx`
Expected: PASS (5 tests). The chart's `sr-only` summary is what the last test reads; recharts draws nothing measurable in jsdom and that is fine.

- [ ] **Step 7: Lint, scanner, commit**

Run: `pnpm -F @lightbulb/frontend lint && (cd apps/frontend && node scripts/brand-tokens-scan.mjs)`

```bash
git add apps/frontend/src/components/messaging/openui apps/frontend/src/components/messaging/__tests__/openuiShow.test.tsx
git commit -m "web: card renderers for metrics, tables, steps, sources, images and charts"
```

---

### Task 7: Web renderers: asking (Choice, FollowUps, Form)

**Files:**
- Create: `apps/frontend/src/components/messaging/openui/renderers/ask.tsx`
- Modify: `apps/frontend/src/components/messaging/openui/renderers/index.tsx` (move `ButtonsView` here; register)
- Test: `apps/frontend/src/components/messaging/__tests__/openuiAsk.test.tsx`

**Interfaces:**
- Consumes: `useCardPress`, `RenderProps`, `SubComponent` (`renderers/shared.tsx`, Tasks 4–5), `choiceReply`, `formReply` (Task 3), `CARD_MAX_OPTIONS` (Task 2).
- Produces: renderers for `Choice`, `Buttons`, `Button`, `FollowUps`, `Form`, `Input`, `Select`, `DatePicker`.

- [ ] **Step 1: Write the failing test**

`apps/frontend/src/components/messaging/__tests__/openuiAsk.test.tsx`:

```tsx
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { CardContext } from "../openui/CardContext";
import { OpenUICard } from "../openui/OpenUICard";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
const ctx = (over = {}) => ({ messageId: "card", roomId: "room", authorId: "a", threadRootMessageId: null, attachments: [], answered: null, ...over });
const wrap = (program: string, over = {}) => render(<CardContext.Provider value={ctx(over)}><OpenUICard program={program} /></CardContext.Provider>);

it("a single Choice posts on the press", async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  wrap('root = Stack([q])\nq = Choice("Which plan first?", ["Starter", "Studio"])', { onReply });
  expect(screen.getByText("Which plan first?")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Studio" }));
  expect(onReply).toHaveBeenCalledWith("Studio");
});

it("a multi Choice posts the picks on Done", async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  wrap('root = Stack([q])\nq = Choice("Which channels?", ["Email", "SMS", "Push"], true)', { onReply });
  await userEvent.click(screen.getByRole("checkbox", { name: "Email" }));
  await userEvent.click(screen.getByRole("checkbox", { name: "Push" }));
  expect(onReply).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "Done" }));
  expect(onReply).toHaveBeenCalledWith("Email, Push");
});

it("options are bounded", () => {
  const options = Array.from({ length: 20 }, (_, i) => `"Option ${i + 1}"`).join(", ");
  wrap(`root = Stack([q])\nq = Choice("Pick", [${options}])`, { onReply: vi.fn() });
  expect(screen.getAllByRole("button").filter((b) => /^Option/.test(b.textContent ?? ""))).toHaveLength(8);
  expect(screen.getByText("+12 more")).toBeInTheDocument();
  wrap('root = Stack([q])\nq = Choice("Anything?", [])', { onReply: vi.fn() });
  expect(screen.getByText("Anything?")).toBeInTheDocument();
});

it("FollowUps are chips that post their text", async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  wrap('root = Stack([f])\nf = FollowUps(["Show me the numbers", "Draft the email"])', { onReply });
  await userEvent.click(screen.getByRole("button", { name: "Draft the email" }));
  expect(onReply).toHaveBeenCalledWith("Draft the email");
});

it("a Form posts Label: value lines", async () => {
  const onReply = vi.fn().mockResolvedValue(undefined);
  wrap(['root = Stack([f])', 'f = Form("Book it", [Input("name", "Name", "Your name"), Select("plan", "Plan", ["Starter", "Studio"]), DatePicker("start", "Start")], "Book")'].join("\n"), { onReply });
  await userEvent.type(screen.getByRole("textbox", { name: "Name" }), "Ada");
  await userEvent.selectOptions(screen.getByRole("combobox", { name: "Plan" }), "Studio");
  await userEvent.type(screen.getByLabelText("Start"), "2026-11-01");
  await userEvent.click(screen.getByRole("button", { name: "Book" }));
  expect(onReply).toHaveBeenCalledWith("Name: Ada\nPlan: Studio\nStart: 2026-11-01");
});

it("an answered Choice shows the pick", () => {
  wrap('root = Stack([q])\nq = Choice("Which plan first?", ["Starter", "Studio"])', { onReply: vi.fn(), answered: "Studio" });
  const group = screen.getByRole("group", { name: "Which plan first?" });
  expect(within(group).getByRole("button", { name: "Studio" })).toHaveAttribute("aria-pressed", "true");
  expect(within(group).getByRole("button", { name: "Starter" })).toBeDisabled();
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__/openuiAsk.test.tsx`
Expected: FAIL (no Choice/FollowUps/Form renderers).

- [ ] **Step 3: Write the asking renderers**

`apps/frontend/src/components/messaging/openui/renderers/ask.tsx`:

```tsx
"use client";

import { useId, useState } from "react";
import { CARD_MAX_OPTIONS, choiceReply, formReply } from "@lightbulb/shared";
import { Button as UiButton } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input as UiInput } from "@/components/ui/input";
import { useCardPress } from "./shared";
import type { RenderProps, SubComponent } from "./shared";

export function ChoiceView({ props }: RenderProps<{ question: string; options: string[]; multi?: boolean }>) {
  const { press, disabled, answered } = useCardPress();
  const [picked, setPicked] = useState<string[]>([]);
  const shown = props.options.slice(0, CARD_MAX_OPTIONS);
  const hidden = props.options.length - shown.length;
  const answeredSet = new Set((answered ?? "").split(",").map((s) => s.trim()));
  return (
    <div role="group" aria-label={props.question} className="grid gap-2 openui-live">
      <p className="text-ui font-medium">{props.question}</p>
      {props.multi ? (
        <>
          {shown.map((o) => {
            const checked = answered !== null ? answeredSet.has(o) : picked.includes(o);
            return <label key={o} className="flex items-center gap-2 text-ui">
              <Checkbox checked={checked} disabled={disabled} aria-label={o}
                onCheckedChange={(v) => setPicked((cur) => v ? [...cur, o] : cur.filter((p) => p !== o))} />
              {o}
            </label>;
          })}
          {answered === null && <UiButton type="button" size="sm" className="justify-self-start" disabled={disabled || picked.length === 0} onClick={() => press(choiceReply(picked))}>Done</UiButton>}
        </>
      ) : (
        <div className="flex flex-wrap gap-2">
          {shown.map((o) => <UiButton key={o} type="button" size="sm" variant="outline" disabled={disabled} aria-pressed={answered !== null && answered === o} onClick={() => press(o)}>{o}</UiButton>)}
        </div>
      )}
      {hidden > 0 && <p className="text-caption text-muted-foreground">+{hidden} more</p>}
      {answered !== null && <p className="text-caption openui-answer">You picked {answered}.</p>}
    </div>
  );
}

type ButtonProps = { label: string; reply?: string; variant?: "primary" | "secondary" };

export function ButtonsView({ props }: RenderProps<{ buttons: SubComponent<ButtonProps>[] }>) {
  const { press, disabled, answered } = useCardPress();
  return (
    <div className="flex flex-wrap gap-2 openui-live" role="group">
      {props.buttons.map((b, i) => {
        const text = (b.props.reply ?? b.props.label).trim();
        return <UiButton key={`${b.props.label}-${i}`} type="button" size="sm" variant={b.props.variant === "secondary" ? "outline" : "default"}
          disabled={disabled} aria-pressed={answered !== null && answered === text} onClick={() => press(text)}>{b.props.label}</UiButton>;
      })}
    </div>
  );
}

/** Button never renders alone: Buttons reads its props. */
export function ButtonView() { return null; }

export function FollowUpsView({ props }: RenderProps<{ replies: string[] }>) {
  const { press, disabled } = useCardPress();
  return (
    <div className="flex flex-wrap gap-2 openui-live" role="group" aria-label="Suggested replies">
      {props.replies.map((r) => <UiButton key={r} type="button" size="sm" variant="outline" disabled={disabled} onClick={() => press(r)}>{r}</UiButton>)}
    </div>
  );
}

type FieldProps = { name: string; label: string; placeholder?: string; options?: string[] };

export function FormView({ props }: RenderProps<{ title: string; fields: SubComponent<FieldProps>[]; submit?: string }>) {
  const { press, disabled, answered } = useCardPress();
  const base = useId();
  const [values, setValues] = useState<Record<string, string>>({});
  const set = (name: string, value: string) => setValues((cur) => ({ ...cur, [name]: value }));
  const submit = () => press(formReply(props.fields.map((f) => ({ label: f.props.label, value: values[f.props.name] ?? "" }))));
  return (
    <form className="grid gap-2 openui-live" aria-label={props.title} onSubmit={(e) => { e.preventDefault(); submit(); }}>
      <p className="text-ui font-medium">{props.title}</p>
      {props.fields.map((f) => {
        const id = `${base}-${f.props.name}`;
        const value = values[f.props.name] ?? "";
        return <div key={f.props.name} className="grid gap-1">
          <label htmlFor={id} className="text-caption text-muted-foreground">{f.props.label}</label>
          {f.typeName === "Select"
            ? <select id={id} className="room-input" value={value} disabled={disabled} onChange={(e) => set(f.props.name, e.target.value)}>
                <option value="">Choose…</option>
                {(f.props.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            : f.typeName === "DatePicker"
              ? <UiInput id={id} type="date" value={value} disabled={disabled} onChange={(e) => set(f.props.name, e.target.value)} />
              : <UiInput id={id} type="text" placeholder={f.props.placeholder} value={value} disabled={disabled} onChange={(e) => set(f.props.name, e.target.value)} />}
        </div>;
      })}
      {answered === null
        ? <UiButton type="submit" size="sm" className="justify-self-start" disabled={disabled}>{props.submit ?? "Submit"}</UiButton>
        : <p className="text-caption openui-answer">Sent.</p>}
    </form>
  );
}

/** Fields never render alone: Form reads their props. */
export function InputView() { return null; }
export function SelectView() { return null; }
export function DatePickerView() { return null; }
```

`DatePicker` is a native `<input type="date">` on purpose: no picker library.

- [ ] **Step 4: Register and remove the Task 5 stub**

In `renderers/index.tsx`: delete the interim `ButtonsView` and its two imports, import `{ ButtonView, ButtonsView, ChoiceView, DatePickerView, FollowUpsView, FormView, InputView, SelectView } from "./ask"`, and add them to `RENDERERS` under their names (`Choice`, `Buttons`, `Button`, `FollowUps`, `Form`, `Input`, `Select`, `DatePicker`). Every one of the twenty names now has an entry; change the type of `RENDERERS` from `Partial<Record<...>>` to `Record<CardName, ...>` so a missing name fails typecheck.

- [ ] **Step 5: Run all card tests**

Run: `pnpm -F @lightbulb/frontend exec vitest run src/components/messaging/__tests__/openui`
Expected: PASS (all four files).

- [ ] **Step 6: Typecheck, lint, scanner, commit**

Run: `pnpm -F @lightbulb/frontend exec tsc --noEmit -p tsconfig.json && pnpm -F @lightbulb/frontend lint && (cd apps/frontend && node scripts/brand-tokens-scan.mjs)`

```bash
git add apps/frontend/src/components/messaging/openui apps/frontend/src/components/messaging/__tests__/openuiAsk.test.tsx
git commit -m "web: card renderers for Choice, Buttons, FollowUps and Form"
```

This closes PR 1 (Tasks 1–7): open it, wait for CI and Codex, merge. It is inert in prod until an agent writes a fence.

---

### Task 8: The skill: generated signatures, the rule and the reply rules

**Files:**
- Create: `packages/shared/scripts/openui-reference.mjs`
- Create: `apps/infra/hermes/skills-src/lightbulb/references/ui.md`
- Test: `packages/shared/src/ui/reference.test.ts`
- Modify: `apps/infra/hermes/skills-src/lightbulb/SKILL.md` (one routing row, one non-negotiable, the reply rules)
- Modify: `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md` (anchors)
- Test: `apps/backend/tests/unit/services/test_hermes_ui_skill.py`

**Interfaces:**
- Consumes: `cardLibrary` (Task 2).
- Produces: `references/ui.md` with a block between `<!-- openui:signatures:start -->` and `<!-- openui:signatures:end -->` that equals `cardLibrary.prompt({})`'s "Component Signatures" section.

- [ ] **Step 1: Write the generator**

`packages/shared/scripts/openui-reference.mjs`:

```js
// Writes the card signatures into the lightbulb skill reference, between two markers.
// Run from the repo root:  node --import tsx packages/shared/scripts/openui-reference.mjs
// (`tsx` lets node import the .ts library.) src/ui/reference.test.ts fails when the file and the library drift.
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { END, REFERENCE_PATH, START, signaturesBlock } from "../src/ui/reference.ts";

const here = dirname(fileURLToPath(import.meta.url));
const path = resolve(here, "../../..", REFERENCE_PATH);
const current = readFileSync(path, "utf8");
const start = current.indexOf(START), end = current.indexOf(END);
if (start < 0 || end < 0) throw new Error(`markers missing in ${path}`);
const next = current.slice(0, start + START.length) + "\n" + signaturesBlock() + "\n" + current.slice(end);
writeFileSync(path, next);
console.log(`wrote ${path}`);
```

If `tsx` is not in the root devDependencies (`grep '"tsx"' package.json`), add `"tsx": "^4.19.0"` there in this task and run `pnpm install`.

`packages/shared/src/ui/reference.ts`:

```ts
import { cardLibrary } from "./library";

export const REFERENCE_PATH = "apps/infra/hermes/skills-src/lightbulb/references/ui.md";
export const START = "<!-- openui:signatures:start -->";
export const END = "<!-- openui:signatures:end -->";

/** The "Component Signatures" section of the generated prompt, verbatim: one line per component. */
export function signaturesBlock(): string {
  const prompt = cardLibrary.prompt({});
  const lines = prompt.split("\n");
  const from = lines.findIndex((l) => l.startsWith("## Component Signatures"));
  const to = lines.findIndex((l, i) => i > from && l.startsWith("## "));
  if (from < 0) throw new Error("prompt has no Component Signatures section");
  return lines.slice(from + 1, to < 0 ? undefined : to).join("\n").trim();
}
```

- [ ] **Step 2: Write the drift test**

`packages/shared/src/ui/reference.test.ts`:

```ts
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, it } from "vitest";
import { END, REFERENCE_PATH, START, signaturesBlock } from "./reference";

it("the skill reference carries the library's signatures verbatim (run scripts/openui-reference.mjs to refresh)", () => {
  const text = readFileSync(resolve(__dirname, "../../../..", REFERENCE_PATH), "utf8");
  const inner = text.slice(text.indexOf(START) + START.length, text.indexOf(END)).trim();
  expect(inner).toBe(signaturesBlock());
});
```

- [ ] **Step 3: Write the reference**

`apps/infra/hermes/skills-src/lightbulb/references/ui.md`:

````markdown
# Cards in chat

A reply may carry ONE card: a fenced block tagged `openui-lang`. The app draws it as
buttons, a table, numbers, a plan or a form. A person's press on it arrives as their
next message, addressed to you, in plain words ("Studio", "Name: Ada", a table).

## When

| You want to | Write |
|---|---|
| Ask the person to choose between things you can name | `Choice` (or `Buttons` for two or three labelled replies) |
| Offer the next things they might ask for | `FollowUps` |
| Show a number, a change, a KPI | `Metric` |
| Compare things side by side | `Table` |
| Have the person correct numbers or names you drafted | `EditableTable` |
| Show a plan or where work stands | `Steps` |
| Collect a few fields (a name, a plan, a date) | `Form` with `Input`, `Select`, `DatePicker` |
| Back a research answer with its links | `Sources` |
| Show a picture you attached in this reply | `Image` (by file name, never a URL) |
| Show how numbers move, when the shape is the point | `BarChart`, `LineChart` |

Not a card: a sign-off, a spend, a launch, a public post. Those are approvals, with the
`request_approval` tool. A `Choice` is for a quick pick in conversation.

## Rules

- One fence per reply, with a sentence or two of your own before it. Never a fence in a
  feed post, in mail, or in a thread under a message that is not yours.
- `root = Stack([...])` is always the first line. Arguments are positional, in the order
  of the signature; strings in double quotes; leave trailing optional arguments out.
- A `Choice` has two to six options of at most forty characters, and asks one thing. Put
  what you recommend first.
- Only what you can act on: never offer an option you would not do, and never a button
  that implies a record changed (nothing changes on a press; the person's words do).
- A card is not a document. Research, plans and reports still go in a document
  (`references/chat-vs-document.md`); a card may summarize one.

## Examples

```openui-lang
root = Stack([t, q])
t = Text("Two plans are ready. I'd start with Studio: it is where the demand was.")
q = Choice("Which plan should I price first?", ["Studio", "Starter", "Both at once"])
```

```openui-lang
root = Stack([m1, m2, tbl])
m1 = Metric("MRR", "$4,200", "+12% vs September")
m2 = Metric("Open invoices", "7", "3 overdue")
tbl = Table([Col("Customer", ["Acme", "Birch"]), Col("Due", ["Oct 2", "Oct 9"]), Col("Amount", ["$1,200", "$640"])])
```

## Signatures

Arguments marked `?` are optional. A child in `[...]` is written inline (`Col("Plan", ["A"])`)
or as a named line referenced by name.

<!-- openui:signatures:start -->
<!-- openui:signatures:end -->
````

Then run: `node --import tsx packages/shared/scripts/openui-reference.mjs` and confirm the block is filled with twenty signature lines.

- [ ] **Step 4: Run the drift test**

Run: `pnpm -F @lightbulb/shared exec vitest run src/ui/reference.test.ts`
Expected: PASS. Edit one character inside the markers and run again: FAIL. Restore it.

- [ ] **Step 5: The skill's always-on text**

In `apps/infra/hermes/skills-src/lightbulb/SKILL.md`:

(a) In the "Where to read the detail" table, after the `references/voice.md` row, add:

```markdown
| `references/ui.md` | **Asking the person to pick, or showing numbers, a comparison, a plan, a form**: the one `openui-lang` card a reply may carry, when each component fits, and the signatures. |
```

(b) In "Non-negotiables", after the "A person's decision is an approval" bullet, add:

```markdown
- **Ask with a card, not a list.** When the person must pick between things you can
  name, or you have numbers, a comparison or a plan to show, write the one
  `openui-lang` card from `references/ui.md`; their press comes back as their next
  message. A sign-off is still an approval (`request_approval`), never a card.
```

(c) After the Non-negotiables heading's bullets, add a short section (the 2026-10-13 reply rules):

```markdown
## How you reply in chat

- Lead with the answer or the action. No preamble.
- One to four sentences by default. Bullets only for three or more parallel items the
  reader will act on. No headings in chat.
- Commit to a concrete next step with an owner and a time, or say plainly that you
  cannot do it.
- Never offer work you will not do in this turn or put on a task.
- At most one question, and only if you are blocked. Options go in a card.
- When done, say "Done" plus the link or the number. Do not recap the steps.
- Match the room: terse and lowercase if the people are.
```

Measure: `wc -c apps/infra/hermes/skills-src/lightbulb/SKILL.md` must be at most 13,600.

- [ ] **Step 6: Anchors**

In `apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md`, in the anchor list, add:

```text
  - Cards in chat ........... references/ui.md
                              <- packages/shared/src/ui/library.ts (the vocabulary; signatures block is GENERATED by
                                 packages/shared/scripts/openui-reference.mjs and pinned by src/ui/reference.test.ts),
                                 apps/frontend/src/components/messaging/openui/ (web renderers, click -> reply),
                                 apps/mobile/src/components/chat/openui/ (phone). SKILL.md: the "Ask with a card"
                                 non-negotiable and "How you reply in chat" (docs/research/2026-10-13-employee-voice-and-consumer-agents.md rung 1).
```

- [ ] **Step 7: The backend-side build check**

`apps/backend/tests/unit/services/test_hermes_ui_skill.py`:

```python
"""The lightbulb skill routes card-shaped replies to references/ui.md, and the built tree ships it
with every component name the shared library defines (packages/shared/src/ui/library.ts)."""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
BUILD = REPO / "apps/infra/hermes/build-skills.sh"
LIBRARY = REPO / "packages/shared/src/ui/library.ts"


def _names() -> list[str]:
    return re.findall(r'name: "([A-Za-z]+)"', LIBRARY.read_text())


def test_the_skill_routes_cards_to_the_ui_reference_and_ships_every_signature(tmp_path):
    out = tmp_path / "skills"
    subprocess.run([str(BUILD), str(out)], check=True, capture_output=True)
    skill = (out / "lightbulb/SKILL.md").read_text()
    row = next(line for line in skill.splitlines() if "`references/ui.md`" in line)
    assert row.startswith("|") and "openui-lang" in row
    assert "Ask with a card" in skill and "request_approval" in skill
    assert "## How you reply in chat" in skill and "Lead with the answer" in skill
    ui = (out / "lightbulb/references/ui.md").read_text()
    names = _names()
    assert len(names) == 20
    for name in names:
        assert f"{name}(" in ui, name
    assert "<!-- openui:signatures:start -->" in ui and "request_approval" in ui
    assert "ui.md" in (REPO / "apps/infra/hermes/skills-src/lightbulb/MAINTENANCE.md").read_text()
```

Run: `cd apps/backend && uv run pytest tests/unit/services/test_hermes_ui_skill.py tests/unit/services/test_hermes_skill_tool_names.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add packages/shared/scripts/openui-reference.mjs packages/shared/src/ui/reference.ts packages/shared/src/ui/reference.test.ts apps/infra/hermes/skills-src/lightbulb apps/backend/tests/unit/services/test_hermes_ui_skill.py package.json pnpm-lock.yaml
git commit -m "skill: cards in chat (references/ui.md, generated signatures) and the chat reply rules"
```

This is PR 2. Merging it triggers `build-hermes-image.yml`, which pushes `h-<YYYY.M.D>-<sha7>` to ECR. It changes no running runtime.

---

### Task 9: Dev pin, dev acceptance, prod pin and roll

**Files:**
- Modify: `hermes-version.json` (`dev.tag`, later `prod.tag`), one PR each.

**Interfaces:**
- Consumes: the image tag printed by the `build-hermes-image.yml` run for PR 2's merge commit.

- [ ] **Step 1: Find the candidate tag**

Run: `gh run list --repo TheLightbulbCompany/lightbulb --workflow build-hermes-image.yml --limit 3 --json headSha,conclusion,databaseId` and open the run for PR 2's merge SHA; its summary prints the tag (`h-2026.10.17-abc1234`). Confirm in ECR: `aws ecr describe-images --repository-name lightbulb/hermes-agent-arm64 --image-ids imageTag=<tag> --profile isol8-admin --region us-east-1 --query 'imageDetails[0].imagePushedAt'`.

- [ ] **Step 2: Flip dev**

On a fresh branch from `origin/main`, set `"dev": { "tag": "<tag>" }` in `hermes-version.json`, PR titled `chore(hermes): pin dev to <tag> (cards in chat)`, merge after CI. Dev deploys on merge; watch `gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status --interval 60`.

- [ ] **Step 3: Reach the dev test workspace's runtime**

A pin changes what a NEW runtime is created with. For the dev test org, either roll it (`cd apps/backend && uv run python scripts/roll_hermes_runtimes.py --env dev --owner <org_id>`) or reprovision from the browser console as CLAUDE.md's "Workspace Re-provisioning (Dev)" shows, then `uv run python scripts/reapply_hermes_skills_dirs.py --env dev --owner <org_id>` so the existing profiles carry `references/ui.md`. Verify: `aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <id> --profile isol8-admin --query 'agentRuntimeArtifact.containerConfiguration.containerUri'` shows the new tag.

- [ ] **Step 4: Chrome acceptance on dev (receipts on the pin PR)**

Signed in as a dev test account (Clerk sign-in token, CLAUDE.md "Manual dev testing accounts"):

1. DM an agent: "Decide for me: should we price Starter or Studio first? Give me the choice." Expect a Choice card. Press one. Expect the agent's next message to act on the pick, and the card to show "You picked …" with disabled buttons after reload.
2. "Compare three email providers for us in a table." Expect a Table card, no prose wall.
3. "Where does the launch plan stand?" Expect Steps (or a short answer; a card is not mandatory here, note what came).
4. In `#general`, @mention an agent with a multi-pick question. Expect a multi Choice; pick two, Done; the reply posts top-level, mentioning the agent.
5. Open a thread under an agent card and ask again; the answer lands in the thread.
6. Read `openui.parse_error` in PostHog for the test org over the session: note the count.

Paste the six observations and screenshots on the pin PR.

- [ ] **Step 5: Flip prod and roll**

PR `chore(hermes): pin prod to <tag> (cards in chat)` with the same tag. After it merges and the owner's prod train deploys it: `scripts/roll_hermes_runtimes.py --env prod` (a one-off Fargate run-task per the `prod-runtime-roll-via-run-task` memory; it waits for idle sessions, ~10.5 min per busy owner) then `scripts/reapply_hermes_skills_dirs.py --env prod --all`. Verify one prod runtime's `containerUri` as in Step 3.

---

### Task 10: The phone: renderer, fence rule and click to reply

**Files:**
- Create: `apps/mobile/src/components/chat/openui/CardReplyContext.tsx`
- Create: `apps/mobile/src/components/chat/openui/library.tsx`
- Create: `apps/mobile/src/components/chat/openui/OpenUICard.tsx`
- Modify: `apps/mobile/src/components/chat/MessageMarkdown.tsx` (the `fence` rule at line 69)
- Modify: `apps/mobile/src/components/room/MessageGroup.tsx` (provide the context around each agent message)
- Modify: `apps/mobile/src/lib/rooms.ts` (`useSendDrafts.send` takes an optional `replyToMessageId`)
- Modify: `apps/mobile/src/app/(app)/room/[roomId]/index.tsx` and `apps/mobile/src/app/(app)/room/[roomId]/thread/[messageId].tsx` (pass `onCardReply` and `cardAnswers`)
- Modify: `apps/mobile/jest.config.js` (`transformIgnorePatterns` allows `@openuidev`)
- Test: `apps/mobile/src/components/chat/__tests__/openui.test.tsx`

**Interfaces:**
- Consumes: `CARD_COMPONENTS`, `CARD_ROOT`, `CARD_FENCE`, `CARD_MAX_BYTES`, `CARD_MAX_OPTIONS`, `choiceReply`, `formReply`, `tableReply`, `cardAnswerFor`, `CardReply` (Tasks 2–3).
- Produces: `CardReplyContext` with `{ messageId, agentId, threadRootMessageId, attachments, answered, onReply? }`; `MessageGroupProps.onCardReply?: (reply: CardReply) => Promise<void>` and `MessageGroupProps.cardAnswers?: ReadonlyMap<string, string>`; `useSendDrafts(...).send(draft, { replyToMessageId? })`.

- [ ] **Step 1: Write the failing test**

`apps/mobile/src/components/chat/__tests__/openui.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react-native";
import { chatPalette } from "../../../config/theme";
import { MessageMarkdown } from "../MessageMarkdown";
import { CardReplyContext } from "../openui/CardReplyContext";

const fence = (body: string) => "```openui-lang\n" + body + "\n```";
const ctx = (over = {}) => ({ messageId: "card", agentId: "agent-1", threadRootMessageId: null, attachments: [], answered: null, ...over });

it("renders a Choice from an agent's fence and posts the press", () => {
  const onReply = jest.fn().mockResolvedValue(undefined);
  render(<CardReplyContext.Provider value={ctx({ onReply })}>
    <MessageMarkdown text={fence('root = Stack([q])\nq = Choice("Which plan first?", ["Starter", "Studio"])')} palette={chatPalette.light} />
  </CardReplyContext.Provider>);
  expect(screen.getByText("Which plan first?")).toBeTruthy();
  fireEvent.press(screen.getByRole("button", { name: "Studio" }));
  expect(onReply).toHaveBeenCalledWith("Studio");
});

it("a fence with no card context stays code", () => {
  render(<MessageMarkdown text={fence('root = Stack([t])\nt = Text("hi")')} palette={chatPalette.light} />);
  expect(screen.getByText(/root = Stack/)).toBeTruthy();
});

it("an answered card is read-only and shows the pick", () => {
  render(<CardReplyContext.Provider value={ctx({ onReply: jest.fn(), answered: "Studio" })}>
    <MessageMarkdown text={fence('root = Stack([q])\nq = Choice("Which plan first?", ["Starter", "Studio"])')} palette={chatPalette.light} />
  </CardReplyContext.Provider>);
  expect(screen.getByRole("button", { name: "Studio", disabled: true })).toBeTruthy();
  expect(screen.getByText("You picked Studio.")).toBeTruthy();
});

it("charts fall back to a table on the phone", () => {
  render(<CardReplyContext.Provider value={ctx()}>
    <MessageMarkdown text={fence('root = Stack([c])\nc = BarChart(["Jan", "Feb"], [10, 20], "Signups")')} palette={chatPalette.light} />
  </CardReplyContext.Provider>);
  expect(screen.getByText("Signups")).toBeTruthy();
  expect(screen.getByText("Feb")).toBeTruthy();
  expect(screen.getByText("20")).toBeTruthy();
});
```

- [ ] **Step 2: Run it to see it fail**

Run: `pnpm -F @lightbulb/mobile exec jest src/components/chat/__tests__/openui.test.tsx`
Expected: FAIL, `../openui/CardReplyContext` not found.

- [ ] **Step 3: Let jest transform the OpenUI packages**

In `apps/mobile/jest.config.js`, add `@openuidev` to the allowlisted package names inside the existing `transformIgnorePatterns` regex (the pattern lists packages that jest-expo transforms; append `|@openuidev/.*` inside the group).

- [ ] **Step 4: Context**

`apps/mobile/src/components/chat/openui/CardReplyContext.tsx`:

```tsx
import { createContext, useContext } from "react";
import type { MessageAttachment } from "@lightbulb/shared";

/** The phone twin of the web CardContext: provided by MessageGroup for an agent's message only. */
export interface CardReplyValue {
  messageId: string;
  agentId: string;
  threadRootMessageId: string | null;
  attachments: readonly MessageAttachment[];
  answered: string | null;
  onReply?: (text: string) => Promise<void>;
}

export const CardReplyContext = createContext<CardReplyValue | null>(null);
export const useCardReply = () => useContext(CardReplyContext);
export const useCardLive = () => { const c = useContext(CardReplyContext); return !!c?.onReply && c.answered === null; };
```

- [ ] **Step 5: Renderers and the library**

`apps/mobile/src/components/chat/openui/library.tsx`:

```tsx
import { useState } from "react";
import { Linking, Pressable, Text, TextInput, View } from "react-native";
import { createLibrary, defineComponent, useTriggerAction } from "@openuidev/react-lang";
import { CARD_COMPONENTS, CARD_MAX_OPTIONS, CARD_ROOT, choiceReply, formReply, tableReply, type CardName } from "@lightbulb/shared";
import { fonts, spacing, type ChatPalette } from "../../../config/theme";
import { useCardLive, useCardReply } from "./CardReplyContext";

// Palette and font field names are the ones `ChatPalette` / `fonts` in config/theme.ts define
// (fg, fgSoft, edge, raised, link, alert; sans, sansMedium, sansSemiBold): read that file, do not guess.
type RP<P> = { props: P; renderNode: (v: unknown) => React.ReactNode };
type Sub<P> = { typeName: string; props: P };

// ponytail: one module-level palette set by OpenUICard before render; react-lang renderers take no
// extra props. Upgrade to a React context read inside each renderer if two palettes ever render at once.
let palette: ChatPalette;
export const setCardPalette = (p: ChatPalette) => { palette = p; };

function usePress() {
  const live = useCardLive();
  const ctx = useCardReply();
  const trigger = useTriggerAction();
  const [busy, setBusy] = useState(false);
  return {
    press: (text: string) => { if (!live || busy) return; setBusy(true); trigger(text); },
    disabled: !live || busy,
    answered: ctx?.answered ?? null,
  };
}

const Chip = ({ label, disabled, pressed, onPress }: { label: string; disabled: boolean; pressed?: boolean; onPress: () => void }) => (
  <Pressable accessibilityRole="button" accessibilityLabel={label} accessibilityState={{ disabled, selected: !!pressed }} disabled={disabled} onPress={onPress}
    style={{ borderWidth: 1, borderColor: pressed ? palette.fg : palette.edge, borderRadius: 999, paddingHorizontal: spacing.md, paddingVertical: spacing.xs, opacity: disabled && !pressed ? 0.6 : 1 }}>
    <Text style={{ fontFamily: fonts.sansMedium, color: palette.fg }}>{label}</Text>
  </Pressable>
);
const Row = ({ children }: { children: React.ReactNode }) => <View style={{ flexDirection: "row", flexWrap: "wrap", gap: spacing.sm }}>{children}</View>;
const Prose = ({ children, soft, bold }: { children: React.ReactNode; soft?: boolean; bold?: boolean }) =>
  <Text style={{ fontFamily: bold ? fonts.sansSemiBold : fonts.sans, color: soft ? palette.fgSoft : palette.fg }}>{children}</Text>;

function SimpleTable({ columns, rows }: { columns: string[]; rows: string[][] }) {
  return (
    <View style={{ borderWidth: 1, borderColor: palette.edge, borderRadius: 6 }}>
      <View style={{ flexDirection: "row", borderBottomWidth: 1, borderColor: palette.edge }}>{columns.map((c) => <Text key={c} style={{ flex: 1, padding: spacing.sm, fontFamily: fonts.sansSemiBold, color: palette.fg }}>{c}</Text>)}</View>
      {rows.map((r, i) => <View key={i} style={{ flexDirection: "row" }}>{r.map((cell, j) => <Text key={j} style={{ flex: 1, padding: spacing.sm, color: palette.fg }}>{cell}</Text>)}</View>)}
    </View>
  );
}

const R = {
  Stack: ({ props, renderNode }: RP<{ children: unknown }>) => <View style={{ gap: spacing.md }}>{renderNode(props.children)}</View>,
  Text: ({ props }: RP<{ text: string; size?: string }>) => <Prose bold={props.size === "heading"} soft={props.size === "small"}>{props.text}</Prose>,
  Callout: ({ props }: RP<{ text: string; tone?: string }>) => <View style={{ borderLeftWidth: 3, borderLeftColor: props.tone === "warn" ? palette.alert : palette.link, paddingLeft: spacing.sm }}><Prose>{props.text}</Prose></View>,
  Metric: ({ props }: RP<{ label: string; value: string; delta?: string }>) => <View><Prose soft>{props.label}</Prose><Text style={{ fontFamily: fonts.sansSemiBold, fontSize: 22, color: palette.fg }}>{props.value}</Text>{props.delta ? <Prose soft>{props.delta}</Prose> : null}</View>,
  Table: ({ props }: RP<{ columns: Sub<{ label: string; data: (string | number)[] }>[] }>) => {
    const cols = props.columns.map((c) => c.props);
    const n = Math.max(0, ...cols.map((c) => c.data.length));
    return <SimpleTable columns={cols.map((c) => c.label)} rows={Array.from({ length: n }, (_, r) => cols.map((c) => String(c.data[r] ?? "")))} />;
  },
  Col: () => null,
  EditableTable: ({ props }: RP<{ columns: string[]; rows: string[][]; submit?: string }>) => {
    const { press, disabled, answered } = usePress();
    const [rows, setRows] = useState(() => props.rows.map((r) => props.columns.map((_, i) => r[i] ?? "")));
    return <View style={{ gap: spacing.sm }}>
      {rows.map((r, ri) => <View key={ri} style={{ flexDirection: "row", gap: spacing.xs }}>{r.map((cell, ci) =>
        <TextInput key={ci} accessibilityLabel={`${props.columns[ci]}, row ${ri + 1}`} editable={!disabled} value={cell} onChangeText={(v) => setRows((cur) => cur.map((row, a) => a === ri ? row.map((x, b) => b === ci ? v : x) : row))}
          style={{ flex: 1, borderWidth: 1, borderColor: palette.edge, borderRadius: 6, padding: spacing.sm, color: palette.fg }} />)}</View>)}
      {answered === null ? <Row><Chip label={props.submit ?? "Submit"} disabled={disabled} onPress={() => press(tableReply(props.columns, rows))} /></Row> : <Prose soft>Sent.</Prose>}
    </View>;
  },
  BarChart: ({ props }: RP<{ labels: string[]; values: number[]; title?: string }>) => <View style={{ gap: spacing.xs }}>{props.title ? <Prose soft>{props.title}</Prose> : null}<SimpleTable columns={["", "Value"]} rows={props.labels.map((l, i) => [l, String(props.values[i] ?? 0)])} /></View>,
  LineChart: ({ props }: RP<{ labels: string[]; values: number[]; title?: string }>) => <View style={{ gap: spacing.xs }}>{props.title ? <Prose soft>{props.title}</Prose> : null}<SimpleTable columns={["", "Value"]} rows={props.labels.map((l, i) => [l, String(props.values[i] ?? 0)])} /></View>,
  Steps: ({ props }: RP<{ steps: { label: string; status?: string }[] }>) => <View style={{ gap: spacing.xs }}>{props.steps.map((s, i) => <Prose key={i} soft={s.status === "done"}>{s.status === "done" ? "✓ " : s.status === "doing" ? "› " : "○ "}{s.label}</Prose>)}</View>,
  Sources: ({ props }: RP<{ sources: { title: string; url: string }[] }>) => <View style={{ gap: spacing.xs }}>{props.sources.map((s, i) => /^https?:\/\//.test(s.url)
    ? <Text key={i} accessibilityRole="link" style={{ color: palette.link, textDecorationLine: "underline" }} onPress={() => { void Linking.openURL(s.url); }}>{s.title}</Text>
    : <Prose key={i} soft>{s.title}</Prose>)}</View>,
  Image: ({ props }: RP<{ fileName: string; alt: string }>) => <Prose soft>{props.alt}</Prose>, // attachments already render inline under the bubble; the card names the picture
  Choice: ({ props }: RP<{ question: string; options: string[]; multi?: boolean }>) => {
    const { press, disabled, answered } = usePress();
    const [picked, setPicked] = useState<string[]>([]);
    const shown = props.options.slice(0, CARD_MAX_OPTIONS);
    return <View style={{ gap: spacing.sm }}>
      <Prose bold>{props.question}</Prose>
      <Row>{shown.map((o) => <Chip key={o} label={o} disabled={disabled} pressed={props.multi ? picked.includes(o) : answered === o}
        onPress={() => props.multi ? setPicked((cur) => cur.includes(o) ? cur.filter((p) => p !== o) : [...cur, o]) : press(o)} />)}</Row>
      {props.multi && answered === null ? <Row><Chip label="Done" disabled={disabled || picked.length === 0} onPress={() => press(choiceReply(picked))} /></Row> : null}
      {props.options.length > shown.length ? <Prose soft>+{props.options.length - shown.length} more</Prose> : null}
      {answered !== null ? <Prose soft>You picked {answered}.</Prose> : null}
    </View>;
  },
  Buttons: ({ props }: RP<{ buttons: Sub<{ label: string; reply?: string }>[] }>) => { const { press, disabled, answered } = usePress(); return <Row>{props.buttons.map((b, i) => { const t = (b.props.reply ?? b.props.label).trim(); return <Chip key={i} label={b.props.label} disabled={disabled} pressed={answered === t} onPress={() => press(t)} />; })}</Row>; },
  Button: () => null,
  FollowUps: ({ props }: RP<{ replies: string[] }>) => { const { press, disabled } = usePress(); return <Row>{props.replies.map((r) => <Chip key={r} label={r} disabled={disabled} onPress={() => press(r)} />)}</Row>; },
  Form: ({ props }: RP<{ title: string; fields: Sub<{ name: string; label: string; placeholder?: string; options?: string[] }>[]; submit?: string }>) => {
    const { press, disabled, answered } = usePress();
    const [values, setValues] = useState<Record<string, string>>({});
    return <View style={{ gap: spacing.sm }}>
      <Prose bold>{props.title}</Prose>
      {props.fields.map((f) => <View key={f.props.name} style={{ gap: spacing.xs }}>
        <Prose soft>{f.props.label}</Prose>
        {f.typeName === "Select"
          ? <Row>{(f.props.options ?? []).map((o) => <Chip key={o} label={o} disabled={disabled} pressed={values[f.props.name] === o} onPress={() => setValues((c) => ({ ...c, [f.props.name]: o }))} />)}</Row>
          : <TextInput accessibilityLabel={f.props.label} editable={!disabled} placeholder={f.typeName === "DatePicker" ? "YYYY-MM-DD" : f.props.placeholder} value={values[f.props.name] ?? ""}
              onChangeText={(v) => setValues((c) => ({ ...c, [f.props.name]: v }))} style={{ borderWidth: 1, borderColor: palette.edge, borderRadius: 6, padding: spacing.sm, color: palette.fg }} />}
      </View>)}
      {answered === null ? <Row><Chip label={props.submit ?? "Submit"} disabled={disabled} onPress={() => press(formReply(props.fields.map((f) => ({ label: f.props.label, value: values[f.props.name] ?? "" }))))} /></Row> : <Prose soft>Sent.</Prose>}
    </View>;
  },
  Input: () => null, Select: () => null, DatePicker: () => null,
} satisfies Record<CardName, (p: never) => React.ReactNode>;

export const mobileCardLibrary = createLibrary({
  root: CARD_ROOT,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- OpenUI types each renderer by its schema; R is checked by name above
  components: CARD_COMPONENTS.map((c) => defineComponent({ name: c.name, description: c.description, props: c.props, component: R[c.name as CardName] as any })),
});
```

The phone's `DatePicker` is a text field with a `YYYY-MM-DD` placeholder in this plan; a native date sheet is a follow-up.

- [ ] **Step 6: The card mount and the fence rule**

`apps/mobile/src/components/chat/openui/OpenUICard.tsx`:

```tsx
import { useCallback } from "react";
import { Text, View } from "react-native";
import { Renderer, type ActionEvent } from "@openuidev/react-lang";
import { CARD_MAX_BYTES } from "@lightbulb/shared";
import { spacing, type ChatPalette } from "../../../config/theme";
import { useCardReply } from "./CardReplyContext";
import { mobileCardLibrary, setCardPalette } from "./library";

export function OpenUICard({ program, palette }: { program: string; palette: ChatPalette }) {
  const ctx = useCardReply();
  setCardPalette(palette);
  const onAction = useCallback((event: ActionEvent) => {
    if (event.type !== "continue_conversation" || !ctx?.onReply) return;
    const text = event.humanFriendlyMessage.trim();
    if (text) void ctx.onReply(text);
  }, [ctx]);
  if (new TextEncoder().encode(program).length > CARD_MAX_BYTES) return <Text selectable style={{ color: palette.fg }}>{program}</Text>;
  return (
    <View style={{ marginTop: spacing.sm, borderWidth: 1, borderColor: palette.edge, borderRadius: 10, backgroundColor: palette.raised, padding: spacing.md }}>
      <Renderer response={program} library={mobileCardLibrary} isStreaming={false} onAction={onAction} />
    </View>
  );
}
```

In `MessageMarkdown.tsx`, the `fence` rule: at its top, after computing `lang` and `content`, add

```tsx
    if (lang === CARD_FENCE && cardReply) return <OpenUICard key={node.key} program={content} palette={palette} />;
```

`rules` is a module constant, so `cardReply` and `palette` are not in scope there: convert `rules` into a function `rulesFor(cardReply: CardReplyValue | null, palette: ChatPalette | undefined)` that returns the same object (the other rules unchanged), and in `MessageMarkdownInner` compute `const cardReply = useCardReply();` and `const r = useMemo(() => rulesFor(cardReply, palette), [cardReply, palette]);` passing `rules={r}`. Import `CARD_FENCE` from `@lightbulb/shared`, `OpenUICard` from `./openui/OpenUICard`, `useCardReply`/`CardReplyValue` from `./openui/CardReplyContext`.

- [ ] **Step 7: Provide the context and the send path**

`apps/mobile/src/lib/rooms.ts`, in `useSendDrafts`: change the `send` signature to `(draft: ComposedMessage, options?: { replyToMessageId?: string | null }) => Promise<void>` and build the request with `replyToMessageId: options?.replyToMessageId ?? threadRootMessageId`. Everything else unchanged.

`apps/mobile/src/components/room/MessageGroup.tsx`: add props

```ts
  onCardReply?: (reply: CardReply) => Promise<void>;
  cardAnswers?: ReadonlyMap<string, string>;
```

and wrap each `<MessageMarkdown .../>` for an agent message (the branch at line 280, not the `mine` branch at 314) in

```tsx
<CardReplyContext.Provider value={message.author.kind === "agent" && !deleted ? {
  messageId: message.messageId, agentId: message.author.id, threadRootMessageId: message.threadRootMessageId, attachments: message.attachments ?? [],
  answered: cardAnswers?.get(message.messageId) ?? null,
  onReply: onCardReply ? (text) => onCardReply({ text, agentId: message.author.id, messageId: message.messageId, threadRootMessageId: message.threadRootMessageId }) : undefined,
} : null}>
  <MessageMarkdown text={markdownText} palette={palette} />
</CardReplyContext.Provider>
```

In `room/[roomId]/index.tsx` and `room/[roomId]/thread/[messageId].tsx`, where `<MessageGroup` is rendered, pass:

```tsx
onCardReply={(reply) => send({ text: reply.text, mentions: [{ kind: "agent", id: reply.agentId }] }, { replyToMessageId: reply.messageId })}
cardAnswers={cardAnswers}
```

with `const cardAnswers = useMemo(() => new Map(rooms.messages.filter((m) => m.author.kind === "agent").map((m) => [m.messageId, cardAnswerFor(rooms.messages, m.messageId)]).filter((e): e is [string, string] => e[1] !== null)), [rooms.messages]);` and `cardAnswerFor` imported from `@lightbulb/shared`. In the thread screen, `send` already carries the thread root.

- [ ] **Step 8: Run the tests and the Metro gate**

Run: `pnpm -F @lightbulb/mobile exec jest src/components/chat/__tests__/openui.test.tsx src/components/room/__tests__/MessageGroup.test.tsx`
Expected: PASS.

Run: `pnpm -F @lightbulb/mobile exec tsc --noEmit && pnpm -F @lightbulb/mobile lint && cd apps/mobile && npx expo export --platform ios --output-dir /tmp/openui-export`
Expected: typecheck and lint clean; the export completes (this is `mobile-ci.yml`'s Metro-resolution gate; if Metro cannot resolve `@openuidev/react-lang`'s `exports`, add `"@openuidev/react-lang"` to the `resolver.unstable_enablePackageExports` allowance in `apps/mobile/metro.config.js`).

- [ ] **Step 9: Drive it in the simulator**

Per the `mobile-simulator-driving-recipe` memory (Maestro, never position-tap a send): open a dev room where an agent posted a Choice (from Task 9 Step 4), press an option, see the reply post and the card show "You picked …". Record the flow.

- [ ] **Step 10: Commit**

```bash
git add apps/mobile
git commit -m "phone: render agent cards and post a press as the person's reply"
```

This is PR 3. Merging to main ships an OTA (`mobile-ota.yml`); confirm delivery per the `mobile-delivery-pipeline-traps` memory, not the pipeline colour.
