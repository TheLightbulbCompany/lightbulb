import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { ApiError, type LocalSession, type LocalSessionAuditEntry, type LocalSessionEvent } from "@lightbulb/shared";
import { SessionDetails, type SessionDetailsTab } from "../SessionDetails";

const usage = { inputTokens: 1200, outputTokens: 340, cacheReadTokens: 0, cacheWriteTokens: 0 };
const session = (changes: Partial<LocalSession> = {}): LocalSession => ({
  sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2.1.283", generation: "g", status: "idle",
  activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null,
  latestEventSeq: 3, title: "Fix login", usage, ...changes,
});
const ev = (seq: number, e: Omit<LocalSessionEvent, "seq" | "truncated">): LocalSessionEvent => ({ seq, truncated: false, ...e });

/** An owner's client whose every setter answers the way the server would. */
const client = () => ({
  submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn(), getLocalSession: vi.fn(), listWorkspaceSessions: vi.fn(), listLocalSessionEvents: vi.fn(),
  setLocalSessionSharing: vi.fn(async (_: string, shared: boolean) => session({ shared })),
  setLocalSessionTeamControl: vi.fn(async (_: string, teamControl: boolean) => session({ teamControl })),
  setLocalSessionUsageSharing: vi.fn(async (_: string, shareUsage: boolean) => session({ shareUsage })),
  getLocalSessionLink: vi.fn(async () => { throw new ApiError(404, null); }), putLocalSessionLink: vi.fn(),
  listLocalSessionAudit: vi.fn(async () => ({ entries: [] as LocalSessionAuditEntry[] })),
  removeLocalSessionSender: vi.fn(async () => session()),
});

function Details({ tab: initial = "access", s = session(), own = true, c = client(), events = [] }: {
  tab?: SessionDetailsTab; s?: LocalSession; own?: boolean; c?: ReturnType<typeof client>; events?: LocalSessionEvent[];
}) {
  const [tab, setTab] = useState<SessionDetailsTab>(initial);
  return <SessionDetails open onOpenChange={() => {}} tab={tab} onTab={setTab} session={s} ownerName="Alice" own={own} client={c} events={events} />;
}
const switchNamed = (name: string) => screen.getByRole("switch", { name });

it("gives the owner one control for who can see the session, and two switches", async () => {
  render(<Details />);
  expect(screen.getAllByRole("switch").map(s => (s as HTMLButtonElement).labels[0]?.textContent)).toEqual([
    "Let teammates control this session", "Show token usage to teammates",
  ]);
  expect(screen.queryByText("Shared with the workspace")).not.toBeInTheDocument(); // Only me is that switch now
  await waitFor(() => expect(screen.getByLabelText("Who can see this")).toHaveValue("workspace"));
  expect(screen.getByRole("button", { name: "Copy link" })).toBeInTheDocument();
});

it("writes each switch through its own client method and shows the server's answer", async () => {
  const c = client();
  render(<Details c={c} />);
  await userEvent.click(switchNamed("Let teammates control this session"));
  expect(c.setLocalSessionTeamControl).toHaveBeenCalledWith("s1", true);
  expect(await screen.findByRole("switch", { name: "Let teammates control this session", checked: true })).toBeInTheDocument();

  await userEvent.click(switchNamed("Show token usage to teammates"));
  expect(c.setLocalSessionUsageSharing).toHaveBeenCalledWith("s1", true);
  expect(await screen.findByRole("switch", { name: "Show token usage to teammates", checked: true })).toBeInTheDocument();

  // Sharing is the one control's: Only me, on a second click.
  await waitFor(() => expect(screen.getByLabelText("Who can see this")).toBeEnabled());
  await userEvent.selectOptions(screen.getByLabelText("Who can see this"), "me");
  await userEvent.click(screen.getByRole("button", { name: "Click again to stop sharing" }));
  expect(c.setLocalSessionSharing).toHaveBeenCalledWith("s1", false);
  expect(screen.getByLabelText("Who can see this")).toHaveValue("me");
});

it("shows a failed save's error and puts the switch back", async () => {
  const c = { ...client(), setLocalSessionTeamControl: vi.fn(async () => { throw new Error("500"); }) };
  render(<Details c={c} />);
  await userEvent.click(switchNamed("Let teammates control this session"));
  expect(await screen.findByText("Could not change who controls this session.")).toHaveAttribute("role", "alert");
  expect(switchNamed("Let teammates control this session")).not.toBeChecked();
});

it("offers no pick-up switch on a Codex session", () => {
  render(<Details s={session({ provider: "codex" })} />);
  expect(screen.queryByRole("switch", { name: "Let teammates pick up this session" })).not.toBeInTheDocument();
  expect(switchNamed("Let teammates control this session")).toBeInTheDocument();
});

it("shows a teammate a read-only sentence and no switches", () => {
  const { rerender } = render(<Details own={false} />);
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  expect(screen.getByText("Only Alice can send to this session or hand it on. Anyone in the workspace can read it.")).toBeInTheDocument();
  rerender(<Details own={false} s={session({ teamControl: true, pickup: { allowed: true, state: "preparing" } })} />);
  expect(screen.getByText("Alice lets teammates send follow-ups and answer its prompts, and pick it up on their own Mac.")).toBeInTheDocument();
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
});

