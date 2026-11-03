import { act, render as rtlRender, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { SWRConfig } from "swr";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { LiveEvent, LocalSession } from "@lightbulb/shared";
import { NewSessionButton, NewSessionView } from "../NewSessionButton";

const m = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const auth = vi.hoisted(() => ({ orgId: "org-a" }));
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "alice", orgId: auth.orgId, isLoaded: true }) }));
vi.mock("@/lib/api", () => ({ useApi: () => m }));

const MAC = { deviceId: "mac_1", generation: "gen_1", name: "Alice's MacBook" };
const MAC2 = { deviceId: "mac_2", generation: "gen_2", name: "Alice's Studio" };
const tool = (id: "claude" | "codex", label: string) => ({ id, label, modeLabel: id === "claude" ? "Permission mode" : "Approval mode",
  models: [{ id: `${id}-model`, label: "Big" }], modes: [{ id: `${id}-mode`, label: "Careful" }], defaultMode: `${id}-default`,
  efforts: [{ id: "low", label: "Low" }, { id: "high", label: "High" }], defaultEffort: null });
const OPTIONS = { sharing: true, folders: ["/Users/alice/code", "/Users/alice/site"], tools: [tool("claude", "Claude Code"), tool("codex", "Codex")] };
const session = (sessionId: string, humanUserId = "alice"): LocalSession => ({ sessionId, deviceId: "d", humanUserId, provider: "claude", generation: "g", status: "idle", providerVersion: "2", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null });
const START_ID = "start-1-0000-0000-0000-000000000000";
const startBtn = (label = "Claude Code") => screen.getByRole("button", { name: `Start ${label}` });
const card = (label: string, pressed?: boolean) => screen.findByRole("button", { name: label, pressed });

const wrap = (ui: ReactElement) => <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>{ui}</SWRConfig>;
const render = (ui: ReactElement) => { const view = rtlRender(wrap(ui)); return { ...view, rerender: (next: ReactElement) => view.rerender(wrap(next)) }; };
let handlers: ((event: LiveEvent) => void)[] = [];
const onEvent = (h: (event: LiveEvent) => void) => { handlers.push(h); return () => { handlers = handlers.filter((x) => x !== h); }; };
const emit = (payload: object) => act(() => handlers.forEach((h) => h({ type: "runtime.operation.changed", payload } as unknown as LiveEvent)));
const mockTargets = (targets: object, options: object = OPTIONS) =>
  m.get.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? targets : options);
const view = (sessions: LocalSession[] = [], open = vi.fn(), close = vi.fn()) =>
  <NewSessionView sessions={sessions} onEvent={onEvent} onOpenSession={open} onClose={close} />;
const PENDING_KEY = "lightbulb.newSession.pending.alice:org-a";

beforeEach(() => {
  vi.clearAllMocks(); handlers = []; localStorage.clear();
  delete (window as { __TAURI__?: unknown }).__TAURI__;
  auth.orgId = "org-a";
  vi.spyOn(crypto, "randomUUID").mockReturnValue(START_ID as `${string}-${string}-${string}-${string}-${string}`);
  mockTargets({ enabled: true, devices: [MAC] });
  m.post.mockResolvedValue({ startId: START_ID });
});
afterEach(() => { vi.useRealTimers(); });

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

