import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { SWRConfig } from "swr";

const mockGet = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: mockGet, post: vi.fn() }) }));

import { SessionsTimeline } from "../SessionsTimeline";

const row = (id: string, computer: string, started: string) => ({
  id, harness: "claude-code", computer, repo: "lightbulb", branch: "main", started,
  ended: "2026-10-27T11:30:00Z", prompt_tokens: 1200, completion_tokens: 300,
});

describe("SessionsTimeline", () => {
  it("renders sessions grouped by machine", async () => {
    mockGet.mockResolvedValue({
      sessions: [row("a", "mac", "2026-10-27T10:00:00Z"), row("b", "linux-box", "2026-10-26T10:00:00Z"), row("c", "mac", "2026-10-25T10:00:00Z")],
    });
    render(<SWRConfig value={{ provider: () => new Map() }}><SessionsTimeline /></SWRConfig>);
    const mac = (await screen.findByRole("heading", { name: "mac" })).closest("section")!;
    expect(within(mac).getAllByRole("listitem")).toHaveLength(2);
    const linux = screen.getByRole("heading", { name: "linux-box" }).closest("section")!;
    expect(within(linux).getAllByRole("listitem")).toHaveLength(1);
    expect(within(mac).getAllByText("1,200 prompt tokens")).toHaveLength(2);
    expect(within(mac).getAllByRole("button", { name: "Share" })).toHaveLength(2);
    expect(mockGet).toHaveBeenCalledWith("/sessions");
  });
});
