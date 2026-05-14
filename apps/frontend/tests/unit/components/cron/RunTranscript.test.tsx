import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

// State the mock holds between tests; test bodies tweak before rendering.
const rpcState: {
  data: unknown;
  error: Error | null;
  isLoading: boolean;
  mutate: ReturnType<typeof vi.fn>;
} = {
  data: undefined,
  error: null,
  isLoading: false,
  mutate: vi.fn(),
};

vi.mock("@/hooks/useGatewayRpc", () => ({
  useGatewayRpc: () => rpcState,
  useGatewayRpcMutation: () => vi.fn(),
}));

import {
  RunTranscript,
  firstUserMessage,
  scopeMessagesToRun,
} from "@/components/control/panels/cron/RunTranscript";
import type { HistoryMessage } from "@/hooks/internal/types";

beforeEach(() => {
  rpcState.data = undefined;
  rpcState.error = null;
  rpcState.isLoading = false;
  rpcState.mutate = vi.fn();
});

describe("RunTranscript", () => {
  it("renders a 'no transcript' message when sessionKey is undefined", () => {
    render(<RunTranscript sessionKey={undefined} />);
    expect(screen.getByText(/no transcript available/i)).toBeInTheDocument();
  });

  it("renders transcript text when RPC returns messages", () => {
    rpcState.data = {
      messages: [
        { role: "user", content: [{ type: "text", text: "What's the weather?" }] },
        { role: "assistant", content: [{ type: "text", text: "Sunny and warm." }] },
      ],
    };
    render(<RunTranscript sessionKey="session-abc" />);
    expect(screen.getByText("What's the weather?")).toBeInTheDocument();
    expect(screen.getByText("Sunny and warm.")).toBeInTheDocument();
  });

  it("renders a loading state when isLoading is true", () => {
    rpcState.isLoading = true;
    render(<RunTranscript sessionKey="session-abc" />);
    expect(screen.getByText(/loading transcript/i)).toBeInTheDocument();
  });

  it("renders an error banner with a Retry button when RPC errors", () => {
    rpcState.error = new Error("boom");
    render(<RunTranscript sessionKey="session-abc" />);
    expect(screen.getByText(/transcript unavailable/i)).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: /retry/i });
    fireEvent.click(retry);
    expect(rpcState.mutate).toHaveBeenCalledTimes(1);
  });

  it("falls back to 'no transcript' when RPC returns zero messages", () => {
    rpcState.data = { messages: [] };
    render(<RunTranscript sessionKey="session-abc" />);
    expect(screen.getByText(/no transcript available/i)).toBeInTheDocument();
  });

  it("filters out messages outside [afterTs, beforeTs] when scoping to a run", () => {
    // Three messages across two runs on a shared (non-isolated) session.
    // Only the middle pair falls inside this run's [afterTs, beforeTs] window.
    rpcState.data = {
      messages: [
        {
          role: "user",
          content: [{ type: "text", text: "previous-run prompt" }],
          timestamp: 500,
        },
        {
          role: "user",
          content: [{ type: "text", text: "in-range prompt" }],
          timestamp: 100_200,
        },
        {
          role: "assistant",
          content: [{ type: "text", text: "in-range answer" }],
          timestamp: 100_800,
        },
        {
          role: "user",
          content: [{ type: "text", text: "next-run prompt" }],
          timestamp: 200_000,
        },
      ],
    };
    render(
      <RunTranscript
        sessionKey="session-abc"
        afterTs={100_000}
        beforeTs={101_000}
      />,
    );
    expect(screen.getByText("in-range prompt")).toBeInTheDocument();
    expect(screen.getByText("in-range answer")).toBeInTheDocument();
    expect(screen.queryByText("previous-run prompt")).toBeNull();
    expect(screen.queryByText("next-run prompt")).toBeNull();
  });
});

