import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { LiveEvent, RoomResourceSource, RoomResources as Resources } from "@lightbulb/shared";
import { RoomResources } from "../RoomResources";

const openSessionPanel = vi.fn();
vi.mock("@/components/workspace/workspaceController", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/workspace/workspaceController")>()),
  isDockAttached: () => true,
  openSessionPanel: (...args: unknown[]) => openSessionPanel(...args),
}));

it("opens a session in the dock instead of the pane when the workspace shell is attached", async () => {
  const resources: Resources = {
    devices: [], pairedNodes: [],
    sessions: [{ sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2.1.269", generation: "g",
      status: "idle", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
      controlUnavailableReason: null, latestEventSeq: 0, title: "Load test" }],
  };
  const client = { listResources: vi.fn(async () => resources), listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() } as unknown as RoomResourceSource["client"];
  const source: RoomResourceSource = { client, connectionRevision: 1, onEvent: (_handler: (event: LiveEvent) => void) => () => {} };
  render(<RoomResources roomId="room" source={source} onClose={() => {}}
    participants={[{ kind: "human", id: "alice", displayName: "Alice", mentionHandle: null, role: "member", listenMode: null, joinedAt: "now", joinedSeq: 0 }]} />);
  await userEvent.click(await screen.findByRole("button", { name: "Open Claude · Load test" }));
  expect(openSessionPanel).toHaveBeenCalledWith("room", "s1");
  expect(screen.queryByRole("region", { name: "Native session history" })).not.toBeInTheDocument();
});
