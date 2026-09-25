import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { COMMIT_AND_PR_INSTRUCTION, type LocalSession, type SessionSource } from "@lightbulb/shared";
import { SessionCommitPr } from "../SessionCommitPr";

const session = (changes: Partial<LocalSession> = {}): LocalSession => ({
  sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2.1.282", generation: "g", status: "idle",
  activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null,
  latestEventSeq: 3, ...changes,
});
const client = () => ({
  submitLocalSessionCommand: vi.fn(async (_: string, request: { requestId: string }) => ({ requestId: request.requestId, state: "applied", held: true, createdAt: 1, deliverBy: 31 })),
  getLocalSessionCommand: vi.fn(),
}) as unknown as SessionSource["client"] & { submitLocalSessionCommand: ReturnType<typeof vi.fn> };

it("sends one plain follow-up after a confirmation, never a git call of ours", async () => {
  const c = client();
  render(<SessionCommitPr session={session()} client={c} own ownerName="You" />);
  await userEvent.click(screen.getByRole("button", { name: "Commit & open PR" }));
  expect(await screen.findByRole("dialog", { name: "Commit and open a pull request?" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Send to Claude" }));
  expect(c.submitLocalSessionCommand).toHaveBeenCalledWith("s1", expect.objectContaining({ kind: "send", text: COMMIT_AND_PR_INSTRUCTION }));
});

it("is the owner's, or a teammate's only while the owner lets teammates control the session", () => {
  const { rerender } = render(<SessionCommitPr session={session()} client={client()} own={false} ownerName="Alice" />);
  expect(screen.queryByRole("button", { name: "Commit & open PR" })).not.toBeInTheDocument();
  rerender(<SessionCommitPr session={session({ teamControl: true })} client={client()} own={false} ownerName="Alice" />);
  expect(screen.getByRole("button", { name: "Commit & open PR" })).toBeInTheDocument();
  rerender(<SessionCommitPr session={session({ status: "offline" })} client={client()} own ownerName="You" />);
  expect(screen.queryByRole("button", { name: "Commit & open PR" })).not.toBeInTheDocument();
});
