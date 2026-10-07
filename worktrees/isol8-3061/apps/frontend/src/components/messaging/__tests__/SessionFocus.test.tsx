import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { RoomResourceSource } from "@lightbulb/shared";
import { SessionFocus } from "../SessionFocus";

const session = { sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude" as const, providerVersion: "2.1.269", generation: "g", status: "busy" as const, activeTurnId: null,
  capabilities: { sendMode: "queue" as const, steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 0, title: "Load test" };
const client = { listResources: vi.fn(async () => ({ devices: [], pairedNodes: [], sessions: [session] })), listWorkspaceSessions: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() } as unknown as RoomResourceSource["client"];
const source: RoomResourceSource = { client, connectionRevision: 1, onEvent: () => () => {} };

it("renders the session in the middle and the conversation on the right for someone else's session", async () => {
  render(<SessionFocus focus={{ sessionId: "s1", ownerUserId: "alice", ownerName: "Alice", roomId: "dm-1", own: false }} source={source} onClose={vi.fn()} conversation={<section aria-label="Messages" />} scope="org" />);
  expect(await screen.findByRole("heading", { name: /Load test/ })).toBeInTheDocument();
  expect(screen.getByRole("complementary", { name: "Conversation" })).toContainElement(screen.getByRole("region", { name: "Messages" }));
});

it("renders only the session for your own session and closes", async () => {
  const onClose = vi.fn();
  render(<SessionFocus focus={{ sessionId: "s1", ownerUserId: "me", ownerName: "You", roomId: "general", own: true }} source={source} onClose={onClose} conversation={<section aria-label="Messages" />} scope="org" />);
  expect(await screen.findByRole("heading", { name: /Load test/ })).toBeInTheDocument();
  expect(screen.queryByRole("complementary", { name: "Conversation" })).not.toBeInTheDocument();
  screen.getByRole("button", { name: "Close session" }).click();
  expect(onClose).toHaveBeenCalled();
});

it("says 'your Mac' and 'your terminal', never \"You's\", for your own waiting session", async () => {
  const waiting = { ...session, status: "waiting-approval" as const };
  const waitingClient = { listResources: vi.fn(async () => ({ devices: [], pairedNodes: [], sessions: [waiting] })), listWorkspaceSessions: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() } as unknown as RoomResourceSource["client"];
  const waitingSource: RoomResourceSource = { client: waitingClient, connectionRevision: 1, onEvent: () => () => {} };
  render(<SessionFocus focus={{ sessionId: "s1", ownerUserId: "me", ownerName: "You", roomId: "general", own: true }} source={waitingSource} onClose={vi.fn()} conversation={<section aria-label="Messages" />} scope="org" />);
  expect(await screen.findByText("You · Waiting for input on your Mac")).toBeInTheDocument();
  expect(screen.getByText("Follow-ups queue on your Mac. Approvals stay in your terminal.")).toBeInTheDocument();
});
