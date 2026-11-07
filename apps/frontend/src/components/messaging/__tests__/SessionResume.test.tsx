import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { useSessionResume, type LocalSession } from "@lightbulb/shared";
import { ResumeDelivery, SessionResume } from "../SessionResume";

const ended = (changes: Partial<LocalSession> = {}): LocalSession => ({
  sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2.1.288", generation: "g1", status: "ended",
  activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null,
  latestEventSeq: 3, title: "Fix login", resumeAllowed: ["bob"], resumable: true, ...changes,
});
const client = () => ({ submitLocalSessionCommand: vi.fn(), resumeLocalSession: vi.fn(async () => ended()), getLocalSessionCommand: vi.fn() });

it("opens an owner desktop conversation without asking for Continue", () => {
  render(<SessionResume own ownerName="Alice" flow={{ available: true, automatic: true, status: null, saving: false, error: null, resume: vi.fn(), chip: null }} />);
  expect(screen.getByRole("status")).toHaveTextContent("Opening this conversation");
  expect(screen.queryByRole("button")).toBeNull();
});

it("checks a slow pending open without submitting another resume", async () => {
  const check = vi.fn(async () => {});
  const resume = vi.fn();
  render(<SessionResume own ownerName="Alice" flow={{ available: false, automatic: true, status: "Opening this session on your Mac…", saving: false,
    error: "Your Mac has not opened this conversation yet.", check, resume, chip: null }} />);
  await userEvent.click(screen.getByRole("button", { name: "Check again" }));
  expect(check).toHaveBeenCalledOnce();
  expect(resume).not.toHaveBeenCalled();
});

function Harness({ s, c, own = false, userId = "bob", autoOpen = false }: { s: LocalSession; c: ReturnType<typeof client>; own?: boolean; userId?: string; autoOpen?: boolean }) {
  const flow = useSessionResume(c, s, { own, userId, autoOpen, ownerName: "Alice", nameOf: id => (id === "bob" ? "Bob" : "Someone"), uuid: () => "r-1" });
  return <><SessionResume flow={flow} own={own} ownerName="Alice" /><ResumeDelivery flow={flow} /></>;
}

it("offers Continue when a conversation the owner already opened subsequently ends", async () => {
  const c = client();
  const view = render(<Harness own autoOpen s={ended({ status: "idle", generation: "g2" })} c={c} />);
  view.rerender(<Harness own autoOpen s={ended({ generation: "g2" })} c={c} />);
  expect(await screen.findByRole("button", { name: "Continue" })).toBeInTheDocument();
  expect(screen.queryByText("Opening this conversation…")).toBeNull();
  expect(c.resumeLocalSession).not.toHaveBeenCalled();
});

it("lets someone who could send resume it with a message, in plan mode on the owner's Mac", async () => {
  const c = client();
  render(<Harness s={ended()} c={c} />);
  expect(screen.getByText("Starts it again on Alice’s Mac in plan mode. Alice sees it there and can end it.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
  await userEvent.type(screen.getByLabelText("Message to send when it resumes"), "Carry on");
  await userEvent.click(screen.getByRole("button", { name: "Resume and send" }));
  expect(c.resumeLocalSession).toHaveBeenCalledWith("s1", { resumeId: "r-1", message: "Carry on" });
});

it("offers nothing to someone who could not send when it ended", () => {
  const { container } = render(<Harness s={ended()} c={client()} userId="carol" />);
  expect(container).toBeEmptyDOMElement();
});

it("offers Continue and a way to resolve a folder-access refusal", () => {
  render(<Harness own userId="alice" s={ended({ resume: { state: "failed", byUserId: "alice", requestedAt: "2026-10-03T10:00:00Z", reason: "FOLDER_NOT_APPROVED" } })} c={client()} />);
  expect(screen.getByRole("button", { name: "Continue" })).toBeEnabled();
  expect(screen.getByText(/folder access for Lightbulb/)).toBeInTheDocument();
});

it("shows a waiting resume instead of a second button", () => {
  render(<Harness s={ended({ resume: { state: "pending", byUserId: "bob", requestedAt: "2026-10-03T10:00:00Z" } })} c={client()} />);
  expect(screen.getByRole("status")).toHaveTextContent("Opening this session on Alice’s Mac…");
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("says Delivery unknown when the first message's outcome is lost", async () => {
  const c = client();
  c.getLocalSessionCommand.mockResolvedValue({ state: "unknown", requestId: "r-1" });
  const { rerender } = render(<Harness s={ended({ sessionId: "s-lost" })} c={c} />);
  await userEvent.type(screen.getByLabelText("Message to send when it resumes"), "Carry on");
  await userEvent.click(screen.getByRole("button", { name: "Resume and send" }));
  rerender(<Harness s={ended({ sessionId: "s-lost", status: "idle", generation: "g2" })} c={c} />);
  expect(await screen.findByText(/Delivery unknown/)).toBeInTheDocument();
});
