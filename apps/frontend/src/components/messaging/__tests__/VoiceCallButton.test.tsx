import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const start = vi.fn(async () => {});
const hangUp = vi.fn(async () => {});
vi.mock("@lightbulb/shared", async (orig) => ({
  ...(await orig<object>()),
  VoiceCallSession: vi.fn().mockImplementation(() => ({ start, hangUp, subscribe: (l: (v: unknown) => void) => { l({ state: "live", deadline: Date.now() + 120_000, working: "browser", error: null }); return () => {}; } })),
}));
vi.mock("@/lib/api", () => ({ useApi: () => ({ post: vi.fn() }) }));
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => ({ onEvent: () => () => {} }) }));

import { VoiceCallButton } from "../VoiceCallButton";

describe("VoiceCallButton", () => {
  it("starts a call and shows progress and hang up", async () => {
    render(<VoiceCallButton agentId="agent-1" agentName="Finance" />);
    fireEvent.click(screen.getByRole("button", { name: "Call Finance" }));
    expect(start).toHaveBeenCalledWith("agent-1");
    expect(await screen.findByText("Finance is working: browser")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Hang up" }));
    expect(hangUp).toHaveBeenCalled();
  });
});
