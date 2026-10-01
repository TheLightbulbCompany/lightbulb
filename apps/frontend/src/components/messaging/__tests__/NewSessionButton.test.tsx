import { act, render as rtlRender, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { SWRConfig } from "swr";
import { beforeEach, expect, it, vi } from "vitest";
import type { LiveEvent, LocalSession } from "@lightbulb/shared";
import { NewSessionButton, NewSessionView } from "../NewSessionButton";

const m = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "alice", isLoaded: true }) }));
vi.mock("@/lib/api", () => ({ useApi: () => m }));

const MAC = { deviceId: "mac_1", generation: "gen_1", name: "Alice's MacBook" };
const MAC2 = { deviceId: "mac_2", generation: "gen_2", name: "Alice's Studio" };
const tool = (id: "claude" | "codex", label: string) => ({ id, label, modeLabel: id === "claude" ? "Permission mode" : "Approval mode",
  models: [{ id: `${id}-model`, label: "Big" }], modes: [{ id: `${id}-mode`, label: "Careful" }], defaultMode: `${id}-default`,
  efforts: [{ id: "low", label: "Low" }, { id: "high", label: "High" }], defaultEffort: null });
const OPTIONS = { sharing: true, folders: ["/Users/alice/code", "/Users/alice/site"], tools: [tool("claude", "Claude Code"), tool("codex", "Codex")] };
const session = (sessionId: string, humanUserId = "alice"): LocalSession => ({ sessionId, deviceId: "d", humanUserId, provider: "claude", generation: "g", status: "idle", providerVersion: "2", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null });
const START_ID = "start-1-0000-0000-0000-000000000000";
const PLACEHOLDER = "What do you want to do?";

const wrap = (ui: ReactElement) => <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{ui}</SWRConfig>;
const render = (ui: ReactElement) => { const view = rtlRender(wrap(ui)); return { ...view, rerender: (next: ReactElement) => view.rerender(wrap(next)) }; };
let handlers: ((event: LiveEvent) => void)[] = [];
const onEvent = (h: (event: LiveEvent) => void) => { handlers.push(h); return () => { handlers = handlers.filter((x) => x !== h); }; };
const emit = (payload: object) => act(() => handlers.forEach((h) => h({ type: "runtime.operation.changed", payload } as unknown as LiveEvent)));
const mockTargets = (targets: object, options: object = OPTIONS) =>
  m.get.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? targets : options);
const view = (sessions: LocalSession[] = [], open = vi.fn()) =>
  <NewSessionView sessions={sessions} onEvent={onEvent} onOpenSession={open} onClose={vi.fn()} />;

beforeEach(() => {
  vi.clearAllMocks(); handlers = []; localStorage.clear();
  vi.spyOn(crypto, "randomUUID").mockReturnValue(START_ID as `${string}-${string}-${string}-${string}-${string}`);
  mockTargets({ enabled: true, devices: [MAC] });
  m.post.mockResolvedValue({ startId: START_ID });
});

it("the button stays hidden unless desktop execution is enabled", async () => {
  mockTargets({ enabled: false, devices: [] });
  render(<NewSessionButton onOpen={vi.fn()} />);
  await waitFor(() => expect(m.get).toHaveBeenCalled());
  expect(screen.queryByRole("button", { name: "New session" })).not.toBeInTheDocument();
});

