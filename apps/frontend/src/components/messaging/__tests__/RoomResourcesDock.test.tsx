import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { IDockviewPanelProps } from "dockview-react";
import type { LiveEvent, LocalSession, RoomResourceSource, RoomResources as Resources } from "@lightbulb/shared";
import { RoomResources } from "../RoomResources";
import { SessionDockPanel } from "@/components/workspace/panels/SessionDockPanel";

const openSessionPanel = vi.fn();
vi.mock("@/components/workspace/workspaceController", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/workspace/workspaceController")>()),
  isDockAttached: () => true,
  openSessionPanel: (...args: unknown[]) => openSessionPanel(...args),
}));

// SessionDockPanel — mounted only from these tests, so its own useLiveEvents mock lives here.
const dockMessaging = {
  listResources: vi.fn(async (): Promise<Resources> => ({ devices: [], pairedNodes: [], sessions: [] })),
  listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })),
  submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(),
  getRoom: vi.fn(async () => ({ members: [] })),
};
const dockLive = { messaging: dockMessaging, onEvent: (_h: (e: LiveEvent) => void) => () => {}, connectionRevision: 1 };
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => dockLive }));

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

describe("SessionDockPanel", () => {
  function panelProps(params: { roomId: string; sessionId: string }): IDockviewPanelProps<{ roomId: string; sessionId: string }> {
    const api = { setTitle: vi.fn(), onDidParametersChange: () => ({ dispose: () => {} }) };
    return { api, containerApi: {}, params } as unknown as IDockviewPanelProps<{ roomId: string; sessionId: string }>;
  }

  it("resolves an ended session instead of reporting it unavailable", async () => {
    const endedSession: LocalSession = {
      sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2.1.269", generation: "g",
      status: "ended", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
      controlUnavailableReason: null, latestEventSeq: 0, title: "Ended session",
    };
    dockMessaging.listResources.mockResolvedValueOnce({ devices: [], pairedNodes: [], sessions: [endedSession] });
    render(<SessionDockPanel {...panelProps({ roomId: "room", sessionId: "s1" })} />);
    await waitFor(() => expect(screen.getByRole("region", { name: "Native session history" })).toBeInTheDocument());
    expect(dockMessaging.listResources).toHaveBeenCalledWith("room", { includeEnded: true });
    expect(screen.queryByText("Session unavailable in this conversation.")).not.toBeInTheDocument();
  });
});
