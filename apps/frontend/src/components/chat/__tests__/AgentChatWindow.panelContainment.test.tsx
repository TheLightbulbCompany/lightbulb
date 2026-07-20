// @vitest-environment jsdom
/**
 * Panel containment: every AgentChatWindow render branch must be a BOUNDED
 * flex column.
 *
 * The window renders into a dock slot that is height-bounded and clips
 * (`.workspace-shell { overflow: clip }`). A flex item defaults to
 * `min-height: auto`, so if ANY element on the chain from the panel root down
 * to the scroll container is missing `min-h-0`, that element grows to full
 * transcript height: the message area stops being a scrollport and everything
 * below it — crucially ChatInput — is pushed past the clip boundary, where it
 * is both invisible and unreachable (a clipped box refuses to scroll). The app
 * looks frozen; only a reload recovers.
 *
 * This shipped twice from the same cause: two early-return branches
 * (`chatError` and `isInitialState`) hand-rolled their own column and each
 * omitted `min-h-0`, while the branches beside them had it. The `chatError`
 * branch is now deleted outright (errors render as transcript bubbles), but
 * the invariant needs a guard that covers every branch, including future ones.
 *
 * jsdom does no layout, so — like MessageList.scrollContainment.test.tsx spying
 * on scrollIntoView rather than measuring pixels — this asserts the structural
 * invariant that produces the layout.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { AgentChatWindow } from "../AgentChatWindow";
import type { RenderGroup } from "@/hooks/internal/types";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({
    getToken: vi.fn().mockResolvedValue("test-token"),
    isSignedIn: true,
    userId: "user_1",
  }),
  useUser: () => ({ isSignedIn: true, isLoaded: true, user: { id: "user_1" } }),
  useOrganization: () => ({ organization: null, membership: null, isLoaded: true }),
}));

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    isConnected: true,
    onChatMessage: () => () => {},
    onEvent: () => () => {},
  }),
}));

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [{ id: "a1", name: "Friday" }] }),
  agentDisplayName: (a: { name?: string } | null) => a?.name ?? "Agent",
}));

vi.mock("../ExecApprovalCards", () => ({ ExecApprovalCards: () => null }));

const baseChat = {
  renderGroups: [] as RenderGroup[],
  renderItems: [],
  isStreaming: false,
  sendMessage: vi.fn(),
  cancelMessage: vi.fn(),
  clearMessages: vi.fn(),
  isConnected: true,
  needsBootstrap: false,
  queuedMessages: [],
  removeQueuedMessage: vi.fn(),
  clearQueue: vi.fn(),
  steerMessage: vi.fn(),
  retryQueuedMessage: vi.fn(),
};

let chatState: Record<string, unknown> = {};
vi.mock("@/hooks/useAgentChat", () => ({
  useAgentChat: () => ({ ...baseChat, ...chatState }),
  BOOTSTRAP_MESSAGE: "bootstrap",
}));

function transcript(text: string): RenderGroup[] {
  return [
    {
      side: "assistant",
      key: "g1",
      items: [{ kind: "message", key: "g1:m", message: { role: "assistant", text } }],
    },
  ];
}

/**
 * Walk from the scroll container up to the panel root. Every ancestor on that
 * chain is a flex item in a column and must carry `min-h-0`, or it will size
 * itself to its content instead of to the bounded slot.
 */
function assertBoundedColumnChain(root: HTMLElement) {
  const scroller = root.querySelector<HTMLElement>(
    ".overflow-y-auto, .overflow-hidden",
  );
  expect(
    scroller,
    "expected a scroll/clip container in the panel — without one the transcript sizes the panel",
  ).not.toBeNull();

  const offenders: string[] = [];
  // Start above the scroller: the scroller itself is allowed to be the
  // overflow boundary, but everything between it and the panel root must be
  // explicitly shrinkable.
  let el = scroller!.parentElement;
  while (el && el !== root.parentElement) {
    const cls = el.className;
    if (typeof cls === "string" && cls.includes("flex-col") && !cls.includes("min-h-0")) {
      offenders.push(cls);
    }
    el = el.parentElement;
  }

  expect(
    offenders,
    `flex-column ancestor(s) missing min-h-0 — the composer will be pushed past the ` +
      `dock panel's clip boundary and become unreachable:\n  ${offenders.join("\n  ")}`,
  ).toEqual([]);

  // Second route to the same "present but unreachable" failure: a scrollport
  // that centers on its main axis with UNSAFE alignment. `justify-content:
  // center` splits the overflow across both ends, so the top is clipped and
  // scrollHeight never covers it (measured: scrollHeight 300 for 400px of
  // content, top pinned at -100px). `justify-center-safe` falls back to
  // flex-start once it overflows.
  const unsafeCentered = Array.from(
    root.querySelectorAll<HTMLElement>(".overflow-y-auto, .overflow-hidden"),
  )
    .map((el) => (typeof el.className === "string" ? el.className : ""))
    .filter((cls) => cls.includes("flex-col") && /(^|\s)justify-center(\s|$)/.test(cls));

  expect(
    unsafeCentered,
    `scroll container(s) using unsafe justify-center — content above the fold ` +
      `becomes unreachable when it overflows. Use justify-center-safe:\n  ${unsafeCentered.join("\n  ")}`,
  ).toEqual([]);
}

describe("AgentChatWindow — panel containment", () => {
  it("keeps the composer inside a bounded column in the empty/initial state", () => {
    chatState = { renderGroups: [], isLoadingHistory: false };
    const { container } = render(<AgentChatWindow agentId="a1" />);

    expect(screen.getByRole("textbox")).toBeInTheDocument();
    assertBoundedColumnChain(container.firstElementChild as HTMLElement);
  });

  it("keeps the composer inside a bounded column while history loads", () => {
    chatState = { renderGroups: [], isLoadingHistory: true };
    const { container } = render(<AgentChatWindow agentId="a1" />);

    expect(screen.getByRole("textbox")).toBeInTheDocument();
    assertBoundedColumnChain(container.firstElementChild as HTMLElement);
  });

  it("keeps the composer inside a bounded column with a transcript", () => {
    chatState = { renderGroups: transcript("hello"), isLoadingHistory: false };
    const { container } = render(<AgentChatWindow agentId="a1" />);

    expect(screen.getByRole("textbox")).toBeInTheDocument();
    assertBoundedColumnChain(container.firstElementChild as HTMLElement);
  });

  it("still renders the composer when the transcript contains an error", () => {
    // Regression: an error must not swap the panel into a different layout.
    // The old `if (chatError)` branch did exactly that and lost the composer.
    chatState = {
      renderGroups: transcript("Error: Gateway connection lost"),
      isLoadingHistory: false,
    };
    const { container } = render(<AgentChatWindow agentId="a1" />);

    expect(screen.getByRole("textbox")).toBeEnabled();
    // No separate full-width error banner — errors live in the transcript.
    expect(screen.queryByText("Error", { selector: "p" })).toBeNull();
    assertBoundedColumnChain(container.firstElementChild as HTMLElement);
  });
});
