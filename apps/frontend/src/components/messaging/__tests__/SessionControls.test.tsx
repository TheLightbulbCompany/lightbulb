import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { LocalSession, SessionControlClient } from "@lightbulb/shared";
import { SessionApprovalCard, SessionUsageMeter, TeamControlToggle } from "../SessionControls";

const pendingApproval = {
  approvalId: "appr-0123456789abcdef0123456789abcdef", tool: "Bash", summary: "git push", inputDigest: "d".repeat(64), exact: true, modes: [],
} as const;
const session = (changes: Partial<LocalSession> = {}): LocalSession => ({
  sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2.1.282", generation: "g", status: "waiting-approval",
  activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null,
  latestEventSeq: 3, pendingApproval: { ...pendingApproval, modes: [...pendingApproval.modes] }, ...changes,
});
const client = () => ({ submitLocalSessionCommand: vi.fn(async () => ({})), setLocalSessionTeamControl: vi.fn(async (_: string, enabled: boolean) => session({ teamControl: enabled })) }) as unknown as SessionControlClient & { submitLocalSessionCommand: ReturnType<typeof vi.fn>; setLocalSessionTeamControl: ReturnType<typeof vi.fn> };

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
  await userEvent.click(screen.getByRole("button", { name: "Deny…" }));
  await userEvent.type(screen.getByLabelText(/Tell Claude why/), "not on main");
  await userEvent.click(screen.getByRole("button", { name: "Deny" }));
  expect(c.submitLocalSessionCommand).toHaveBeenCalledWith("s1", expect.objectContaining({ decision: "deny", text: "not on main" }));
  rerender(<SessionApprovalCard session={session({ pendingApproval: { ...pendingApproval, approvalId: "appr-ffffffffffffffffffffffffffffffff", tool: "Edit", modes: ["acceptEdits"] } })} client={c} own ownerName="You" />);
  await userEvent.click(screen.getByRole("button", { name: "Approve and switch to auto-accept edits" }));
  expect(c.submitLocalSessionCommand).toHaveBeenLastCalledWith("s1", expect.objectContaining({ decision: "allow", mode: "acceptEdits" }));
});

it("draws nothing when the session is not waiting", () => {
  const { container } = render(<SessionApprovalCard session={session({ status: "busy" })} client={client()} own ownerName="You" />);
  expect(container).toBeEmptyDOMElement();
});

it("the owner's toggle writes through the server", async () => {
  const c = client();
  render(<TeamControlToggle session={session({ status: "busy" })} client={c} />);
  await userEvent.click(screen.getByRole("checkbox", { name: "Let teammates control this session" }));
  expect(c.setLocalSessionTeamControl).toHaveBeenCalledWith("s1", true);
  expect(await screen.findByRole("checkbox", { name: "Let teammates control this session" })).toBeChecked();
});

it("shows the token meter, and only the owner can share it", async () => {
  const usage = { inputTokens: 1200, outputTokens: 340, cacheReadTokens: 0, cacheWriteTokens: 0 };
  const setLocalSessionUsageSharing = vi.fn(async () => session({ usage, shareUsage: true }));
  const c = { ...client(), setLocalSessionUsageSharing } as unknown as SessionControlClient;
  const { rerender } = render(<SessionUsageMeter session={session({ usage })} client={c} own />);
  expect(screen.getByText("1.2k in · 340 out")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("checkbox", { name: "Show usage to teammates" }));
  expect(setLocalSessionUsageSharing).toHaveBeenCalledWith("s1", true);
  rerender(<SessionUsageMeter session={session({ usage })} client={c} own={false} />);
  expect(screen.queryByRole("checkbox", { name: "Show usage to teammates" })).not.toBeInTheDocument();
  rerender(<SessionUsageMeter session={session()} client={c} own={false} />);
  expect(screen.queryByText(/ in · /)).not.toBeInTheDocument();
});
