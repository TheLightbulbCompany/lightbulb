import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";

vi.mock("@/components/teams/inbox/hooks/useAttentionFeed", () => ({ useAttentionFeed: vi.fn() }));
vi.mock("@/lib/shellNav", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/lib/shellNav")>();
  return { ...mod, handleShellLinkClick: vi.fn() };
});

import { HomeNeedsYou, needsYouSub, needsYouTitle } from "../HomeNeedsYou";
import { useAttentionFeed } from "@/components/teams/inbox/hooks/useAttentionFeed";
import type { AttentionItem } from "@/components/teams/shared/types";

const item = (over: Partial<AttentionItem>): AttentionItem => ({
  id: "a1",
  sourceKind: "approval",
  subject: { kind: "approval", id: "ap1", title: "Hire Pulse as an SDR", identifier: null, status: null, href: null },
  whyNow: "adds a seat at $20/mo",
  severity: "high",
  activityAt: "2026-09-05T17:40:00Z",
  createdAt: "2026-09-05T17:40:00Z",
  relatedIssue: { kind: "issue", id: "iss1", title: null, identifier: "LUL-10", status: null, href: null },
  originAgentName: "Atlas",
  ...over,
});

function feed(over: Partial<ReturnType<typeof useAttentionFeed>>) {
  vi.mocked(useAttentionFeed).mockReturnValue({
    items: [], count: 0, isLoading: false, isError: false, refresh: vi.fn(), ...over,
  });
}

describe("HomeNeedsYou", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows at most three decisions and the company-wide count on the link", () => {
    feed({ items: [item({ id: "1" }), item({ id: "2" }), item({ id: "3" }), item({ id: "4", subject: { kind: "approval", id: "x", title: "Fourth", identifier: null, status: null, href: null } })], count: 7 });
    render(<HomeNeedsYou />);
    const section = screen.getByRole("region", { name: "Needs you" });
    expect(within(section).getAllByRole("link", { name: /Hire Pulse/ })).toHaveLength(3);
    expect(within(section).queryByText("Fourth")).not.toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Open Inbox · 7" })).toHaveAttribute("href", expect.stringContaining("teams-inbox"));
  });

  it("links a decision to its task when there is one, else to the Inbox", () => {
    feed({ items: [item({ id: "1" }), item({ id: "2", relatedIssue: null })], count: 2 });
    render(<HomeNeedsYou />);
    const rows = screen.getAllByRole("link", { name: /Hire Pulse/ });
    expect(rows[0]).toHaveAttribute("href", expect.stringContaining("iss1"));
    expect(rows[1]).toHaveAttribute("href", expect.stringContaining("teams-inbox"));
  });

  it("says nothing is waiting, calmly, when the feed is empty", () => {
    feed({});
    render(<HomeNeedsYou />);
    expect(screen.getByRole("status")).toHaveTextContent("Nothing is waiting on you.");
    expect(screen.getByRole("link", { name: "Open Inbox" })).toBeInTheDocument();
  });

  it("never renders a count it has not read", () => {
    feed({ isLoading: true });
    render(<HomeNeedsYou />);
    expect(screen.getByRole("status")).toHaveTextContent("Checking what needs you…");
    expect(screen.queryByText(/Open Inbox ·/)).not.toBeInTheDocument();
  });

  it("reports a read failure as unavailable, not as empty", () => {
    feed({ isError: true });
    render(<HomeNeedsYou />);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't read what needs you just now.");
  });

  it("titles a row from the subject, falling back to the kind, and builds the sub line", () => {
    expect(needsYouTitle(item({}))).toBe("Hire Pulse as an SDR");
    expect(needsYouTitle(item({ subject: { kind: "x", id: "y", title: "  ", identifier: null, status: null, href: null }, sourceKind: "recovery_action" }))).toBe("Recovery");
    expect(needsYouSub(item({}))).toBe("Atlas · adds a seat at $20/mo");
    expect(needsYouSub(item({ originAgentName: null }))).toBe("adds a seat at $20/mo");
  });
});
