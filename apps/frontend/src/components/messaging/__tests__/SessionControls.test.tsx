import { createHash } from "node:crypto";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { ApiError, canonicalJson, type LocalSession, type SessionControlClient } from "@lightbulb/shared";
import { SessionApprovalCard } from "../SessionControls";

const pendingApproval = {
  approvalId: "appr-0123456789abcdef0123456789abcdef", tool: "Bash", summary: "git push", inputDigest: "d".repeat(64), exact: true, modes: [],
} as const;
const session = (changes: Partial<LocalSession> = {}): LocalSession => ({
  sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2.1.282", generation: "g", status: "waiting-approval",
  activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null,
  latestEventSeq: 3, pendingApproval: { ...pendingApproval, modes: [...pendingApproval.modes] }, ...changes,
});
const client = () => ({ submitLocalSessionCommand: vi.fn(async () => ({})) }) as unknown as SessionControlClient & { submitLocalSessionCommand: ReturnType<typeof vi.fn> };

it("lets the owner approve the exact prompt, bound to its digest", async () => {
  const c = client();
  render(<SessionApprovalCard session={session()} client={c} own ownerName="You" />);
  expect(screen.getByText("git push")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Approve" }));
  expect(c.submitLocalSessionCommand).toHaveBeenCalledWith("s1", expect.objectContaining({
    kind: "approve", decision: "allow", approvalId: pendingApproval.approvalId, inputDigest: pendingApproval.inputDigest, expectedGeneration: "g",
  }));
  expect(await screen.findByText("Sent to your Mac.")).toBeInTheDocument();
});

it("drops the card when the prompt is answered in the terminal or the server says it is gone", async () => {
  const c = client();
  const { rerender } = render(<SessionApprovalCard session={session()} client={c} own ownerName="You" />);
  // The next session update carries no prompt: the card goes without a click.
  rerender(<SessionApprovalCard session={session({ status: "busy", pendingApproval: undefined })} client={c} own ownerName="You" />);
  expect(screen.queryByLabelText("Permission prompt")).not.toBeInTheDocument();
  // Still listed as open, but the answer comes back as a conflict and the re-read row has no prompt: no stale buttons stay up.
  const stale = { ...c, getLocalSession: vi.fn(async () => session({ status: "busy", pendingApproval: undefined })) };
  rerender(<SessionApprovalCard session={session()} client={stale} own ownerName="You" />);
  c.submitLocalSessionCommand.mockRejectedValueOnce(new ApiError(409, { code: "LOCAL_SESSION_CONFLICT" }, "That permission prompt has changed."));
  await userEvent.click(screen.getByRole("button", { name: "Approve" }));
  await waitFor(() => expect(screen.queryByLabelText("Permission prompt")).not.toBeInTheDocument());
  expect(stale.getLocalSession).toHaveBeenCalledWith("s1");
});

it("keeps the card with a Try again note when a conflict leaves the prompt open", async () => {
  const c = { ...client(), getLocalSession: vi.fn(async () => session()) };
  c.submitLocalSessionCommand.mockRejectedValueOnce(new ApiError(409, { code: "LOCAL_SESSION_CONFLICT" }, "Session changed. Try again."));
  render(<SessionApprovalCard session={session()} client={c} own ownerName="You" />);
  await userEvent.click(screen.getByRole("button", { name: "Approve" }));
  expect(await screen.findByText("Could not send the answer. Try again.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
});

it("shows a teammate the prompt but no controls until the owner opts in", () => {
  const { rerender } = render(<SessionApprovalCard session={session()} client={client()} own={false} ownerName="Alice" />);
  expect(screen.getByText("Waiting on Alice's approval in the terminal.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  rerender(<SessionApprovalCard session={session({ teamControl: true })} client={client()} own={false} ownerName="Alice" />);
  expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
});

it("keeps a prompt it cannot show whole deny-only, and offers only suggested modes", async () => {
  const c = client();
  const { rerender } = render(<SessionApprovalCard session={session({ pendingApproval: { ...pendingApproval, modes: [], exact: false } })} client={c} own ownerName="You" />);
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  expect(screen.getByText("Lightbulb shows only part of this call. Approve it on your Mac; you can deny it here.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Deny…" }));
  await userEvent.type(screen.getByLabelText(/Tell Claude why/), "not on main");
  await userEvent.click(screen.getByRole("button", { name: "Deny" }));
  expect(c.submitLocalSessionCommand).toHaveBeenCalledWith("s1", expect.objectContaining({ decision: "deny", text: "not on main" }));
  rerender(<SessionApprovalCard session={session({ pendingApproval: { ...pendingApproval, approvalId: "appr-ffffffffffffffffffffffffffffffff", tool: "Edit", modes: ["acceptEdits"] } })} client={c} own ownerName="You" />);
  await userEvent.click(screen.getByRole("button", { name: "Approve and switch to auto-accept edits" }));
  expect(c.submitLocalSessionCommand).toHaveBeenLastCalledWith("s1", expect.objectContaining({ decision: "allow", mode: "acceptEdits" }));
});

it("offers Claude's own similar-commands rule, naming what it allows", async () => {
  const c = client();
  render(<SessionApprovalCard session={session({ pendingApproval: { ...pendingApproval, summary: "npm run greet", modes: ["similar"], similar: "npm run *" } })} client={c} own ownerName="You" />);
  await userEvent.click(screen.getByRole("button", { name: "Allow similar commands for this session (npm run *)" }));
  expect(c.submitLocalSessionCommand).toHaveBeenCalledWith("s1", expect.objectContaining({ decision: "allow", mode: "similar" }));
});

it("answers a Codex prompt the same way, with Codex's own choices", async () => {
  const c = client();
  render(<SessionApprovalCard session={session({ provider: "codex", pendingApproval: { ...pendingApproval, summary: "/bin/zsh -lc 'mkdir -p /tmp/x'", modes: ["session"] } })} client={c} own ownerName="You" />);
  expect(screen.getByText("Codex wants to use Bash")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Allow for this conversation" }));
  expect(c.submitLocalSessionCommand).toHaveBeenCalledWith("s1", expect.objectContaining({ decision: "allow", mode: "session" }));
});

it("draws nothing when the session is not waiting", () => {
  const { container } = render(<SessionApprovalCard session={session({ status: "busy" })} client={client()} own ownerName="You" />);
  expect(container).toBeEmptyDOMElement();
});

// Hashed outside the component: sorted keys, compact, as the Mac digests the call.
const editInput = { file_path: "/Users/me/src/x.ts", old_string: "a", new_string: "b" };
const editDigest = createHash("sha256").update('{"file_path":"/Users/me/src/x.ts","new_string":"b","old_string":"a"}').digest("hex");
const fullEdit = { ...pendingApproval, tool: "Edit", summary: "src/x.ts", inputDigest: editDigest, exact: false, full: true, modes: [], input: editInput };
const diffText = (container: HTMLElement) => Array.from(container.querySelectorAll(".native-session-diff-line"), l => l.textContent);

it("shows an edit's diff and allows it when the digest of what is shown matches", async () => {
  const c = client();
  const { container } = render(<SessionApprovalCard session={session({ pendingApproval: fullEdit })} client={c} own ownerName="You" />);
  expect(diffText(container)).toEqual(["-a\n", "+b\n"]);
  expect(screen.getByText("/Users/me/src/x.ts")).toBeInTheDocument();
  await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
  expect(c.submitLocalSessionCommand).toHaveBeenCalledWith("s1", expect.objectContaining({ kind: "approve", decision: "allow", inputDigest: editDigest, shownDigest: editDigest }));
});

it("stays deny-only when the shown input does not hash to the prompt's digest", async () => {
  render(<SessionApprovalCard session={session({ pendingApproval: { ...fullEdit, inputDigest: "e".repeat(64) } })} client={client()} own ownerName="You" />);
  expect(await screen.findByText("Lightbulb shows only part of this call. Approve it on your Mac; you can deny it here.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
});

it("a call that is not full stays deny-only with the partial copy", () => {
  render(<SessionApprovalCard session={session({ pendingApproval: { ...pendingApproval, exact: false, full: false, modes: [], input: { command: "git push …" } }, teamControl: true })} client={client()} own={false} ownerName="Leo" />);
  expect(screen.getByText("git push …")).toBeInTheDocument();
  expect(screen.getByText("Lightbulb shows only part of this call. Approve it on Leo's Mac; you can deny it here.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
});

const sha = (input: object) => createHash("sha256").update(canonicalJson(input)).digest("hex");

it("shows a Bash call's flags, and that it runs outside the sandbox, before allowing it", async () => {
  const input = { command: "make deploy", description: "ship", dangerouslyDisableSandbox: true };
  render(<SessionApprovalCard session={session({ pendingApproval: { ...pendingApproval, modes: [], exact: false, full: true, inputDigest: sha(input), input } })} client={client()} own ownerName="You" />);
  expect(screen.getByText("Runs outside the sandbox")).toBeInTheDocument();
  expect(screen.getByText("Description: ship")).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
});

it("shows the whole JSON for a key no friendly view draws, and allows it only on a digest match", async () => {
  const input = { command: "ls", sneaky: "yes" };
  const json = (_: string, element: Element | null) => element?.tagName === "PRE" && element.textContent === JSON.stringify(input, null, 2);
  const { rerender } = render(<SessionApprovalCard session={session({ pendingApproval: { ...pendingApproval, modes: [], exact: false, full: true, inputDigest: "e".repeat(64), input } })} client={client()} own ownerName="You" />);
  expect(screen.getByText(json)).toBeInTheDocument();
  expect(await screen.findByText("Lightbulb shows only part of this call. Approve it on your Mac; you can deny it here.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  rerender(<SessionApprovalCard session={session({ pendingApproval: { ...pendingApproval, modes: [], exact: false, full: true, inputDigest: sha(input), input } })} client={client()} own ownerName="You" />);
  expect(await screen.findByRole("button", { name: "Approve" })).toBeInTheDocument();
});

it("keeps a call with hidden characters deny-only and spells them out", () => {
  const input = { command: "echo \u202Egnp.exe" };
  render(<SessionApprovalCard session={session({ pendingApproval: { ...pendingApproval, modes: [], exact: false, full: true, inputDigest: sha(input), input } })} client={client()} own ownerName="You" />);
  expect(screen.getByText((_, element) => element?.tagName === "PRE" && element.textContent === '{\n  "command": "echo \\u202Egnp.exe"\n}')).toBeInTheDocument();
  expect(screen.getByText("Lightbulb shows only part of this call. Approve it on your Mac; you can deny it here.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
});