it("the dialog offers both tools as cards; Start starts with the defaults, an empty prompt, then becomes the new session", async () => {
  const open = vi.fn();
  const ui = render(view([session("old")], open));
  expect(await screen.findByRole("dialog", { name: "New session" })).toHaveTextContent("Opens a live terminal on Alice's MacBook that your workspace can watch.");
  expect(await card("Claude Code", true)).toBeInTheDocument();
  expect(await card("Codex", false)).toBeInTheDocument();
  // One ready Mac: no Mac picker; no first-message box.
  expect(screen.queryByLabelText("Mac")).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Folder")).toHaveValue("/Users/alice/code");
  await userEvent.click(startBtn());
  expect(m.post).toHaveBeenCalledWith("/desktop-sessions/start?source=web", {
    startId: START_ID, deviceId: "mac_1", generation: "gen_1", tool: "claude", folder: "/Users/alice/code",
    model: null, mode: "claude-default", effort: null, prompt: "",
  });
  expect(await screen.findByText(/Starting on Alice's MacBook · ~\/code…/)).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  emit({ operationId: START_ID, state: "succeeded", updatedAt: "now", result: {}, error: null });
  // A teammate's session, and another session I started, are never mistaken for this launch.
  ui.rerender(view([session("old"), session("theirs", "bob"), { ...session("other"), startId: "other-start" }], open));
  expect(open).not.toHaveBeenCalled();
  ui.rerender(view([session("old"), { ...session("new"), startId: START_ID }], open));
  expect(open).toHaveBeenCalledWith("new");
});

it("clicking the Codex card selects it without starting; Start Codex starts codex", async () => {
  render(view([session("old")], vi.fn()));
  await userEvent.click(await card("Codex", false));
  expect(await card("Codex", true)).toBeInTheDocument();
  expect(m.post).not.toHaveBeenCalled();
  await userEvent.click(startBtn("Codex"));
  expect(m.post).toHaveBeenCalledWith("/desktop-sessions/start?source=web", expect.objectContaining({ tool: "codex", folder: "/Users/alice/code", prompt: "" }));
});

it("closing the dialog leaves New session", async () => {
  const close = vi.fn();
  render(view([], vi.fn(), close));
  await card("Claude Code", true);
  await userEvent.keyboard("{Escape}");
  expect(close).toHaveBeenCalled();
});

it("keeps an uncertain launch pending so a second start cannot duplicate it", async () => {
  m.post.mockRejectedValueOnce(new Error("connection dropped"));
  render(view());
  await userEvent.click(await screen.findByRole("button", { name: "Start Claude Code" }));
  expect(await screen.findByText(/Could not confirm whether your Mac started this session/)).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Dismiss and start another" })).toBeInTheDocument();
});

it("keeps a retryable Mac outcome pending because the native launch may have happened", async () => {
  render(view());
  await userEvent.click(await screen.findByRole("button", { name: "Start Claude Code" }));
  emit({ operationId: START_ID, state: "failed", updatedAt: "now", result: null,
    error: { code: "desktop_timeout", message: "Mac reply was lost", retryable: true } });
  expect(await screen.findByText(/Could not confirm whether your Mac started this session/)).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("allows another start when the Mac generation was lost before delivery", async () => {
  render(view());
  await userEvent.click(await screen.findByRole("button", { name: "Start Claude Code" }));
  emit({ operationId: START_ID, state: "failed", updatedAt: "now", result: null,
    error: { code: "desktop_generation_lost", message: "Mac restarted", retryable: true } });
  expect(await screen.findByRole("alert")).toHaveTextContent("Mac restarted");
  expect(startBtn()).toBeEnabled();
});

it("restores an uncertain launch after closing and reopening the web composer", async () => {
  m.post.mockRejectedValueOnce(new Error("connection dropped"));
  const first = render(view());
  await userEvent.click(await screen.findByRole("button", { name: "Start Claude Code" }));
  expect(await screen.findByText(/Could not confirm whether your Mac started this session/)).toBeInTheDocument();
  first.unmount();
  render(view());
  expect(await screen.findByText(/Could not confirm whether your Mac started this session/)).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(m.post).toHaveBeenCalledTimes(1);
});

it("does not carry an unresolved start into a different workspace", async () => {
  m.post.mockRejectedValueOnce(new Error("connection dropped"));
  const ui = render(view());
  await userEvent.click(await screen.findByRole("button", { name: "Start Claude Code" }));
  expect(await screen.findByText(/Could not confirm whether your Mac started this session/)).toBeInTheDocument();
  auth.orgId = "org-b";
  ui.rerender(view());
  await waitFor(() => expect(startBtn()).toBeEnabled());
});

it("restores the last-used Mac, tool and folder, and lists recent folders first", async () => {
  mockTargets({ enabled: true, devices: [MAC, MAC2] });
  localStorage.setItem("lightbulb.newSession.alice", JSON.stringify({ deviceId: "mac_2", tool: "codex", folders: ["/Users/alice/site"] }));
  render(view());
  expect(await card("Codex", true)).toBeInTheDocument();
  expect(screen.getByLabelText("Mac")).toHaveValue("mac_2");
  expect(screen.getByLabelText("Folder")).toHaveValue("/Users/alice/site");
  expect(screen.getAllByRole("option").slice(0, 4).map((o) => o.textContent)).toEqual(["Alice's MacBook", "Alice's Studio", "~/site", "~/code"]);
  // Model and mode sit in a collapsed section, from the tool's closed lists.
  await userEvent.click(screen.getByText("Model and mode"));
  await userEvent.selectOptions(screen.getByLabelText("Approval mode"), "codex-mode");
  await userEvent.selectOptions(screen.getByLabelText("Reasoning effort"), "high");
  await userEvent.click(startBtn("Codex"));
  expect(m.post).toHaveBeenCalledWith("/desktop-sessions/start?source=web", expect.objectContaining({ deviceId: "mac_2", tool: "codex", folder: "/Users/alice/site", model: null, mode: "codex-mode", effort: "high" }));
  expect(JSON.parse(localStorage.getItem("lightbulb.newSession.alice")!)).toEqual({ deviceId: "mac_2", tool: "codex", folders: ["/Users/alice/site"] });
});

it("hides Reasoning effort for a tool that offers none", async () => {
  mockTargets({ enabled: true, devices: [MAC] }, { ...OPTIONS, tools: [{ ...tool("claude", "Claude Code"), efforts: [] }] });
  render(view());
  expect(await screen.findByLabelText("Model")).toBeInTheDocument();
  expect(screen.queryByLabelText("Reasoning effort")).not.toBeInTheDocument();
});

it.each([["corrupt", "{not json"], ["stale", JSON.stringify({ deviceId: "gone", tool: "vim", folders: ["/nope"] })]])("falls back to defaults on a %s last use", async (_, stored) => {
  localStorage.setItem("lightbulb.newSession.alice", stored);
  render(view());
  expect(await card("Claude Code", true)).toBeInTheDocument();
  expect(screen.getByLabelText("Folder")).toHaveValue("/Users/alice/code");
});

it("no ready Mac: one line in the dialog, no tools", async () => {
  mockTargets({ enabled: true, devices: [] });
  render(view());
  const dialog = await screen.findByRole("dialog");
  expect(await screen.findByText("No Mac is ready. Open the Lightbulb desktop app on your Mac and sign in.")).toBeInTheDocument();
  expect(dialog).toContainElement(screen.getByRole("status"));
  expect(screen.queryByRole("button", { name: "Claude Code" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^Start / })).not.toBeInTheDocument();
});

it("sharing off: one line pointing at the switch, no tools", async () => {
  mockTargets({ enabled: true, devices: [MAC] }, { ...OPTIONS, sharing: false });
  render(view());
  expect(await screen.findByText(/Session sharing is off on Alice's MacBook/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Settings → Desktop → Session sharing" })).toHaveAttribute("href", "/settings?panel=desktop");
  expect(screen.queryByRole("button", { name: "Claude Code" })).not.toBeInTheDocument();
});

it("blocks an older Mac that cannot advertise safe permission defaults", async () => {
  mockTargets({ enabled: true, devices: [MAC] }, { ...OPTIONS, tools: OPTIONS.tools.map((t) => ({ id: t.id, label: t.label, modeLabel: t.modeLabel, models: t.models, modes: t.modes })) });
  render(view());
  expect(await screen.findByText(/Update Lightbulb on Alice's MacBook to choose safe session settings/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Claude Code" })).not.toBeInTheDocument();
});

it("keeps the Mac picker when the selected Mac is blocked, so another Mac can be picked", async () => {
  m.get.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? { enabled: true, devices: [MAC, MAC2] }
    : { ...OPTIONS, sharing: path.includes("mac_2") });
  render(view());
  expect(await screen.findByText(/Session sharing is off on Alice's MacBook/)).toBeInTheDocument();
  await userEvent.selectOptions(screen.getByLabelText("Mac"), "mac_2");
  expect(await card("Claude Code", true)).toBeInTheDocument();
  expect(startBtn()).toBeEnabled();
});

it.each([
  ["LAUNCH_FAILED", "Terminal didn't open: LSOpenURLsWithRole() failed", "Terminal didn't open: LSOpenURLsWithRole() failed"],
  // Only a pre-fix desktop app answers DENIED: it could never record an approval.
  ["DENIED", "The start was not approved on the Mac.", "Your Lightbulb desktop app needs an update to start sessions. Update it on your Mac, then try again."],
])("says in the dialog what failed on the Mac (%s, even beating the HTTP response) and lets you retry", async (code, message, shown) => {
  m.post.mockImplementation(async () => { await new Promise((r) => setTimeout(r, 0)); emit({ operationId: START_ID, state: "failed", updatedAt: "now", result: null, error: { code, message, retryable: false } }); return {}; });
  render(view());
  await userEvent.click(await screen.findByRole("button", { name: "Start Claude Code" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(shown);
  expect(screen.getByRole("dialog")).toContainElement(screen.getByRole("alert"));
  await userEvent.click(startBtn());
  expect(m.post).toHaveBeenCalledTimes(2);
});

it("names the Mac by its own name, never the pairing's user id", async () => {
  mockTargets({ enabled: true, devices: [{ ...MAC, name: "Your Mac" }] }, { ...OPTIONS, name: "PP's MacBook Pro" });
  render(view());
  await userEvent.click(await screen.findByRole("button", { name: "Start Claude Code" }));
  expect(await screen.findByText("Starting on PP's MacBook Pro · ~/code…")).toBeInTheDocument();
  emit({ operationId: START_ID, state: "succeeded", updatedAt: "now", result: {}, error: null });
  expect(await screen.findByText("Opened on PP's MacBook Pro · ~/code. Waiting for the session…")).toBeInTheDocument();
});

const desktop = (invoke: (command: string, args?: unknown) => Promise<unknown>) => {
  (window as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke: vi.fn(invoke) } };
  return (window as unknown as { __TAURI__: { core: { invoke: ReturnType<typeof vi.fn> } } }).__TAURI__.core.invoke;
};

it("inside the desktop app it starts a real terminal on this Mac with the chosen tool and folder", async () => {
  mockTargets({ enabled: false, devices: [] });
  const invoke = desktop(async (command) => command === "terminal_options" ? { ...OPTIONS, name: "This Mac" }
    : command === "terminal_panel" ? { live: true } : { holderId: "ab12cd34", tool: "claude" });
  render(<NewSessionButton onOpen={vi.fn()} />);
  expect(await screen.findByRole("button", { name: "New session" })).toBeInTheDocument();
  render(view());
  await userEvent.click(await screen.findByRole("button", { name: "Start Claude Code" }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("terminal_start", { params: {
    startId: START_ID, tool: "claude", folder: "/Users/alice/code", model: null, mode: "claude-default", effort: null, prompt: "",
  } }));
  expect(m.post).not.toHaveBeenCalled();
  expect(await screen.findByText("Running on this Mac. It moves to Sessions when it reports in.")).toBeInTheDocument();
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("terminal_panel", expect.objectContaining({ holderId: "ab12cd34" })));
});

it("a Codex start this Mac holds keeps its terminal past the deadline while its holder lives", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const invoke = desktop(async (command) => command === "terminal_options" ? OPTIONS
    : command === "terminal_panel" ? { live: true } : { holderId: "cx1" });
  render(view());
  await userEvent.click(await card("Codex", false));
  await userEvent.click(startBtn("Codex"));
  const line = "Running on this Mac. It joins Sessions after your first prompt. Quit Codex to start a different session.";
  expect(await screen.findByText(line)).toBeInTheDocument();
  await act(async () => { vi.advanceTimersByTime(151_000); });
  expect(screen.getByText(line)).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Session terminal" })).toBeInTheDocument();
  expect(screen.queryByText(/Could not confirm|never reported in/)).not.toBeInTheDocument();
  expect(invoke).toHaveBeenCalledWith("terminal_panel", expect.objectContaining({ holderId: "cx1" }));
  expect(JSON.parse(localStorage.getItem(PENDING_KEY)!)).toMatchObject({ phase: "opened", holderId: "cx1", tool: "codex" });
});

it("a held start whose holder is gone clears itself and the form returns", async () => {
  let live = true;
  desktop(async (command) => command === "terminal_options" ? OPTIONS
    : command === "terminal_panel" ? { live } : { holderId: "cx1" });
  const first = render(view());
  await userEvent.click(await card("Codex", false));
  await userEvent.click(startBtn("Codex"));
  expect(await screen.findByText(/It joins Sessions after your first prompt/)).toBeInTheDocument();
  first.unmount();
  live = false; // Codex quit before its first prompt
  vi.useFakeTimers({ shouldAdvanceTime: true });
  render(view());
  for (let i = 0; i < 12; i++) await act(async () => { vi.advanceTimersByTime(1_000); });
  expect(await screen.findByRole("dialog", { name: "New session" })).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: "Start Codex" })).toBeEnabled();
  expect(screen.queryByText(/Could not confirm/)).not.toBeInTheDocument();
  expect(localStorage.getItem(PENDING_KEY)).toBeNull();
});

it("ending a held session while its panel stays open returns to the start form", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let live = true;
  desktop(async (command) => command === "terminal_options" ? OPTIONS
    : command === "terminal_panel" ? { live } : { holderId: "cx1" });
  render(view());
  await userEvent.click(await card("Codex", false));
  await userEvent.click(startBtn("Codex"));
  expect(await screen.findByText(/It joins Sessions after your first prompt/)).toBeInTheDocument();
  live = false;
  for (let i = 0; i < 17; i++) await act(async () => { vi.advanceTimersByTime(1_000); });
  expect(screen.getByRole("button", { name: "Start Codex" })).toBeEnabled();
  expect(localStorage.getItem(PENDING_KEY)).toBeNull();
});

it("reopening restores a held start as opened and probes its holder", async () => {
  localStorage.setItem(PENDING_KEY, JSON.stringify({ startId: "old", scope: "alice:org-a", startedAt: Date.now() - 60_000, phase: "opened", holderId: "h9", tool: "codex" }));
  const invoke = desktop(async (command) => command === "terminal_options" ? OPTIONS : { live: true });
  render(view());
  expect(await screen.findByText(/It joins Sessions after your first prompt/)).toBeInTheDocument();
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("terminal_panel", expect.objectContaining({ holderId: "h9" })));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("one missed probe does not clear a held start: the holder must stay gone", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  localStorage.setItem(PENDING_KEY, JSON.stringify({ startId: "old", scope: "alice:org-a", startedAt: Date.now() - 60_000, phase: "opened", holderId: "h9", tool: "codex" }));
  let live = false;
  desktop(async (command) => command === "terminal_options" ? OPTIONS : { live });
  render(view());
  for (let i = 0; i < 3; i++) await act(async () => { vi.advanceTimersByTime(1_000); });
  live = true; // the next re-ask finds it
  for (let i = 0; i < 12; i++) await act(async () => { vi.advanceTimersByTime(1_000); });
  expect(screen.getByText(/It joins Sessions after your first prompt/)).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(JSON.parse(localStorage.getItem(PENDING_KEY)!)).toMatchObject({ holderId: "h9" });
});

it("a held start older than six hours is still restored: the holder decides, not its age", async () => {
  localStorage.setItem(PENDING_KEY, JSON.stringify({ startId: "old", scope: "alice:org-a", startedAt: Date.now() - 7 * 60 * 60 * 1000, phase: "opened", holderId: "h9", tool: "codex" }));
  const invoke = desktop(async (command) => command === "terminal_options" ? OPTIONS : { live: true });
  render(view());
  expect(await screen.findByText(/It joins Sessions after your first prompt/)).toBeInTheDocument();
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("terminal_panel", expect.objectContaining({ holderId: "h9" })));
});

it("a start nobody could confirm can be dismissed, so the view is not locked for hours", async () => {
  localStorage.setItem(PENDING_KEY, JSON.stringify({ startId: "old", scope: "alice:org-a", startedAt: Date.now() - 60_000, phase: "opened" }));
  render(view());
  expect(await screen.findByText(/Could not confirm whether your Mac started this session/)).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Dismiss and start another" }));
  await waitFor(() => expect(startBtn()).toBeEnabled());
  expect(localStorage.getItem(PENDING_KEY)).toBeNull();
});

it("a local start that fails shows why", async () => {
  desktop(async (command) => { if (command === "terminal_options") return OPTIONS; throw new Error("That folder is not one of this Mac's approved folders."); });
  render(view());
  await userEvent.click(await screen.findByRole("button", { name: "Start Claude Code" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("That folder is not one of this Mac's approved folders.");
});

it("an older desktop app without terminals keeps the remote start", async () => {
  desktop(async () => { throw new Error("unknown command"); });
  render(view());
  await userEvent.click(await screen.findByRole("button", { name: "Start Claude Code" }));
  await waitFor(() => expect(m.post).toHaveBeenCalledWith("/desktop-sessions/start?source=web", expect.objectContaining({ deviceId: "mac_1" })));
});

it("inside the desktop app it shows the terminal command and the PATH line it needs", async () => {
  const line = `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc`;
  desktop(async (command) => command === "terminal_options" ? { ...OPTIONS, cli: { pathLine: line } } : { linked: true });
  render(view());
  expect(await screen.findByText("lightbulb claude")).toBeInTheDocument();
  expect(screen.getByText(line)).toBeInTheDocument();
});

it("a desktop app without the command shows no terminal hint", async () => {
  desktop(async (command) => command === "terminal_options" ? OPTIONS : { linked: true });
  render(view());
  expect(await screen.findByRole("combobox", { name: "Folder" })).toBeInTheDocument();
  expect(screen.queryByText("lightbulb claude")).toBeNull();
});
