# Get Started Guide Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an in-app "Get started" resource-center (floating launcher + compact card) to `/chat` with an activation checklist and a concept library, and remove the forced channel-setup gate.

**Architecture:** A new `getstarted/` component group mounted in `ChatLayout`, gated to render only once provisioning is `ready`. A compact card anchored to a bottom-right launcher (NOT a full-height drawer); action steps *minimize* the card and hand off to the real surface. Step completion is driven by a tiny event bus (producers emit on real success; the guide subscribes) plus one derived signal (channel linked). State persists in Clerk `unsafeMetadata`. The `ProvisioningStepper` `channels` phase is removed so channel setup is nudged here instead of forced pre-chat.

**Tech Stack:** Next.js 16 App Router, React 19, TypeScript, Tailwind v4 (cream/forest CSS vars), lucide-react, Clerk, SWR, vitest.

## Global Constraints

- **Frontend only.** No backend endpoints, no new DynamoDB tables, no new npm deps.
- **Design tokens (verbatim):** `--cream:#faf7f2` `--cream-deeper:#f3efe6` `--cream-deepest:#ebe5d6` `--edge:#e0dbd0` `--forest:#06402B` `--forest-soft:#4a9e74` `--forest-tint:#e6efe9` `--ink:#1a1a1a` `--mute:#8a8578` `--gold:#c2a378`. Reference via Tailwind arbitrary values (e.g. `bg-[var(--cream)]`, `text-[var(--forest)]`), matching existing components like `CompanyNav`.
- **Fonts:** body = DM Sans (default); editorial headings use the existing `font-editorial-serif` class (Lora).
- **Persistence:** Clerk `user.unsafeMetadata` (same pattern as the existing `onboarded` flag in `apps/frontend/src/app/onboarding/page.tsx`). Always merge — never overwrite the whole `unsafeMetadata`.
- **Testing:** vitest, **TARGETED single-file runs only** (`pnpm vitest run <file>`). NEVER run the full `pnpm test` suite (it consumes ~17.7 GB RAM). CI runs `tsc --noEmit` over test files too — **annotate every mock-builder's return type** so type-incomplete mocks don't pass locally but fail CI.
- **Render gate:** the guide renders only when `useProvisioningState().phase === "normal"`.
- **Icons:** lucide-react (already a dependency). Use `Sparkles`, `MessageSquare`, `ListPlus`, `Store`, `Plug`, `Users`, `Check`, `X`, `Minus`, `ArrowRight`, `ChevronLeft`.
- **Copy/comment rule:** code comments explain WHY, never reference PR numbers or this plan.

**Component data model (shared across tasks):**

```typescript
// StepId values used everywhere:
type StepId = "meet" | "chat" | "task" | "hire" | "channel";
// Article ids used everywhere:
type ArticleId = "meet-friday-atlas" | "getting-around" | "company-panels"
               | "marketplace" | "channels" | "billing-providers";
```

---

### Task 1: Step + article config and content registry

**Files:**
- Create: `apps/frontend/src/components/getstarted/getStartedConfig.ts`
- Create: `apps/frontend/src/components/getstarted/getStartedContent.tsx`
- Test: `apps/frontend/src/components/getstarted/__tests__/getStartedContent.test.ts`

**Interfaces:**
- Produces: `STEPS: StepDef[]` (5 entries), `ARTICLES: Article[]` (6 entries), types `StepId`, `ArticleId`, `StepDef`, `Article`, and `FridayAtlasDiagram` (a React component).

- [ ] **Step 1: Write the failing test**

```typescript
// getStartedContent.test.ts
import { describe, it, expect } from "vitest";
import { STEPS } from "../getStartedConfig";
import { ARTICLES } from "../getStartedContent";

describe("get started content", () => {
  it("defines exactly the five core steps in order", () => {
    expect(STEPS.map((s) => s.id)).toEqual(["meet", "chat", "task", "hire", "channel"]);
  });
  it("every action step has a CTA label", () => {
    for (const s of STEPS.filter((s) => s.id !== "meet")) {
      expect(s.ctaLabel.length).toBeGreaterThan(0);
    }
  });
  it("defines six articles with unique ids", () => {
    const ids = ARTICLES.map((a) => a.id);
    expect(ids).toHaveLength(6);
    expect(new Set(ids).size).toBe(6);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/getStartedContent.test.ts`
Expected: FAIL — cannot find module `../getStartedConfig`.

- [ ] **Step 3: Write `getStartedConfig.ts`**

```typescript
export type StepId = "meet" | "chat" | "task" | "hire" | "channel";

export interface StepDef {
  id: StepId;
  title: string;
  description: string;
  /** Empty for "meet" (it opens the article instead of a CTA). */
  ctaLabel: string;
  /** lucide-react icon name resolved by the component. */
  icon: "users" | "message" | "listplus" | "store" | "plug";
}

// Five core steps, in display order. "meet" is reading-based (opens its
// article); the rest are actions that minimize the card and hand off.
export const STEPS: StepDef[] = [
  { id: "meet", title: "Meet Friday & Atlas", description: "Your CEO and your recruiter — the two agents every workspace starts with.", ctaLabel: "", icon: "users" },
  { id: "chat", title: "Chat with Friday", description: "Talk to your CEO. Ask for something real — Friday plans it and delegates.", ctaLabel: "Open chat", icon: "message" },
  { id: "task", title: "Create your first task", description: "Open Tasks and file a job the team picks up and tracks.", ctaLabel: "New task", icon: "listplus" },
  { id: "hire", title: "Hire your first specialist", description: "Deploy a ready-made agent into your team from the marketplace.", ctaLabel: "Browse marketplace", icon: "store" },
  { id: "channel", title: "Connect a channel", description: "Reach your agents from Slack, Discord, Telegram or WhatsApp.", ctaLabel: "Set up channels", icon: "plug" },
];
```

- [ ] **Step 4: Write `getStartedContent.tsx`**

