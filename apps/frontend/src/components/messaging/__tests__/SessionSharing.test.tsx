import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { LocalSession } from "@lightbulb/shared";
import { SessionMenu } from "../LocalSessionPanel";

const session: LocalSession = { sessionId: "session", deviceId: "mac", humanUserId: "alice", provider: "claude", generation: "g1", status: "idle", providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null };

async function choose(name: string) {
  await userEvent.click(screen.getByRole("button", { name: "Session options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name }));
}

describe("per-session sharing", () => {
  it("lets the owner stop sharing one session, then resume it", async () => {
    const client = { setLocalSessionSharing: vi.fn(async () => session) };
    const { rerender } = render(<SessionMenu session={session} ownerName="Alice" own client={client} />);
    await choose("Stop sharing this session");
    expect(client.setLocalSessionSharing).toHaveBeenCalledWith("session", false);
    expect(await screen.findByText("Sharing paused. Only you can see this session.")).toBeInTheDocument();
    rerender(<SessionMenu session={{ ...session, shared: false }} ownerName="Alice" own client={client} />);
    await choose("Resume sharing");
    expect(client.setLocalSessionSharing).toHaveBeenLastCalledWith("session", true);
  });

  it("never offers it to a teammate", async () => {
    render(<SessionMenu session={session} ownerName="Alice" own={false} client={{ setLocalSessionSharing: vi.fn() }} />);
    await userEvent.click(screen.getByRole("button", { name: "Session options" }));
    await screen.findByRole("menuitem", { name: "About this session" });
    expect(screen.queryByRole("menuitem", { name: "Stop sharing this session" })).toBeNull();
  });
});
