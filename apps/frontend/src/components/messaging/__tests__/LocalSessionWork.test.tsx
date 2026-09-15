import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import { LocalSessionPanel } from "../LocalSessionPanel";
import type { LocalSession } from "@lightbulb/shared";

const m = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn(), userId: "bob", orgId: "org", open: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: m.userId, orgId: m.orgId, isLoaded: true }) }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: m.get, patch: m.patch }) }));
vi.mock("@/components/workspace/workspaceController", () => ({ openPanel: m.open }));
const linked = { state: "linked", revision: "rev1", link: { kind: "issue", companyId: "company", issueId: "issue" }, title: "Private synthetic title" };
const session: LocalSession = { sessionId: "session", deviceId: "mac", humanUserId: "alice", provider: "codex", generation: "g1", status: "idle", providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null };
const client = { listResources: vi.fn(), listWorkspaceSessions: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() };
function view() {
  const cache = new Map();
  const tree = () => <SWRConfig value={{ provider: () => cache }}><LocalSessionPanel session={session} ownerName="Alice" own={false} roomId="room" client={client} revision={1} onAccessLost={() => {}} /></SWRConfig>;
  return { ...render(tree()), tree };
}
beforeEach(() => {
  vi.clearAllMocks(); m.userId = "bob"; m.orgId = "org";
  m.get.mockResolvedValue(linked); m.patch.mockResolvedValue({ state: "empty", revision: "rev2", link: null });
});

it("fetches protected work only on explicit expansion using Teams credentials and drops it on collapse/error", async () => {
  view();
  expect(m.get).not.toHaveBeenCalled();
  await userEvent.click(screen.getByRole("button", { name: "Linked work" }));
  expect(await screen.findByText(linked.title)).toBeInTheDocument();
  expect(m.get).toHaveBeenCalledWith("/teams/rooms/room/local-sessions/session/work-link", { credentials: "include", retryOn401: true });
  await userEvent.click(screen.getByRole("button", { name: "Linked work" }));
  expect(screen.queryByText(linked.title)).not.toBeInTheDocument();
  m.get.mockRejectedValue(new Error("PRIVATE SERVER ERROR"));
  await userEvent.click(screen.getByRole("button", { name: "Linked work" }));
  expect(await screen.findByText("Linked work unavailable.")).toBeInTheDocument();
  expect(screen.queryByText(linked.title)).not.toBeInTheDocument();
  expect(screen.queryByText("PRIVATE SERVER ERROR")).not.toBeInTheDocument();
});

it("fences late protected responses across acting user changes", async () => {
  let resolve!: (value: typeof linked) => void;
  m.get.mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  const v = view();
  await userEvent.click(screen.getByRole("button", { name: "Linked work" }));
  await waitFor(() => expect(m.get).toHaveBeenCalled());
  m.userId = "outsider";
  m.get.mockResolvedValue({ state: "unavailable", revision: "rev1", link: linked.link });
  v.rerender(v.tree());
  await act(async () => resolve(linked));
  expect(await screen.findByText("Linked work unavailable.")).toBeInTheDocument();
  expect(screen.queryByText(linked.title)).not.toBeInTheDocument();
});

it("links only explicit identifiers, using current revision, and never sends a session command", async () => {
  m.get.mockResolvedValue({ state: "empty", revision: null, link: null });
  view();
  await userEvent.click(screen.getByRole("button", { name: "Linked work" }));
  await userEvent.click(await screen.findByRole("button", { name: "Link existing work" }));
  await userEvent.type(screen.getByRole("textbox", { name: "Issue ID" }), "44444444-4444-4444-8444-444444444444");
  await userEvent.click(screen.getByRole("button", { name: "Save link" }));
  expect(m.patch).toHaveBeenCalledWith("/teams/rooms/room/local-sessions/session/work-link", { link: { kind: "issue", issueId: "44444444-4444-4444-8444-444444444444" }, expectedLinkRevision: null }, { credentials: "include", retryOn401: true });
  expect(client.submitLocalSessionCommand).not.toHaveBeenCalled();
});