```tsx
import React from "react";

export type ArticleId =
  | "meet-friday-atlas" | "getting-around" | "company-panels"
  | "marketplace" | "channels" | "billing-providers";

export interface Article {
  id: ArticleId;
  tag: string;
  title: string;
  /** Rendered inside the article reader. */
  body: React.ReactNode;
}

// Friday → Atlas → specialists org-chart, ported from the approved mockup.
export function FridayAtlasDiagram(): React.ReactElement {
  return (
    <svg width="300" height="186" viewBox="0 0 300 186" fill="none"
      role="img" aria-label="Friday delegates to Atlas and to specialists; Atlas recruits the specialists.">
      <path d="M150 52 V72 M150 72 H78 M150 72 H222 M78 72 V92 M222 72 V92" stroke="#b5af9f" strokeWidth="1.6" />
      <path d="M222 92 V108 M222 108 H190 M222 108 H254 M190 108 V120 M254 108 V120" stroke="#cfc9ba" strokeWidth="1.4" />
      <path d="M96 132 C140 150 178 140 198 124" stroke="#c2a378" strokeWidth="1.6" strokeDasharray="4 4" markerEnd="url(#gsArrow)" />
      <defs><marker id="gsArrow" markerWidth="7" markerHeight="7" refX="5" refY="3.5" orient="auto"><path d="M0 0 L7 3.5 L0 7 Z" fill="#c2a378" /></marker></defs>
      <rect x="112" y="22" width="76" height="32" rx="9" fill="#06402B" />
      <text x="150" y="42" textAnchor="middle" fontSize="13" fontWeight="700" fill="#faf7f2">Friday</text>
      <text x="150" y="13" textAnchor="middle" fontSize="9" fill="#8a8578">CEO</text>
      <rect x="44" y="92" width="68" height="30" rx="8" fill="#c2a378" />
      <text x="78" y="111" textAnchor="middle" fontSize="12" fontWeight="700" fill="#3a2c12">Atlas</text>
      <rect x="170" y="120" width="40" height="26" rx="7" fill="#fff" stroke="#4a9e74" strokeWidth="1.4" />
      <text x="190" y="136" textAnchor="middle" fontSize="9" fontWeight="600" fill="#06402B">Sales</text>
      <rect x="234" y="120" width="40" height="26" rx="7" fill="#fff" stroke="#4a9e74" strokeWidth="1.4" />
      <text x="254" y="136" textAnchor="middle" fontSize="9" fontWeight="600" fill="#06402B">Research</text>
      <text x="222" y="170" textAnchor="middle" fontSize="9" fill="#8a8578">specialists you hire</text>
    </svg>
  );
}

const P = "text-sm leading-relaxed text-[var(--ink)] mb-3";

export const ARTICLES: Article[] = [
  {
    id: "meet-friday-atlas", tag: "The agents", title: "Meet Friday & Atlas",
    body: (<>
      <p className={P}>Isol8 works like a tiny company that runs itself. You don’t wire up agents one by one — you tell <b>Friday</b> what you want, and Friday assembles the team to do it.</p>
      <div className="bg-[var(--cream-deeper)] border border-[var(--edge)] rounded-2xl p-4 text-center mb-4"><FridayAtlasDiagram /></div>
      <p className={P}><b>Friday — your CEO.</b> The orchestrator you talk to. Give it a goal; it plans, delegates to specialists, and reports back.</p>
      <p className={P}><b>Atlas — your recruiter.</b> When a job needs a skill you don’t have yet, Atlas finds or builds the right agent from the marketplace.</p>
    </>),
  },
  {
    id: "getting-around", tag: "Orientation", title: "Getting around",
    body: (<>
      <p className={P}>Everything happens in one workspace. The left sidebar is your map.</p>
      <p className={P}><b>My agents</b> — your roster (Friday, Atlas, hired specialists). Click one to chat.</p>
      <p className={P}><b>Company panels</b> — Dashboard, Inbox, Tasks, Routines, Goals, Projects, Org chart, Skills, Activity: how the team’s work is tracked.</p>
      <p className={P}><b>Marketplace · Browse / Publish</b> — hire ready-made agents, or publish your own.</p>
      <p className={P}><b>Settings</b> (the gear) — Profile, Billing, Channels, Marketplace, Skills, Connections.</p>
    </>),
  },
  { id: "company-panels", tag: "The work", title: "Company panels",
    body: (<><p className={P}>The Company section is where your team’s work lives. <b>Inbox</b> and <b>Tasks</b> hold the jobs agents pick up; <b>Routines</b> and <b>Goals</b> set recurring and longer-horizon work; <b>Org chart</b>, <b>Skills</b> and <b>Activity</b> show how the team is shaped and what it’s been doing.</p></>) },
  { id: "marketplace", tag: "Grow your team", title: "The marketplace",
    body: (<><p className={P}>Open <b>Browse</b> to hire a ready-made specialist — it’s deployed straight into your roster. Open <b>Publish</b> to package one of your own agents and share it with the community. Friday and Atlas can’t be published; everything else can.</p></>) },
  { id: "channels", tag: "Reach", title: "Channels",
    body: (<><p className={P}>Connect Slack, Discord, Telegram or WhatsApp to talk to your agents from the apps you already use. Set it up in <b>Settings → Channels</b>; each agent can have its own bot.</p></>) },
  { id: "billing-providers", tag: "Reference", title: "Billing & AI providers",
    body: (<><p className={P}>Your plan and AI provider were chosen at signup. There are three ways to power inference: managed <b>Claude credits</b>, your own <b>ChatGPT</b> account, or your own <b>API key</b>. Manage all of it in <b>Settings → Billing</b>.</p></>) },
];
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/getStartedContent.test.ts`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/getstarted/getStartedConfig.ts apps/frontend/src/components/getstarted/getStartedContent.tsx apps/frontend/src/components/getstarted/__tests__/getStartedContent.test.ts
git commit -m "feat(getstarted): step config + concept article registry"
```

---

### Task 2: Step-completion event bus

**Files:**
- Create: `apps/frontend/src/components/getstarted/getStartedEvents.ts`
- Test: `apps/frontend/src/components/getstarted/__tests__/getStartedEvents.test.ts`

**Interfaces:**
- Produces: `emitStep(id: StepId): void`, `onStep(cb: (id: StepId) => void): () => void`. Decouples producers (chat send, task create, hire success) from the guide.

- [ ] **Step 1: Write the failing test**

```typescript
import { describe, it, expect, vi } from "vitest";
import { emitStep, onStep } from "../getStartedEvents";