it("About shows the model, the mode and the tokens", () => {
  render(<Details tab="about" s={session({ model: "claude-opus-5-5", permissionMode: "acceptEdits" })} />);
  const about = screen.getByRole("tabpanel", { name: "About" });
  expect(within(about).getByText("claude-opus-5-5 · Auto-accept edits")).toBeInTheDocument();
  expect(within(about).getByText("1.2k in · 340 out")).toBeInTheDocument();
  expect(within(about).getByText("Claude Code 2.1.283")).toBeInTheDocument();
});

it("About names the folder as the Mac reported it, never as a path from home", () => {
  // Prod 2026-10-07: a session in ~/Lightbulb/lb-accept2-86aa00 read "~/lb-accept2-86aa00".
  render(<Details tab="about" s={session({ cwdName: "lb-accept2-86aa00" })} />);
  const about = screen.getByRole("tabpanel", { name: "About" });
  expect(within(about).getByText("lb-accept2-86aa00")).toBeInTheDocument();
  expect(about).not.toHaveTextContent("~/");
});

it("About says nothing about output arriving block by block: the terminal is live", () => {
  render(<Details tab="about" s={session({ workspace: { revision: 1, coverage: "unknown", changed: false, overlap: false } })} />);
  const about = screen.getByRole("tabpanel", { name: "About" });
  expect(about.textContent).not.toMatch(/block by block|token by token/);
  expect(within(about).getByText("Outside changes to this checkout are not being watched.")).toBeInTheDocument();
});

it("Changes shows the plan and the files when the events carry them, and says so when they do not", async () => {
  const events = [
    ev(1, { kind: "tool", role: "assistant", text: "Update Todos", itemId: "t1", tool: { name: "TodoWrite", status: "running", input: { todos: [
      { content: "Map it", status: "completed" }, { content: "Fix it", status: "in_progress" },
    ] } } }),
    ev(2, { kind: "tool", role: "assistant", text: "src/a.ts", itemId: "e1", tool: { name: "Edit", status: "running", input: { file_path: "src/a.ts" } } }),
    ev(3, { kind: "tool", role: "tool", text: "ok", parentId: "e1", tool: { name: "result", status: "done", result: { filePath: "src/a.ts", structuredPatch: [{ lines: ["-old", "+new"] }] } } }),
  ];
  const { unmount } = render(<Details tab="changes" events={events} />);
  expect(screen.getByText("Plan · 1 of 2 done")).toBeInTheDocument();
  expect(screen.getByText("src/a.ts")).toBeInTheDocument();
  expect(screen.queryByText(/No plan or file changes yet/)).not.toBeInTheDocument();
  unmount();
  render(<Details tab="changes" events={[ev(1, { kind: "text", role: "assistant", text: "hi" })]} />);
  expect(screen.getByText(/^No plan or file changes yet\./)).toBeInTheDocument();
});

it("switches tabs from the tab list", async () => {
  render(<Details tab="about" />);
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("tab", { name: "Access" }));
  expect(switchNamed("Let teammates control this session")).toBeInTheDocument();
});

it("keeps a saved switch when the owner leaves Access before the session refresh arrives", async () => {
  render(<Details />);
  await userEvent.click(switchNamed("Let teammates control this session"));
  expect(await screen.findByRole("switch", { name: "Let teammates control this session", checked: true })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("tab", { name: "About" }));
  expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("tab", { name: "Access" }));
  expect(switchNamed("Let teammates control this session")).toBeChecked();
});

it("lists the people the owner let in, each with Remove", async () => {
  const c = client();
  render(<SessionDetails open onOpenChange={() => {}} tab="access" onTab={() => {}} session={session({ allowedSenders: ["bob"] })} ownerName="Alice"
    own client={c} events={[]} nameOf={id => (id === "bob" ? "Bob" : "Someone")} />);
  expect(screen.getByText("People you let in")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Remove Bob" }));
  expect(c.removeLocalSessionSender).toHaveBeenCalledWith("s1", "bob");
  expect(screen.queryByText("People you let in")).not.toBeInTheDocument();
});

it("tells a person the owner let in that they may send", () => {
  render(<Details own={false} s={session({ allowedSenders: ["user_test_123"] })} />);
  expect(screen.getByText("Alice let you send follow-ups and answer its prompts. Anyone in the workspace can read it.")).toBeInTheDocument();
  expect(screen.queryByText(/^Only Alice can send/)).not.toBeInTheDocument();
});

it("lists who may resume an ended session, each with Remove", async () => {
  const c = { ...client(), revokeLocalSessionResume: vi.fn(async () => session({ status: "ended", resumeAllowed: [] })) };
  render(<SessionDetails open onOpenChange={() => {}} tab="access" onTab={() => {}} session={session({ status: "ended", resumeAllowed: ["bob"] })}
    ownerName="Alice" own client={c} events={[]} nameOf={id => (id === "bob" ? "Bob" : "Someone")} />);
  expect(screen.getByText("Who can resume it")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Remove Bob" }));
  expect(c.revokeLocalSessionResume).toHaveBeenCalledWith("s1", "bob");
});

it("lists who took a copy on Access", async () => {
  const c = { ...client(), listLocalSessionAudit: vi.fn(async () => ({ entries: [
    { at: "2026-10-05T10:00:00Z", action: "session_copied" as const, actorUserId: "user_out", detail: "Sam Lee" },
  ] })) };
  render(<Details c={c} />);
  expect(await screen.findByText(/Sam Lee/)).toBeInTheDocument();
});
