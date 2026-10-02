import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { LocalSession, SessionControlClient } from "@lightbulb/shared";
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

it("shows a private send only to its owner before enabling approval", async () => {
  const detail = { approvalId: pendingApproval.approvalId, inputDigest: pendingApproval.inputDigest,
    destination: "person:bob", destinationLabel: "Bob", text: "First line\nSecond line", requestId: "e12fd3e8-32ca-4b65-8eaf-ab979e3c9449" };
  const c = { ...client(), getPrivateSendApproval: vi.fn(async () => detail) };
  const privateSession = session({ pendingApproval: { ...pendingApproval,
    tool: "mcp__lightbulb_native_sessions__send", summary: "Lightbulb message", exact: false, modes: [] } });
  const { rerender } = render(<SessionApprovalCard session={privateSession} client={c} own ownerName="You" />);
  const message = (_: string, element: Element | null) => element?.tagName === "PRE" && element.textContent === detail.text;
  expect(await screen.findByText(message)).toBeInTheDocument();
  expect(screen.getByText("To: Bob")).toBeInTheDocument();
  expect(screen.getByText(`person:bob · Request ID: ${detail.requestId}`)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  rerender(<SessionApprovalCard session={session({ ...privateSession, teamControl: true })} client={c} own={false} ownerName="Alice" />);
  expect(screen.queryByText(message)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
});

it("lets the owner retry a failed private detail load", async () => {
  const detail = { approvalId: pendingApproval.approvalId, inputDigest: pendingApproval.inputDigest,
    destination: "room:one", text: "send this", requestId: "e12fd3e8-32ca-4b65-8eaf-ab979e3c9449" };
  const c = { ...client(), getPrivateSendApproval: vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(detail) };
  render(<SessionApprovalCard session={session({ pendingApproval: { ...pendingApproval,
    tool: "mcp__lightbulb_native_sessions__send", exact: false, modes: [] } })} client={c} own ownerName="You" />);
  await userEvent.click(await screen.findByRole("button", { name: "Retry loading message" }));
  expect(await screen.findByText("send this")).toBeInTheDocument();
  expect(c.getPrivateSendApproval).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
});

it("lets only the owner approve a pending Codex send after its turn becomes idle", async () => {
  const detail = { approvalId: pendingApproval.approvalId, inputDigest: pendingApproval.inputDigest,
    destination: "room:one", text: "private Codex message", requestId: "e12fd3e8-32ca-4b65-8eaf-ab979e3c9449" };
  const c = { ...client(), getPrivateSendApproval: vi.fn(async () => detail),
    answerNativeSendApproval: vi.fn(async () => ({ requestId: detail.requestId, state: "applied" as const })) };
  const codex = session({ provider: "codex", status: "idle", nativeSendApproval: true, pendingApproval: {
    ...pendingApproval, tool: "mcp__lightbulb_native_sessions__send", summary: "Lightbulb message", exact: false, modes: [],
  } });
  const { rerender } = render(<SessionApprovalCard session={codex} client={c} own ownerName="You" />);
  expect(await screen.findByText("private Codex message")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Approve" }));
  expect(c.answerNativeSendApproval).toHaveBeenCalledWith("s1", {
    approvalId: detail.approvalId, inputDigest: detail.inputDigest, requestId: detail.requestId, decision: "allow",
  });
  rerender(<SessionApprovalCard session={{ ...codex, teamControl: true }} client={c} own={false} ownerName="Alice" />);
  expect(screen.queryByText("private Codex message")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
});
