import { act, render as rtlRender, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { SWRConfig } from "swr";
import { beforeEach, expect, it, vi } from "vitest";
import type { LiveEvent, LocalSession } from "@lightbulb/shared";
import { NewSessionButton } from "../NewSessionButton";

const m = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "alice", isLoaded: true }) }));
vi.mock("@/lib/api", () => ({ useApi: () => m }));

const MAC = { deviceId: "mac_1", generation: "gen_1", name: "Alice's MacBook" };
const OPTIONS = {
  sharing: true,
  folders: ["/Users/alice/code"],
  tools: [{ id: "claude", label: "Claude Code", modeLabel: "Permission mode",
    models: [{ id: "opus", label: "Opus" }], modes: [{ id: "plan", label: "Plan" }] }],
};
const session = (sessionId: string, humanUserId = "alice"): LocalSession => ({ sessionId, deviceId: "d", humanUserId, provider: "claude", generation: "g", status: "idle", providerVersion: "2", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null });

const wrap = (ui: ReactElement) => <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{ui}</SWRConfig>;
const render = (ui: ReactElement) => { const view = rtlRender(wrap(ui)); return { ...view, rerender: (next: ReactElement) => view.rerender(wrap(next)) }; };
let handlers: ((event: LiveEvent) => void)[] = [];
const onEvent = (h: (event: LiveEvent) => void) => { handlers.push(h); return () => { handlers = handlers.filter((x) => x !== h); }; };
const emit = (payload: object) => act(() => handlers.forEach((h) => h({ type: "runtime.operation.changed", payload } as unknown as LiveEvent)));

beforeEach(() => {
  vi.clearAllMocks(); handlers = [];
  vi.spyOn(crypto, "randomUUID").mockReturnValue("start-1-0000-0000-0000-000000000000" as `${string}-${string}-${string}-${string}-${string}`);
  m.get.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? { enabled: true, devices: [MAC] } : OPTIONS);
  m.post.mockResolvedValue({ startId: "start-1-0000-0000-0000-000000000000" });
});

it("stays hidden unless desktop execution is enabled for the workspace", async () => {
  m.get.mockResolvedValue({ enabled: false, devices: [] });
  render(<NewSessionButton sessions={[]} onEvent={onEvent} onOpenSession={vi.fn()} />);
  await waitFor(() => expect(m.get).toHaveBeenCalled());
  expect(screen.queryByRole("button", { name: "New session" })).not.toBeInTheDocument();
});

it("offers only what the Mac reported, starts, and opens the new session when it appears", async () => {
  const open = vi.fn();
  const view = render(<NewSessionButton sessions={[session("old")]} onEvent={onEvent} onOpenSession={open} />);
  await userEvent.click(await screen.findByRole("button", { name: "New session" }));
  expect(m.get).toHaveBeenCalledWith("/desktop-sessions/targets/mac_1/options?generation=gen_1");
  await userEvent.selectOptions(await screen.findByLabelText("Permission mode"), "plan");
  await userEvent.selectOptions(screen.getByLabelText("Model"), "opus");
  expect(screen.getByLabelText("Folder")).toHaveDisplayValue("/Users/alice/code");
  expect(screen.queryByRole("option", { name: /bypass/i })).not.toBeInTheDocument();
  await userEvent.type(screen.getByLabelText("First message"), "fix the build");
  await userEvent.click(screen.getByRole("button", { name: "Start session" }));
  expect(m.post).toHaveBeenCalledWith("/desktop-sessions/start", {
    startId: "start-1-0000-0000-0000-000000000000", deviceId: "mac_1", generation: "gen_1", tool: "claude", folder: "/Users/alice/code",
    model: "opus", mode: "plan", prompt: "fix the build",
  });
  expect(await screen.findByText("Approve the start on your Mac.")).toBeInTheDocument();
  emit({ operationId: "start-1-0000-0000-0000-000000000000", state: "succeeded", updatedAt: "now", result: {}, error: null });
  expect(await screen.findByText(/Opening in Terminal/)).toBeInTheDocument();
  // A teammate's session is never mistaken for mine; the known one is ignored too.
  view.rerender(<NewSessionButton sessions={[session("old"), session("theirs", "bob")]} onEvent={onEvent} onOpenSession={open} />);
  expect(open).not.toHaveBeenCalled();
  view.rerender(<NewSessionButton sessions={[session("old"), session("new")]} onEvent={onEvent} onOpenSession={open} />);
  expect(open).toHaveBeenCalledWith("new");
});

it("shows the Mac's refusal and blocks a start while sharing is off", async () => {
  render(<NewSessionButton sessions={[]} onEvent={onEvent} onOpenSession={vi.fn()} />);
  await userEvent.click(await screen.findByRole("button", { name: "New session" }));
  await userEvent.click(await screen.findByRole("button", { name: "Start session" }));
  emit({ operationId: "start-1-0000-0000-0000-000000000000", state: "failed", updatedAt: "now", result: null, error: { code: "DENIED", message: "The start was not approved on the Mac.", retryable: false } });
  expect(await screen.findByText("The start was not approved on the Mac.")).toBeInTheDocument();
});

it("hears a refusal that beats the HTTP response, and blocks an over-long message", async () => {
  m.post.mockImplementation(async () => { await new Promise((r) => setTimeout(r, 0)); emit({ operationId: "start-1-0000-0000-0000-000000000000", state: "failed", updatedAt: "now", result: null, error: { code: "SHARING_OFF", message: "Turn on session sharing.", retryable: false } }); return {}; });
  render(<NewSessionButton sessions={[]} onEvent={onEvent} onOpenSession={vi.fn()} />);
  await userEvent.click(await screen.findByRole("button", { name: "New session" }));
  await userEvent.click(await screen.findByRole("button", { name: "Start session" }));
  expect(await screen.findByText("Turn on session sharing.")).toBeInTheDocument();
  await userEvent.click(screen.getByLabelText("First message"));
  await userEvent.paste("界".repeat(1400));
  expect(screen.getByText("The first message is too long.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Start session" })).toBeDisabled();
});

it("disables Start when the Mac has sharing off", async () => {
  m.get.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? { enabled: true, devices: [MAC] } : { ...OPTIONS, sharing: false });
  render(<NewSessionButton sessions={[]} onEvent={onEvent} onOpenSession={vi.fn()} />);
  await userEvent.click(await screen.findByRole("button", { name: "New session" }));
  expect(await screen.findByText(/Session sharing is off/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Start session" })).toBeDisabled();
});
