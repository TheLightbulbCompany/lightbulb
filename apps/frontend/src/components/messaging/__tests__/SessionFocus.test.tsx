import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ApiError, type LocalSession, type SessionSource } from "@lightbulb/shared";
import { SessionFocus } from "../SessionFocus";

const session: LocalSession = { sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2.1.269", generation: "g", status: "busy", activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 0, title: "Load test" };
const resolveSessionOwner = () => ({ ownerName: "Alice", own: false });

function focus(entry: { session: LocalSession; ownerName: string; own: boolean } | null, { onClose = vi.fn(), loading = false, getLocalSession }: {
  onClose?: () => void; loading?: boolean; getLocalSession?: SessionSource["client"]["getLocalSession"];
} = {}) {
  const client = {
    listWorkspaceSessions: vi.fn(),
    getLocalSession: getLocalSession ?? vi.fn(async () => { throw new ApiError(404, null); }),
    listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })),
    submitLocalSessionCommand: vi.fn(),
    getLocalSessionCommand: vi.fn(),
  } as unknown as SessionSource["client"];
  return render(<SessionFocus focus={{ sessionId: "s1" }} entries={entry ? [entry] : []} loading={loading} hintRevision={0} client={client}
    resolveSessionOwner={resolveSessionOwner} onAccessLost={vi.fn()} onClose={onClose} />);
}

it("renders someone else's session alone, read-only, with no conversation column", async () => {
  focus({ session, ownerName: "Alice", own: false });
  expect(await screen.findByRole("heading", { name: /Load test/ })).toBeInTheDocument();
  expect(screen.queryByRole("complementary", { name: "Conversation" })).not.toBeInTheDocument();
  expect(screen.getByText("Only Alice can send to this session.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Send follow-up" })).not.toBeInTheDocument();
});

it("renders your own session with its composer and closes", async () => {
  const onClose = vi.fn();
  focus({ session, ownerName: "You", own: true }, { onClose });
  expect(await screen.findByRole("heading", { name: /Load test/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Send follow-up" })).toBeInTheDocument();
  screen.getByRole("button", { name: "Close session" }).click();
  expect(onClose).toHaveBeenCalled();
});

it("falls back to a direct fetch when the workspace list lacks the session, and shows a 404 as no longer shared", async () => {
  focus(null);
  expect(await screen.findByText("This session is no longer shared.")).toBeInTheDocument();
});

it("shows 'Loading session…' while the workspace list has not yet settled, without fetching directly", () => {
  const getLocalSession = vi.fn();
  focus(null, { loading: true, getLocalSession });
  expect(screen.getByText("Loading session…")).toBeInTheDocument();
  expect(getLocalSession).not.toHaveBeenCalled();
});

it("falls back to a direct fetch and renders an ended session with its real status and no composer", async () => {
  const ended: LocalSession = { ...session, status: "ended" };
  focus(null, { getLocalSession: vi.fn(async () => ended) });
  expect(await screen.findByText("Alice · Ended")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Send follow-up" })).not.toBeInTheDocument();
  expect(screen.queryByText("Only Alice can send to this session.")).not.toBeInTheDocument();
});

it("says 'your Mac' and 'your terminal', never \"You's\", for your own waiting session", async () => {
  focus({ session: { ...session, status: "waiting-approval" }, ownerName: "You", own: true });
  expect(await screen.findByText("You · Waiting for input on your Mac")).toBeInTheDocument();
  expect(screen.getByText("Follow-ups queue on your Mac. Approvals stay in your terminal.")).toBeInTheDocument();
});