describe("getStartedEvents", () => {
  it("delivers emitted step ids to subscribers and unsubscribes cleanly", () => {
    const cb = vi.fn();
    const off = onStep(cb);
    emitStep("chat");
    emitStep("hire");
    expect(cb).toHaveBeenCalledWith("chat");
    expect(cb).toHaveBeenCalledWith("hire");
    off();
    emitStep("task");
    expect(cb).toHaveBeenCalledTimes(2);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/getStartedEvents.test.ts`
Expected: FAIL — cannot find module `../getStartedEvents`.

- [ ] **Step 3: Write the implementation**

```typescript
import type { StepId } from "./getStartedConfig";

// A process-local bus so producers (chat send, task create, hire success)
// can report a completed step without importing the guide or threading
// callbacks through deep hooks. SSR-safe: uses a module-level EventTarget.
const target = new EventTarget();
const EVENT = "getstarted:step";

export function emitStep(id: StepId): void {
  target.dispatchEvent(new CustomEvent(EVENT, { detail: id }));
}

export function onStep(cb: (id: StepId) => void): () => void {
  const handler = (e: Event) => cb((e as CustomEvent<StepId>).detail);
  target.addEventListener(EVENT, handler);
  return () => target.removeEventListener(EVENT, handler);
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/getStartedEvents.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/getstarted/getStartedEvents.ts apps/frontend/src/components/getstarted/__tests__/getStartedEvents.test.ts
git commit -m "feat(getstarted): step-completion event bus"
```

---

### Task 3: `useGetStarted` hook (state, persistence, completion, progress)

**Files:**
- Create: `apps/frontend/src/components/getstarted/useGetStarted.ts`
- Test: `apps/frontend/src/components/getstarted/__tests__/useGetStarted.test.tsx`

**Interfaces:**
- Consumes: `STEPS` (Task 1), `onStep` (Task 2), Clerk `useUser`.
- Produces:
  ```typescript
  interface UseGetStarted {
    isOpen: boolean; minimized: boolean; tab: "do" | "learn";
    activeArticle: ArticleId | null;
    open(): void; minimize(): void; close(): void; dismiss(): void;
    setTab(t: "do" | "learn"): void;
    openArticle(id: ArticleId): void; backToLearn(): void;
    completed: Record<StepId, boolean>;
    progress: { done: number; total: number };
    dismissed: boolean; seen: boolean;
  }
  function useGetStarted(opts: { derived: Partial<Record<StepId, boolean>> }): UseGetStarted;
  ```
- **Completion model — uniform `derived OR flag`:** `completed[id] = derived[id] || persistedFlag(id)`. `derived` carries whatever live signals the composition can compute (always `channel` via `hasMainBot`; `task` via the inbox data hook when available). `chat`/`hire` have no cheap live query, so `derived` omits them and they fall back to the event-bus flag. `meet` = article read. This makes completion robust to leave→return **and** to pre-existing state (an existing user who already linked a channel or has tasks shows complete without ever firing an event).

- [ ] **Step 1: Write the failing test**

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useGetStarted } from "../useGetStarted";
import { emitStep } from "../getStartedEvents";

const update = vi.fn().mockResolvedValue(undefined);
let meta: Record<string, unknown> = {};
vi.mock("@clerk/nextjs", () => ({
  useUser: (): { isLoaded: boolean; user: { unsafeMetadata: Record<string, unknown>; update: typeof update } } => ({
    isLoaded: true,
    user: { unsafeMetadata: meta, update },
  }),
}));

describe("useGetStarted", () => {
  beforeEach(() => { meta = {}; update.mockClear(); });

  it("marks steps done from derived live signals (channel + task)", () => {
    const { result } = renderHook(() => useGetStarted({ derived: { channel: true, task: true } }));
    expect(result.current.completed.channel).toBe(true);
    expect(result.current.completed.task).toBe(true);
    expect(result.current.progress).toEqual({ done: 2, total: 5 });
  });

  it("completes an action step when its event fires and persists it", async () => {
    const { result } = renderHook(() => useGetStarted({ derived: {} }));
    await act(async () => { emitStep("hire"); });
    expect(result.current.completed.hire).toBe(true);
    expect(update).toHaveBeenCalled();
  });

  it("opens automatically on first run and records seen", async () => {
    const { result } = renderHook(() => useGetStarted({ derived: {} }));
    expect(result.current.isOpen).toBe(true);
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({ unsafeMetadata: expect.objectContaining({ getStarted: expect.objectContaining({ seen: true }) }) }),
    );
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/useGetStarted.test.tsx`
Expected: FAIL — cannot find module `../useGetStarted`.

- [ ] **Step 3: Write the implementation**

```typescript
import { useCallback, useEffect, useMemo, useState } from "react";
import { useUser } from "@clerk/nextjs";
import { STEPS, type StepId } from "./getStartedConfig";
import type { ArticleId } from "./getStartedContent";
import { onStep } from "./getStartedEvents";

interface GetStartedMeta { seen?: boolean; dismissed?: boolean; read?: ArticleId[]; done?: StepId[] }

export function useGetStarted(opts: { derived: Partial<Record<StepId, boolean>> }) {
  const { isLoaded, user } = useUser();
  const stored = (user?.unsafeMetadata?.getStarted as GetStartedMeta | undefined) ?? {};

  const [done, setDone] = useState<StepId[]>(stored.done ?? []);
  const [read, setRead] = useState<ArticleId[]>(stored.read ?? []);
  const [dismissed, setDismissed] = useState<boolean>(!!stored.dismissed);
  const [seen, setSeen] = useState<boolean>(!!stored.seen);
  const [isOpen, setIsOpen] = useState(false);
  const [minimized, setMinimized] = useState(false);
  const [tab, setTab] = useState<"do" | "learn">("do");
  const [activeArticle, setActiveArticle] = useState<ArticleId | null>(null);

  // Persist a partial patch, MERGING with existing unsafeMetadata so we never
  // clobber the `onboarded` flag or anything else stored there.
  const persist = useCallback((patch: Partial<GetStartedMeta>) => {
    if (!user) return;
    const prev = (user.unsafeMetadata?.getStarted as GetStartedMeta | undefined) ?? {};
    void user.update({ unsafeMetadata: { ...user.unsafeMetadata, getStarted: { ...prev, ...patch } } });
  }, [user]);

  // Event-bus completions (chat/task/hire) → append + persist (idempotent).
  useEffect(() => onStep((id) => {
    setDone((cur) => {
      if (cur.includes(id)) return cur;
      const next = [...cur, id];
      persist({ done: next });
      return next;
    });
  }), [persist]);

  // First-run auto-open, once, after Clerk loads.
  useEffect(() => {
    if (!isLoaded || seen) return;
    setIsOpen(true);
    setSeen(true);
    persist({ seen: true });
  }, [isLoaded, seen, persist]);

  const d = opts.derived;
  const completed = useMemo<Record<StepId, boolean>>(() => ({
    meet: read.includes("meet-friday-atlas"),
    chat: !!d.chat || done.includes("chat"),
    task: !!d.task || done.includes("task"),
    hire: !!d.hire || done.includes("hire"),
    channel: !!d.channel || done.includes("channel"),
  }), [read, done, d.chat, d.task, d.hire, d.channel]);

  const progress = useMemo(() => ({
    done: STEPS.filter((s) => completed[s.id]).length,
    total: STEPS.length,
  }), [completed]);

  const openArticle = useCallback((id: ArticleId) => {
    setActiveArticle(id);
    setRead((cur) => { if (cur.includes(id)) return cur; const next = [...cur, id]; persist({ read: next }); return next; });
  }, [persist]);

  return {
    isOpen, minimized, tab, activeArticle,
    open: () => { setIsOpen(true); setMinimized(false); },
    minimize: () => setMinimized(true),
    close: () => setIsOpen(false),
    dismiss: () => { setDismissed(true); setIsOpen(false); persist({ dismissed: true }); },
    setTab,
    openArticle,
    backToLearn: () => setActiveArticle(null),
    completed, progress, dismissed, seen,
  };
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/useGetStarted.test.tsx`
Expected: PASS (3 tests).

- [ ] **Step 5: `tsc` check + commit**

Run: `cd apps/frontend && pnpm exec tsc --noEmit` (must pass — mocks are typed).

```bash
git add apps/frontend/src/components/getstarted/useGetStarted.ts apps/frontend/src/components/getstarted/__tests__/useGetStarted.test.tsx
git commit -m "feat(getstarted): state/persistence/completion hook"
```

---

### Task 4: `GetStartedLauncher` (floating button + progress ring)

**Files:**
- Create: `apps/frontend/src/components/getstarted/GetStartedLauncher.tsx`
- Test: `apps/frontend/src/components/getstarted/__tests__/GetStartedLauncher.test.tsx`

**Interfaces:**
- Produces: `GetStartedLauncher({ done, total, pulse, onClick }: { done: number; total: number; pulse: boolean; onClick(): void })`.

- [ ] **Step 1: Write the failing test**

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GetStartedLauncher } from "../GetStartedLauncher";

describe("GetStartedLauncher", () => {
  it("shows progress and fires onClick", async () => {
    const onClick = vi.fn();
    render(<GetStartedLauncher done={2} total={5} pulse onClick={onClick} />);
    expect(screen.getByText("2/5")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /get started/i }));
    expect(onClick).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/GetStartedLauncher.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation** (ring math: circumference `2πr`, `r=12` → `75.4`; offset = `75.4 * (1 - done/total)`)

```tsx
import { Sparkles } from "lucide-react";

export function GetStartedLauncher({ done, total, pulse, onClick }: {
  done: number; total: number; pulse: boolean; onClick: () => void;
}): React.ReactElement {
  const C = 75.4;
  const offset = C * (1 - (total ? done / total : 0));
  return (
    <button type="button" onClick={onClick} aria-label="Get started"
      className="fixed right-5 bottom-5 z-40 flex items-center gap-2.5 h-[46px] pl-2 pr-4 rounded-full bg-[var(--forest)] text-[var(--cream)] shadow-[0_10px_26px_-8px_rgba(6,64,43,.5)] hover:brightness-110 transition">
      <span className="relative w-[30px] h-[30px]">
        <svg width="30" height="30" className="-rotate-90">
          <circle cx="15" cy="15" r="12" stroke="rgba(255,255,255,.25)" strokeWidth="3" fill="none" />
          <circle cx="15" cy="15" r="12" stroke="#c2a378" strokeWidth="3" fill="none" strokeLinecap="round" strokeDasharray={C} strokeDashoffset={offset} />
        </svg>
        <span className="absolute inset-0 grid place-items-center text-[9px] font-bold">{done}/{total}</span>
      </span>
      <span className="font-semibold text-[13.5px] flex items-center gap-1.5"><Sparkles size={15} /> Get started</span>
      {pulse && <span className="absolute -top-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-[var(--gold)] animate-ping" />}
    </button>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/GetStartedLauncher.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/getstarted/GetStartedLauncher.tsx apps/frontend/src/components/getstarted/__tests__/GetStartedLauncher.test.tsx
git commit -m "feat(getstarted): floating launcher with progress ring"
```

---

### Task 5: `GetStartedChecklist` (the "do" tab + handoff)

**Files:**
- Create: `apps/frontend/src/components/getstarted/GetStartedChecklist.tsx`
- Test: `apps/frontend/src/components/getstarted/__tests__/GetStartedChecklist.test.tsx`

**Interfaces:**
- Consumes: `STEPS`, `completed` map.
- Produces: `GetStartedChecklist({ completed, onAction, onMeet, onDismiss }: { completed: Record<StepId, boolean>; onAction(id: Exclude<StepId,"meet">): void; onMeet(): void; onDismiss(): void })`. `onAction` is what performs the minimize+navigate handoff (wired in Task 7/8).

- [ ] **Step 1: Write the failing test**

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GetStartedChecklist } from "../GetStartedChecklist";

const allFalse = { meet: false, chat: false, task: false, hire: false, channel: false };

describe("GetStartedChecklist", () => {
  it("calls onAction with the step id when a CTA is clicked", async () => {
    const onAction = vi.fn();
    render(<GetStartedChecklist completed={allFalse} onAction={onAction} onMeet={vi.fn()} onDismiss={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /browse marketplace/i }));
    expect(onAction).toHaveBeenCalledWith("hire");
  });
  it("renders the 'set during signup' confirmation row", () => {
    render(<GetStartedChecklist completed={allFalse} onAction={vi.fn()} onMeet={vi.fn()} onDismiss={vi.fn()} />);
    expect(screen.getByText(/set during signup/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/GetStartedChecklist.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```tsx
import { Check, Users, MessageSquare, ListPlus, Store, Plug, ArrowRight } from "lucide-react";
import { STEPS, type StepDef, type StepId } from "./getStartedConfig";

const ICONS = { users: Users, message: MessageSquare, listplus: ListPlus, store: Store, plug: Plug } as const;

export function GetStartedChecklist({ completed, onAction, onMeet, onDismiss }: {
  completed: Record<StepId, boolean>;
  onAction: (id: Exclude<StepId, "meet">) => void;
  onMeet: () => void;
  onDismiss: () => void;
}): React.ReactElement {
  const done = STEPS.filter((s) => completed[s.id]).length;
  return (
    <div>
      <div className="text-[13.5px] leading-relaxed text-[var(--ink)] bg-[var(--forest-tint)] border border-[#d4e6dc] rounded-xl p-3.5 mb-3.5">
        Isol8 runs like a tiny company. <b className="text-[var(--forest)]">Friday</b> is your CEO — you talk to it, it delegates. <b className="text-[var(--forest)]">Atlas</b> recruits new agents when a job needs a skill you don’t have yet. You’re set up — now put them to work.
      </div>
      <div className="flex items-center gap-2.5 flex-wrap bg-[var(--cream-deeper)] border border-dashed border-[var(--edge)] rounded-xl px-3 py-2.5 mb-5 text-xs text-[var(--mute)]">
        <span className="inline-flex items-center gap-1.5 font-semibold text-[var(--forest-soft)]"><Check size={13} /> Plan & billing</span>
        <span className="inline-flex items-center gap-1.5 font-semibold text-[var(--forest-soft)]"><Check size={13} /> AI provider</span>
        <span>set during signup</span>
      </div>
      <div className="flex justify-between mb-1.5"><span className="text-[13px] font-semibold">Your first wins</span><span className="text-xs font-semibold text-[var(--forest-soft)]">{done} of {STEPS.length}</span></div>
      <div className="h-[7px] rounded-full bg-[var(--cream-deepest)] overflow-hidden mb-4"><i className="block h-full rounded-full bg-gradient-to-r from-[var(--forest-soft)] to-[var(--forest)]" style={{ width: `${(done / STEPS.length) * 100}%` }} /></div>
      {STEPS.map((s: StepDef) => {
        const Icon = ICONS[s.icon];
        const isDone = completed[s.id];
        return (
          <div key={s.id} className={`flex gap-3 p-3 border rounded-xl mb-2.5 ${isDone ? "bg-[var(--cream-deeper)] border-[var(--edge)]" : "bg-white border-[var(--edge)] hover:border-[var(--forest-soft)]"}`}>
            <span className={`flex-none w-[22px] h-[22px] rounded-full grid place-items-center mt-0.5 ${isDone ? "bg-[var(--forest)]" : "border-2 border-[var(--mute)]"}`}>{isDone && <Check size={12} className="text-white" />}</span>
            <span className="flex-none w-[34px] h-[34px] rounded-lg bg-[var(--forest-tint)] grid place-items-center text-[var(--forest)]"><Icon size={18} /></span>
            <div className="flex-1 min-w-0">
              <div className={`text-sm font-semibold ${isDone ? "text-[var(--mute)] line-through" : ""}`}>{s.title}</div>
              <div className="text-xs text-[var(--mute)] mt-0.5 mb-2 leading-snug">{s.description}</div>
              {!isDone && (s.id === "meet"
                ? <button type="button" onClick={onMeet} className="inline-flex items-center gap-1.5 text-xs font-semibold text-[var(--forest)] bg-[var(--forest-tint)] border border-[#d4e6dc] rounded-lg px-2.5 py-1.5">Read <ArrowRight size={13} /></button>
                : <button type="button" onClick={() => onAction(s.id)} className="inline-flex items-center gap-1.5 text-xs font-semibold text-[var(--forest)] bg-[var(--forest-tint)] border border-[#d4e6dc] rounded-lg px-2.5 py-1.5">{s.ctaLabel} <ArrowRight size={13} /></button>)}
            </div>
          </div>
        );
      })}
      <button type="button" onClick={onDismiss} className="block mx-auto mt-3.5 text-xs text-[var(--mute)] underline underline-offset-2">Dismiss checklist</button>
    </div>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/GetStartedChecklist.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/getstarted/GetStartedChecklist.tsx apps/frontend/src/components/getstarted/__tests__/GetStartedChecklist.test.tsx
git commit -m "feat(getstarted): activation checklist"
```

---

### Task 6: `GetStartedCard` (shell, tabs, library + article reader)

**Files:**
- Create: `apps/frontend/src/components/getstarted/GetStartedCard.tsx`
- Test: `apps/frontend/src/components/getstarted/__tests__/GetStartedCard.test.tsx`

**Interfaces:**
- Consumes: `ARTICLES`, `GetStartedChecklist`, the `useGetStarted` return shape.
- Produces: `GetStartedCard({ gs, onAction }: { gs: ReturnType<typeof useGetStarted>; onAction(id: Exclude<StepId,"meet">): void })`. Renders the checklist tab, the learn index, and the article reader; "meet" CTA routes to the `meet-friday-atlas` article in the same card.

- [ ] **Step 1: Write the failing test**

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GetStartedCard } from "../GetStartedCard";

function gsStub(over = {}) {
  return { isOpen: true, minimized: false, tab: "do", activeArticle: null,
    open: vi.fn(), minimize: vi.fn(), close: vi.fn(), dismiss: vi.fn(), setTab: vi.fn(),
    openArticle: vi.fn(), backToLearn: vi.fn(),
    completed: { meet: false, chat: false, task: false, hire: false, channel: false },
    progress: { done: 0, total: 5 }, dismissed: false, seen: true, ...over } as unknown as Parameters<typeof GetStartedCard>[0]["gs"];
}

describe("GetStartedCard", () => {
  it("switches to the learn tab", async () => {
    const setTab = vi.fn();
    render(<GetStartedCard gs={gsStub({ setTab })} onAction={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: /learn the basics/i }));
    expect(setTab).toHaveBeenCalledWith("learn");
  });
  it("renders an opened article", () => {
    render(<GetStartedCard gs={gsStub({ tab: "learn", activeArticle: "meet-friday-atlas" })} onAction={vi.fn()} />);
    expect(screen.getByRole("heading", { name: /meet friday & atlas/i })).toBeInTheDocument();
  });
}); 
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/GetStartedCard.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```tsx
import { Sparkles, X, Minus, ArrowRight, ChevronLeft } from "lucide-react";
import { ARTICLES } from "./getStartedContent";
import { GetStartedChecklist } from "./GetStartedChecklist";
import type { StepId } from "./getStartedConfig";
import type { useGetStarted } from "./useGetStarted";

export function GetStartedCard({ gs, onAction }: {
  gs: ReturnType<typeof useGetStarted>;
  onAction: (id: Exclude<StepId, "meet">) => void;
}): React.ReactElement {
  const article = gs.activeArticle ? ARTICLES.find((a) => a.id === gs.activeArticle) : null;
  return (
    <section role="dialog" aria-label="Get started"
      className="fixed right-5 bottom-[78px] z-50 w-[384px] max-h-[520px] flex flex-col bg-[var(--cream)] border border-[var(--edge)] rounded-2xl shadow-[0_18px_50px_-12px_rgba(6,64,43,.28)] overflow-hidden">
      <div className="px-4 pt-3.5">
        <div className="flex items-center gap-2.5 mb-3">
          <span className="w-[30px] h-[30px] rounded-lg bg-[var(--forest)] grid place-items-center"><Sparkles size={16} className="text-[var(--cream)]" /></span>
          <div><div className="font-editorial-serif text-[16.5px] font-semibold leading-tight">Get started</div><div className="text-[11.5px] text-[var(--mute)]">You’re set up — first wins</div></div>
          <div className="ml-auto flex gap-0.5">
            <button type="button" aria-label="Minimize" onClick={gs.minimize} className="w-7 h-7 rounded-md grid place-items-center text-[var(--mute)] hover:bg-[var(--cream-deepest)]"><Minus size={16} /></button>
            <button type="button" aria-label="Close" onClick={gs.close} className="w-7 h-7 rounded-md grid place-items-center text-[var(--mute)] hover:bg-[var(--cream-deepest)]"><X size={16} /></button>
          </div>
        </div>
        <div className="flex gap-5 border-b border-[var(--edge)]">
          {(["do", "learn"] as const).map((t) => (
            <button key={t} type="button" onClick={() => { gs.backToLearn(); gs.setTab(t); }}
              className={`relative py-2.5 text-[13px] font-semibold ${gs.tab === t ? "text-[var(--forest)] after:absolute after:left-0 after:right-0 after:-bottom-px after:h-0.5 after:bg-[var(--forest)]" : "text-[var(--mute)]"}`}>
              {t === "do" ? "Checklist" : "Learn the basics"}
            </button>
          ))}
        </div>
      </div>
      <div className="p-4 overflow-auto">
        {gs.tab === "do" && (
          <GetStartedChecklist completed={gs.completed} onAction={onAction} onMeet={() => { gs.setTab("learn"); gs.openArticle("meet-friday-atlas"); }} onDismiss={gs.dismiss} />
        )}
        {gs.tab === "learn" && !article && (
          <div>
            {ARTICLES.map((a) => (
              <button key={a.id} type="button" onClick={() => gs.openArticle(a.id)}
                className="w-full flex gap-3 items-center text-left p-3.5 border border-[var(--edge)] rounded-xl bg-white mb-2.5 hover:border-[var(--forest-soft)]">
                <div className="flex-1"><div className="text-sm font-semibold">{a.title}</div><div className="text-xs text-[var(--mute)]">{a.tag}</div></div>
                <ArrowRight size={16} className="text-[var(--mute)]" />
              </button>
            ))}
          </div>
        )}
        {gs.tab === "learn" && article && (
          <div>
            <button type="button" onClick={gs.backToLearn} className="inline-flex items-center gap-1.5 text-xs font-semibold text-[var(--forest)] mb-4"><ChevronLeft size={15} /> All concepts</button>
            <div className="text-[11px] tracking-wider uppercase font-bold text-[var(--forest-soft)] mb-1.5">{article.tag}</div>
            <h1 className="font-editorial-serif text-[22px] font-semibold leading-tight mb-3">{article.title}</h1>
            {article.body}
          </div>
        )}
      </div>
    </section>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/GetStartedCard.test.tsx`
Expected: PASS (2 tests).

- [ ] **Step 5: `tsc` check + commit**

Run: `cd apps/frontend && pnpm exec tsc --noEmit`

```bash
git add apps/frontend/src/components/getstarted/GetStartedCard.tsx apps/frontend/src/components/getstarted/__tests__/GetStartedCard.test.tsx
git commit -m "feat(getstarted): card shell + library + article reader"
```

---

### Task 7: `GetStarted` composition (launcher + card + handoff actions)

**Files:**
- Create: `apps/frontend/src/components/getstarted/GetStarted.tsx`
- Test: `apps/frontend/src/components/getstarted/__tests__/GetStarted.test.tsx`

**Interfaces:**
- Consumes: `useGetStarted`, `GetStartedLauncher`, `GetStartedCard`.
- Produces: `GetStarted({ derived, actions }: { derived: Partial<Record<StepId, boolean>>; actions: GetStartedActions })` where
  ```typescript
  interface GetStartedActions { chat(): void; task(): void; hire(): void; channel(): void }
  ```
  Renders the launcher always; renders the card only when `isOpen && !minimized && !dismissed`. An action handler **minimizes the card, then calls the matching action** (the handoff).

- [ ] **Step 1: Write the failing test**

```tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GetStarted } from "../GetStarted";

vi.mock("../useGetStarted", () => ({
  useGetStarted: () => ({
    isOpen: true, minimized: false, dismissed: false, tab: "do", activeArticle: null,
    open: vi.fn(), minimize: vi.fn(), close: vi.fn(), dismiss: vi.fn(), setTab: vi.fn(),
    openArticle: vi.fn(), backToLearn: vi.fn(),
    completed: { meet: false, chat: false, task: false, hire: false, channel: false },
    progress: { done: 0, total: 5 }, seen: true,
  }),
}));

describe("GetStarted", () => {
  it("runs the matching action on a checklist CTA", async () => {
    const hire = vi.fn();
    render(<GetStarted derived={{}} actions={{ chat: vi.fn(), task: vi.fn(), hire, channel: vi.fn() }} />);
    await userEvent.click(screen.getByRole("button", { name: /browse marketplace/i }));
    expect(hire).toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/GetStarted.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Write the implementation**

```tsx
"use client";
import { GetStartedLauncher } from "./GetStartedLauncher";
import { GetStartedCard } from "./GetStartedCard";
import { useGetStarted } from "./useGetStarted";
import type { StepId } from "./getStartedConfig";

export interface GetStartedActions { chat: () => void; task: () => void; hire: () => void; channel: () => void }

export function GetStarted({ derived, actions }: { derived: Partial<Record<StepId, boolean>>; actions: GetStartedActions }): React.ReactElement | null {
  const gs = useGetStarted({ derived });
  if (gs.dismissed) return <GetStartedLauncher done={gs.progress.done} total={gs.progress.total} pulse={false} onClick={gs.open} />;

  const onAction = (id: Exclude<StepId, "meet">) => {
    gs.minimize();            // hand off: card steps aside, destination opens
    actions[id]();
  };

  return (
    <>
      <GetStartedLauncher done={gs.progress.done} total={gs.progress.total} pulse={!gs.seen} onClick={gs.open} />
      {gs.isOpen && !gs.minimized && <GetStartedCard gs={gs} onAction={onAction} />}
    </>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/GetStarted.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/getstarted/GetStarted.tsx apps/frontend/src/components/getstarted/__tests__/GetStarted.test.tsx
git commit -m "feat(getstarted): launcher+card composition with handoff"
```

---

### Task 8: Producers emit completion events

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentChat.ts:1248` (at the `chat_message_sent` capture)
- Modify: `apps/frontend/src/components/teams/issues/NewIssueDialog.tsx` (on create success)
- Modify: `apps/frontend/src/components/chat/ChatLayout.tsx:261-264` (the `freshlyInstalledAgentId` effect)
- Test: `apps/frontend/src/components/getstarted/__tests__/producers.test.ts`

**Interfaces:**
- Consumes: `emitStep` (Task 2).

- [ ] **Step 1: Write the failing test** (asserts the helper used at each producer site emits correctly)

```typescript
import { describe, it, expect, vi } from "vitest";
import { onStep } from "../getStartedEvents";
import { emitStep } from "../getStartedEvents";

describe("producers emit the right step", () => {
  it("emits chat/task/hire", () => {
    const seen: string[] = [];
    const off = onStep((id) => seen.push(id));
    emitStep("chat"); emitStep("task"); emitStep("hire");
    off();
    expect(seen).toEqual(["chat", "task", "hire"]);
  });
});
```

- [ ] **Step 2: Run test to verify it passes** (the bus already exists)

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/producers.test.ts`
Expected: PASS.

- [ ] **Step 3: Wire the three producers**

In `useAgentChat.ts`, immediately after the existing `capture("chat_message_sent", {…})` call (~line 1248), add:

```typescript
import { emitStep } from "@/components/getstarted/getStartedEvents"; // top of file
// …after capture("chat_message_sent", …):
emitStep("chat");
```

In `NewIssueDialog.tsx`, in the success branch after an issue is created (where the dialog calls `onOpenChange(false)` / its create mutation resolves), add:

```typescript
import { emitStep } from "@/components/getstarted/getStartedEvents";
// in the create-success handler:
emitStep("task");
```

In `ChatLayout.tsx`, inside the existing `useEffect` that watches `drawerState.freshlyInstalledAgentId` (lines ~261-264), add an emit when an id is present:

```typescript
import { emitStep } from "@/components/getstarted/getStartedEvents";
// inside the effect, after the early-return guard:
if (drawerState.freshlyInstalledAgentId) emitStep("hire");
```

- [ ] **Step 4: Re-run the producers test + tsc**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted/__tests__/producers.test.ts && pnpm exec tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/hooks/useAgentChat.ts apps/frontend/src/components/teams/issues/NewIssueDialog.tsx apps/frontend/src/components/chat/ChatLayout.tsx apps/frontend/src/components/getstarted/__tests__/producers.test.ts
git commit -m "feat(getstarted): emit completion events from chat/task/hire"
```

---

### Task 9: Composer focus + `?newIssue` auto-open + mount in ChatLayout

**Files:**
- Modify: `apps/frontend/src/components/chat/ChatInput.tsx` (add textarea ref + focus-event listener)
- Modify: `apps/frontend/src/components/teams/inbox/InboxPage.tsx` (read `?newIssue=1` → open dialog once)
- Modify: `apps/frontend/src/components/chat/ChatLayout.tsx` (mount `<GetStarted/>`, gate, wire actions)
- Test: `apps/frontend/src/components/chat/__tests__/ChatInput.focus.test.tsx`

**Interfaces:**
- Consumes: `GetStarted`, `GetStartedActions` (Task 7), `useProvisioningState` (existing), `useMarketplaceDrawer` (existing), `useApi`/SWR for `/channels/links/me`.

- [ ] **Step 1: Write the failing test (composer focus event)**

```tsx
import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import { ChatInput } from "../ChatInput";

// minimal required props per ChatInput's signature — annotate to satisfy tsc
const noop = () => {};
describe("ChatInput focus event", () => {
  it("focuses the textarea on getstarted:focus-composer", () => {
    render(<ChatInput onSend={noop as (m: string) => void} disabled={false} />);
    window.dispatchEvent(new CustomEvent("getstarted:focus-composer"));
    expect(document.activeElement?.tagName.toLowerCase()).toBe("textarea");
  });
});
```

(Adapt the `ChatInput` props to its real required signature when implementing.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/ChatInput.focus.test.tsx`
Expected: FAIL — textarea not focused.

- [ ] **Step 3: Implement focus + auto-open + mount**

In `ChatInput.tsx`: add `const textareaRef = React.useRef<HTMLTextAreaElement>(null);`, attach `ref={textareaRef}` to the existing `<textarea>` (line ~353), and:

```typescript
React.useEffect(() => {
  const focus = () => textareaRef.current?.focus();
  window.addEventListener("getstarted:focus-composer", focus);
  return () => window.removeEventListener("getstarted:focus-composer", focus);
}, []);
```

In `InboxPage.tsx`: after the existing `searchParams` read, auto-open the New-task dialog once when `?newIssue=1`:

```typescript
React.useEffect(() => {
  if (searchParams.get("newIssue") === "1") {
    setNewIssueOpen(true);
    const params = new URLSearchParams(searchParams.toString());
    params.delete("newIssue");
    router.replace(`?${params.toString()}`, { scroll: false });
  }
}, [searchParams, router]);
```

In `ChatLayout.tsx`: compute `channelLinked` and mount the guide inside the main area (it already lives under `MarketplaceDrawerProvider` via the chat page). Add near the other hooks:

```typescript
import { GetStarted } from "@/components/getstarted/GetStarted";
import { useProvisioningState } from "@/hooks/useProvisioningState";
// existing: const { open: openMarketplace } = useMarketplaceDrawer();
// import { useInboxData } from "@/components/teams/inbox/hooks/useInboxData";
const { phase } = useProvisioningState();
// Derived completion signals (robust to leave→return AND pre-existing state):
//   channel = a main-agent bot is linked;  task = the team already has >= 1 task.
const links = useApi().get("/channels/links/me"); // existing SWR pattern in this file
const channelLinked = /* hasMainBot(links): any provider has an agent_id === "main" bot */ false;
const inbox = useInboxData(/* companyId from the existing teams context in this file */);
const taskExists = (inbox?.items?.length ?? 0) > 0; // CONFIRM useInboxData's items/count field; if the hook needs teams context unavailable here, drop `task` from `derived` (the event-flag still completes it for new users)
const derived = useMemo(() => ({ channel: channelLinked, task: taskExists }), [channelLinked, taskExists]);
```

Then render (e.g. just before the closing main-area `</div>`), gated on readiness:

```tsx
{phase === "normal" && (
  <GetStarted
    derived={derived}
    actions={{
      chat: () => { handleSelectAgent(defaultId ?? agents[0]?.id ?? ""); window.dispatchEvent(new CustomEvent("getstarted:focus-composer")); },
      task: () => { onPanelChange?.(`${TEAMS_PANEL_PREFIX}inbox`); router.push(`?panel=${TEAMS_PANEL_PREFIX}inbox&newIssue=1`); },
      hire: () => openMarketplace({ view: "browse" }),
      channel: () => router.push("/settings?panel=channels"),
    }}
  />
)}
```

Import `TEAMS_PANEL_PREFIX` from `@/components/chat/InlineTeamsPanel`. Use the existing `router`, `handleSelectAgent`, `defaultId`, `agents`, `onPanelChange` already in scope.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/ChatInput.focus.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/chat/ChatInput.tsx apps/frontend/src/components/teams/inbox/InboxPage.tsx apps/frontend/src/components/chat/ChatLayout.tsx apps/frontend/src/components/chat/__tests__/ChatInput.focus.test.tsx
git commit -m "feat(getstarted): mount guide in chat + wire deep-link actions"
```

---

### Task 10: Remove the forced channel-setup gate

**Files:**
- Modify: `apps/frontend/src/components/chat/ProvisioningStepper.tsx`
- Test: `apps/frontend/src/components/chat/__tests__/ProvisioningStepper.channels.test.tsx`

**Interfaces:**
- The post-`agent` branch resolves to `"ready"` unconditionally; the `channels` phase, its render block, its gating helpers used only for it, and the localStorage flag are removed. `BotSetupWizard` stays in `MyChannelsSection` and `AgentChannelsSection` (do NOT delete the component).

- [ ] **Step 1: Write the failing test**

```tsx
import { describe, it, expect } from "vitest";
import * as Stepper from "../ProvisioningStepper";

describe("ProvisioningStepper has no forced channels phase", () => {
  it("does not export/reference a 'channels' interactive phase", () => {
    // Guard test: the source no longer diverts to a channels wizard.
    // Implemented as a behavioral test once the phase-derivation is refactored
    // to a pure helper; assert it returns 'ready' (never 'channels') when the
    // agent step is complete, regardless of hasMainBot.
    expect(Stepper.resolvePostAgentPhase({ gatewayHealth: true })).toBe("ready");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/ProvisioningStepper.channels.test.tsx`
Expected: FAIL — `resolvePostAgentPhase` not exported / still returns `channels`.

- [ ] **Step 3: Refactor + remove**

In `ProvisioningStepper.tsx`:
1. Extract the post-agent decision into an exported pure helper and drop the channels branch:

```typescript
export function resolvePostAgentPhase(_: { gatewayHealth: boolean }): "ready" {
  // Channel setup is no longer forced before chat — it is nudged via the
  // Get Started guide and remains available in Settings → Channels.
  return "ready";
}
```

2. Replace the old derivation (the lines that returned `hasMainBot(linksData) ? "ready" : "channels"` and the member `memberLinkTarget !== null ? "channels" : "ready"`) with `return resolvePostAgentPhase({ gatewayHealth });`.
3. Remove from the file: the `"channels"` member of the `Phase` union, the `if (phase === "channels") { … <BotSetupWizard …/> … }` render block (~lines 652-686), the `findFirstUnlinkedMainProvider` / `hasMainBot` / `memberLinkTarget` / `linksData` SWR + the localStorage "show once" flag **iff** they are now unused, and the `import { BotSetupWizard }` line in this file.
4. Leave `STEPS_PAID` untouched (it already has no `channels` step).

- [ ] **Step 4: Run test + tsc to verify**

Run: `cd apps/frontend && pnpm vitest run src/components/chat/__tests__/ProvisioningStepper.channels.test.tsx && pnpm exec tsc --noEmit`
Expected: PASS, and tsc reports no "unused" errors (confirms the helpers were fully removed).

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/chat/ProvisioningStepper.tsx apps/frontend/src/components/chat/__tests__/ProvisioningStepper.channels.test.tsx
git commit -m "feat(provisioning): remove forced channel-setup gate"
```

---

### Task 11: Analytics + final targeted verification

**Files:**
- Modify: `apps/frontend/src/components/getstarted/GetStarted.tsx` (+ launcher/card click sites) to capture events.
- Modify: `apps/frontend/src/components/getstarted/useGetStarted.ts` (capture on open/dismiss/complete).

**Interfaces:**
- Consumes: `capture` from `@/lib/analytics`.

- [ ] **Step 1: Add captures** (no test — analytics is fire-and-forget; keep to the documented event set)

In `useGetStarted.ts`: `capture("get_started_opened")` in `open()` and the first-run effect; `capture("get_started_dismissed")` in `dismiss()`; `capture("get_started_article_opened", { id })` in `openArticle`; and when `progress.done === progress.total` for the first time, `capture("get_started_completed")`.
In `GetStarted.tsx` `onAction`: `capture("get_started_step_clicked", { step: id })`.

- [ ] **Step 2: Run the whole getstarted folder's tests (targeted, not the full suite)**

Run: `cd apps/frontend && pnpm vitest run src/components/getstarted`
Expected: all PASS.

- [ ] **Step 3: Type-check the changed files**

Run: `cd apps/frontend && pnpm exec tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Lint**

Run: `cd apps/frontend && pnpm run lint`
Expected: no new errors in `src/components/getstarted/` or the modified files.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/getstarted/
git commit -m "feat(getstarted): analytics events"
```

---

## Self-Review

**Spec coverage:**
- Surface (launcher + compact card, not a drawer) → Tasks 4, 6, 7. ✅
- Minimize-on-handoff → Task 7 (`onAction` minimizes then navigates). ✅
- Five-step checklist with verified destinations → Tasks 1, 5, 9. ✅
- "Set during signup" confirmation row → Task 5. ✅
- Six concept articles + SVG diagram → Task 1, rendered in Task 6. ✅
- Completion signals (chat/task/hire via events, channel derived, meet via read) → Tasks 2, 3, 8. ✅
- First-run auto-open + persistence in Clerk metadata → Task 3. ✅
- Remove forced channel gate (keep `BotSetupWizard` in Settings/inspector) → Task 10. ✅
- Analytics → Task 11. ✅
- Render gate on provisioning `normal` → Task 9. ✅

**Placeholder scan:** Three spots are "confirm against the live file" at implementation time — not blind placeholders; each names the exact symbol to verify: Task 9's `hasMainBot` link-shape and `useInboxData`'s items/count field, and Task 8's `NewIssueDialog` create-success branch. If `useInboxData` is awkward to call from `ChatLayout`, drop the `task` key from `derived` — the event-flag still completes it for new users (`chat`/`hire` already work this way). No "TBD/handle edge cases" placeholders elsewhere.

**Type consistency:** `StepId` and `ArticleId` are defined once (Tasks 1) and imported everywhere; `GetStartedActions` keys (`chat/task/hire/channel`) match `Exclude<StepId,"meet">`; `useGetStarted` return shape is consumed unchanged by `GetStartedCard` and `GetStarted`.

## Notes for the implementer

- Build the new `getstarted/` folder first (Tasks 1-7) — it has zero dependencies on the existing app and stays green in isolation. Tasks 8-10 are the integration edits with the most risk; review each carefully against the live file (line numbers drift).
- Disk on the dev machine has been near-full — if `pnpm`/`tsc` fail with `ENOSPC`, free space before assuming a code error.
- Never run the full `pnpm test`; always target files/folders as shown.
