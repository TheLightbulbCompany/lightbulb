import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { SWRConfig } from "swr";
import { SessionFocus } from "../SessionFocus";
import type { LocalSession } from "@lightbulb/shared";

const m = vi.hoisted(() => ({ get: vi.fn(), patch: vi.fn(), userId: "bob", orgId: "org", open: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: m.userId, orgId: m.orgId, isLoaded: true }) }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: m.get, patch: m.patch }) }));
vi.mock("@/components/workspace/workspaceController", () => ({ openPanel: m.open }));
const linked = { state: "linked", revision: "rev1", link: { kind: "issue", companyId: "company", issueId: "issue" }, title: "Private synthetic title" };
const session: LocalSession = { sessionId: "session", deviceId: "mac", humanUserId: "alice", provider: "codex", generation: "g1", status: "idle", providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null };
const client = { listWorkspaceSessions: vi.fn(), getLocalSession: vi.fn(), listLocalSessionEvents: vi.fn(async () => ({ events: [], nextAfterSeq: null })), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), setLocalSessionSharing: vi.fn() };
function view() {
  const cache = new Map();
  const tree = () => <SWRConfig value={{ provider: () => cache }}><SessionFocus focus={{ sessionId: "session" }} entries={[{ session, ownerName: "Alice", own: false }]}
    hintRevision={1} client={client} resolveSessionOwner={() => ({ ownerName: "Alice", own: false, owner: { kind: "human" as const, id: "alice", displayName: "Alice", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "", joinedSeq: 0 } })} onAccessLost={() => {}} onClose={() => {}} /></SWRConfig>;
  return { ...render(tree()), tree };
}
// Linked work is a tab of the session's Details dialog.
async function openWork() {
  await userEvent.click(screen.getByRole("button", { name: "Details" }));
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(within(dialog).getByRole("tab", { name: "Linked work" }));
  return dialog;
}
beforeEach(() => {
  vi.clearAllMocks(); m.userId = "bob"; m.orgId = "org";
  m.get.mockResolvedValue(linked); m.patch.mockResolvedValue({ state: "empty", revision: "rev2", link: null });
});

it("fetches protected work only on explicit open using Teams credentials and drops it on close/error", async () => {
  view();
  expect(m.get).not.toHaveBeenCalled();
  await openWork();
  expect(await screen.findByText(linked.title)).toBeInTheDocument();
  expect(m.get).toHaveBeenCalledWith("/teams/local-sessions/session/work-link", { credentials: "include", retryOn401: true });
  await userEvent.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.queryByText(linked.title)).not.toBeInTheDocument();
  m.get.mockRejectedValue(new Error("PRIVATE SERVER ERROR"));
  await openWork();
  expect(await screen.findByText("Linked work unavailable.")).toBeInTheDocument();
  expect(screen.queryByText(linked.title)).not.toBeInTheDocument();
  expect(screen.queryByText("PRIVATE SERVER ERROR")).not.toBeInTheDocument();
});

// Nothing that opens Details is a Radix trigger, so the pane restores focus itself.
it("returns focus to Details when the dialog closes", async () => {
  view();
  await openWork();
  await userEvent.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "Details" })).toHaveFocus();
});

it("fences late protected responses across acting user changes", async () => {
  let resolve!: (value: typeof linked) => void;
  m.get.mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  const v = view();
  await openWork();
  await waitFor(() => expect(m.get).toHaveBeenCalled());
  m.userId = "outsider";
  m.get.mockResolvedValue({ state: "unavailable", revision: "rev1", link: linked.link });
  v.rerender(v.tree());
  await act(async () => resolve(linked));
  expect(await screen.findByText("Linked work unavailable.")).toBeInTheDocument();
  expect(screen.queryByText(linked.title)).not.toBeInTheDocument();
});

it("links a searched task or project workspace, using current revision, and never sends a session command", async () => {
  const task = { id: "44444444-4444-4444-8444-444444444444", identifier: "LB-7", title: "Fix the relay" };
  const project = { id: "55555555-5555-4555-8555-555555555555", name: "Site", workspaces: [{ id: "66666666-6666-4666-8666-666666666666", name: "main" }] };
  m.get.mockImplementation(async (path: string) => path.startsWith("/teams/issues?") ? { issues: path.includes("q=relay") ? [task] : [] }
    : path === "/teams/projects" ? { projects: [project] } : { state: "empty", revision: null, link: null });
  view();
  await openWork();
  await userEvent.click(await screen.findByRole("button", { name: "Link existing work" }));
  expect(screen.queryByRole("textbox", { name: "Issue ID" })).toBeNull();
  await userEvent.click(await screen.findByRole("button", { name: "Site · main" }));
  expect(m.patch).toHaveBeenLastCalledWith("/teams/local-sessions/session/work-link", { link: { kind: "workspace", projectId: project.id, workspaceId: project.workspaces[0].id }, expectedLinkRevision: null }, { credentials: "include", retryOn401: true });
  await userEvent.click(await screen.findByRole("button", { name: "Link existing work" }));
  await userEvent.type(screen.getByRole("searchbox", { name: "Find a task or project" }), "relay");
  await userEvent.click(await screen.findByRole("button", { name: "LB-7 · Fix the relay" }));
  expect(m.patch).toHaveBeenLastCalledWith("/teams/local-sessions/session/work-link", { link: { kind: "issue", issueId: task.id }, expectedLinkRevision: null }, { credentials: "include", retryOn401: true });
  expect(client.submitLocalSessionCommand).not.toHaveBeenCalled();
});