it("the button opens the view directly, no dialog", async () => {
  const onOpen = vi.fn();
  render(<NewSessionButton onOpen={onOpen} />);
  await userEvent.click(await screen.findByRole("button", { name: "New session" }));
  expect(onOpen).toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("Enter on the first message starts with the defaults, then becomes the new session", async () => {
  const open = vi.fn();
  const ui = render(view([session("old")], open));
  // One ready Mac: no Mac chip; model, mode and effort stay behind the settings button.
  expect(await screen.findByRole("button", { name: "Claude Code", pressed: true })).toBeInTheDocument();
  expect(screen.queryByLabelText("Mac")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Model")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Folder")).toHaveValue("/Users/alice/code");
  await userEvent.type(screen.getByPlaceholderText(PLACEHOLDER), "fix the build{Enter}");
  expect(m.post).toHaveBeenCalledWith("/desktop-sessions/start?source=web", {
    startId: START_ID, deviceId: "mac_1", generation: "gen_1", tool: "claude", folder: "/Users/alice/code",
    model: null, mode: "claude-default", effort: null, prompt: "fix the build",
  });
  expect(await screen.findByText(/Starting on Alice's MacBook · ~\/code…/)).toBeInTheDocument();
  emit({ operationId: START_ID, state: "succeeded", updatedAt: "now", result: {}, error: null });
  // A teammate's session is never mistaken for mine; the known one is ignored too.
  ui.rerender(view([session("old"), session("theirs", "bob")], open));
  expect(open).not.toHaveBeenCalled();
  ui.rerender(view([session("old"), session("new")], open));
  expect(open).toHaveBeenCalledWith("new");
});

it("restores the last-used Mac, tool and folder, and lists recent folders first", async () => {
  mockTargets({ enabled: true, devices: [MAC, MAC2] });
  localStorage.setItem("lightbulb.newSession.alice", JSON.stringify({ deviceId: "mac_2", tool: "codex", folders: ["/Users/alice/site"] }));
  render(view());
  expect(await screen.findByRole("button", { name: "Codex", pressed: true })).toBeInTheDocument();
  expect(screen.getByLabelText("Mac")).toHaveValue("mac_2");
  expect(screen.getByLabelText("Folder")).toHaveValue("/Users/alice/site");
  expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["Alice's MacBook", "Alice's Studio", "~/site", "~/code"]);
  // Model and mode live behind the settings affordance, from the tool's closed lists.
  await userEvent.click(screen.getByRole("button", { name: "Session settings" }));
  await userEvent.selectOptions(await screen.findByLabelText("Approval mode"), "codex-mode");
  await userEvent.selectOptions(screen.getByLabelText("Reasoning effort"), "high");
  await userEvent.type(screen.getByPlaceholderText(PLACEHOLDER), "ship it{Enter}");
  expect(m.post).toHaveBeenCalledWith("/desktop-sessions/start?source=web", expect.objectContaining({ deviceId: "mac_2", tool: "codex", folder: "/Users/alice/site", model: null, mode: "codex-mode", effort: "high" }));
  expect(JSON.parse(localStorage.getItem("lightbulb.newSession.alice")!)).toEqual({ deviceId: "mac_2", tool: "codex", folders: ["/Users/alice/site"] });
});

it.each([["corrupt", "{not json"], ["stale", JSON.stringify({ deviceId: "gone", tool: "vim", folders: ["/nope"] })]])("falls back to defaults on a %s last use", async (_, stored) => {
  localStorage.setItem("lightbulb.newSession.alice", stored);
  render(view());
  expect(await screen.findByRole("button", { name: "Claude Code", pressed: true })).toBeInTheDocument();
  expect(screen.getByLabelText("Folder")).toHaveValue("/Users/alice/code");
});

it("no ready Mac: one line, composer disabled", async () => {
  mockTargets({ enabled: true, devices: [] });
  render(view());
  expect(await screen.findByText("No Mac is ready. Open the Lightbulb desktop app on your Mac and sign in.")).toBeInTheDocument();
  expect(screen.getByPlaceholderText(PLACEHOLDER)).toBeDisabled();
});

it("sharing off: one line pointing at the switch, composer disabled", async () => {
  mockTargets({ enabled: true, devices: [MAC] }, { ...OPTIONS, sharing: false });
  render(view());
  expect(await screen.findByText(/Session sharing is off on Alice's MacBook/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Settings → Native terminal sessions" })).toHaveAttribute("href", "/settings");
  expect(screen.getByPlaceholderText(PLACEHOLDER)).toBeDisabled();
});

it("blocks an older Mac that cannot advertise safe permission defaults", async () => {
  mockTargets({ enabled: true, devices: [MAC] }, { ...OPTIONS, tools: OPTIONS.tools.map((t) => ({ id: t.id, label: t.label, modeLabel: t.modeLabel, models: t.models, modes: t.modes })) });
  render(view());
  expect(await screen.findByText(/Update Lightbulb on Alice's MacBook to choose safe session settings/)).toBeInTheDocument();
  expect(screen.getByPlaceholderText(PLACEHOLDER)).toBeDisabled();
});

it("keeps the Mac chip when the selected Mac is blocked, so another Mac can be picked", async () => {
  m.get.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? { enabled: true, devices: [MAC, MAC2] }
    : { ...OPTIONS, sharing: path.includes("mac_2") });
  render(view());
  expect(await screen.findByText(/Session sharing is off on Alice's MacBook/)).toBeInTheDocument();
  await userEvent.selectOptions(screen.getByLabelText("Mac"), "mac_2");
  expect(await screen.findByRole("button", { name: "Claude Code", pressed: true })).toBeInTheDocument();
  expect(screen.getByPlaceholderText(PLACEHOLDER)).toBeEnabled();
});

it.each([
  ["LAUNCH_FAILED", "Terminal didn't open: LSOpenURLsWithRole() failed", "Terminal didn't open: LSOpenURLsWithRole() failed"],
  // Only a pre-fix desktop app answers DENIED: it could never record an approval.
  ["DENIED", "The start was not approved on the Mac.", "Your Lightbulb desktop app needs an update to start sessions. Update it on your Mac, then try again."],
])("says what failed on the Mac (%s, even beating the HTTP response) and lets you retry", async (code, message, shown) => {
  m.post.mockImplementation(async () => { await new Promise((r) => setTimeout(r, 0)); emit({ operationId: START_ID, state: "failed", updatedAt: "now", result: null, error: { code, message, retryable: false } }); return {}; });
  render(view());
  await screen.findByRole("button", { name: "Claude Code", pressed: true });
  await userEvent.type(screen.getByPlaceholderText(PLACEHOLDER), "go{Enter}");
  expect(await screen.findByText(shown)).toBeInTheDocument();
  expect(screen.getByPlaceholderText(PLACEHOLDER)).toBeEnabled();
});

it("names the Mac by its own name, never the pairing's user id", async () => {
  mockTargets({ enabled: true, devices: [{ ...MAC, name: "Your Mac" }] }, { ...OPTIONS, name: "PP's MacBook Pro" });
  render(view());
  await screen.findByRole("button", { name: "Claude Code", pressed: true });
  await userEvent.type(screen.getByPlaceholderText(PLACEHOLDER), "go{Enter}");
  expect(await screen.findByText("Starting on PP's MacBook Pro · ~/code…")).toBeInTheDocument();
  emit({ operationId: START_ID, state: "succeeded", updatedAt: "now", result: {}, error: null });
  expect(await screen.findByText("Opened in Terminal on PP's MacBook Pro · ~/code. Waiting for the session…")).toBeInTheDocument();
});

it("blocks an over-long first message", async () => {
  render(view());
  await screen.findByRole("button", { name: "Claude Code", pressed: true });
  await userEvent.click(screen.getByPlaceholderText(PLACEHOLDER));
  await userEvent.paste("界".repeat(1400));
  expect(screen.getByText("The first message is too long.")).toBeInTheDocument();
  await userEvent.keyboard("{Enter}");
  expect(m.post).not.toHaveBeenCalled();
});