describe("scopeMessagesToRun", () => {
  const messages: HistoryMessage[] = [
    { role: "user", text: "before window", timestamp: 500 },
    { role: "user", text: "in window", timestamp: 100_500 },
    { role: "assistant", text: "also in window", timestamp: 100_800 },
    { role: "user", text: "after window", timestamp: 200_000 },
    // Conservative: messages with no timestamp are kept (older history formats).
    { role: "user", text: "no-ts message" },
  ];

  it("returns all messages unchanged when both bounds are undefined", () => {
    expect(scopeMessagesToRun(messages)).toEqual(messages);
  });

  it("filters strictly to [afterTs, beforeTs] while preserving ts-less messages", () => {
    const scoped = scopeMessagesToRun(messages, 100_000, 101_000);
    const contents = scoped.map((m) => m.text);
    expect(contents).toContain("in window");
    expect(contents).toContain("also in window");
    expect(contents).toContain("no-ts message"); // conservative keep
    expect(contents).not.toContain("before window");
    expect(contents).not.toContain("after window");
  });

  it("applies only the lower bound when beforeTs is undefined", () => {
    const scoped = scopeMessagesToRun(messages, 100_000);
    const contents = scoped.map((m) => m.text);
    expect(contents).toContain("in window");
    expect(contents).toContain("after window");
    expect(contents).toContain("no-ts message");
    expect(contents).not.toContain("before window");
  });

  it("applies only the upper bound when afterTs is undefined", () => {
    const scoped = scopeMessagesToRun(messages, undefined, 101_000);
    const contents = scoped.map((m) => m.text);
    expect(contents).toContain("before window");
    expect(contents).toContain("in window");
    expect(contents).toContain("no-ts message");
    expect(contents).not.toContain("after window");
  });
});

describe("firstUserMessage", () => {
  it("returns the first user message overall when no afterTs is provided", () => {
    const messages: HistoryMessage[] = [
      { role: "user", text: "first prompt", timestamp: 1_000 },
      { role: "assistant", text: "ack", timestamp: 2_000 },
      { role: "user", text: "second prompt", timestamp: 3_000 },
    ];
    expect(firstUserMessage(messages)).toBe("first prompt");
  });

  it("skips user messages older than afterTs in multi-run sessions", () => {
    const messages: HistoryMessage[] = [
      // Old run: well before the cutoff.
      { role: "user", text: "old prompt", timestamp: 1_000 },
      { role: "assistant", text: "old answer", timestamp: 1_500 },
      // Current run: at the cutoff.
      { role: "user", text: "current prompt", timestamp: 100_000 },
      { role: "assistant", text: "current answer", timestamp: 100_500 },
    ];
    expect(firstUserMessage(messages, 100_000)).toBe("current prompt");
  });

  it("returns undefined (not earliest) when no message has timestamp and afterTs is provided", () => {
    // Tighter semantics: when afterTs is provided but no message has a
    // timestamp satisfying the bound, we deliberately return undefined
    // rather than falling back to the first user message overall. The old
    // fallback was too permissive in shared-session reruns.
    const messages: HistoryMessage[] = [
      { role: "user", text: "no-ts prompt" },
      { role: "assistant", text: "answer" },
    ];
    expect(firstUserMessage(messages, 100_000)).toBeUndefined();
  });

  it("returns undefined when no message is at-or-after afterTs", () => {
    const messages: HistoryMessage[] = [
      { role: "user", text: "stale prompt", timestamp: 1_000 },
      { role: "assistant", text: "stale answer", timestamp: 1_500 },
    ];
    expect(firstUserMessage(messages, 100_000)).toBeUndefined();
  });

  it("rejects messages just before afterTs (no tolerance window)", () => {
    // With the old 5s tolerance this would have matched. With the tighter
    // semantics it must not — back-to-back manual reruns can otherwise
    // surface the previous run's prompt.
    const messages: HistoryMessage[] = [
      { role: "user", text: "previous run prompt", timestamp: 99_999 },
      { role: "user", text: "current run prompt", timestamp: 100_000 },
    ];
    expect(firstUserMessage(messages, 100_000)).toBe("current run prompt");
  });

  it("excludes messages after beforeTs upper bound", () => {
    const messages: HistoryMessage[] = [
      { role: "user", text: "in-range prompt", timestamp: 100_500 },
      { role: "user", text: "out-of-range prompt", timestamp: 200_000 },
    ];
    // afterTs=100_000, beforeTs=101_000 → only the in-range prompt qualifies.
    expect(firstUserMessage(messages, 100_000, 101_000)).toBe("in-range prompt");
  });

  it("returns undefined when only matches are after beforeTs", () => {
    const messages: HistoryMessage[] = [
      { role: "user", text: "next-run prompt", timestamp: 200_000 },
    ];
    expect(firstUserMessage(messages, 100_000, 101_000)).toBeUndefined();
  });
});
