import { act, render as rtlRender, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { SWRConfig } from "swr";
import { beforeEach, expect, it, vi } from "vitest";
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
const startBtn = () => screen.getByRole("button", { name: "Claude Code" });

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
  delete (window as { __TAURI__?: unknown }).__TAURI__;
  auth.orgId = "org-a";
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

it("Clicking Claude Code starts with the defaults, an empty prompt, then becomes the new session", async () => {
  const open = vi.fn();
  const ui = render(view([session("old")], open));
  // One ready Mac: no Mac chip; model, mode and effort stay behind the settings button.
  expect(await screen.findByRole("button", { name: "Claude Code", pressed: true })).toBeInTheDocument();
  expect(screen.queryByLabelText("Mac")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Model")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Folder")).toHaveValue("/Users/alice/code");
  await userEvent.click(await screen.findByRole("button", { name: "Claude Code" }));
  expect(m.post).toHaveBeenCalledWith("/desktop-sessions/start?source=web", {
    startId: START_ID, deviceId: "mac_1", generation: "gen_1", tool: "claude", folder: "/Users/alice/code",
    model: null, mode: "claude-default", effort: null, prompt: "",
  });
  expect(await screen.findByText(/Starting on Alice's MacBook · ~\/code…/)).toBeInTheDocument();
  emit({ operationId: START_ID, state: "succeeded", updatedAt: "now", result: {}, error: null });
  // A teammate's session, and another session I started, are never mistaken for this launch.
  ui.rerender(view([session("old"), session("theirs", "bob"), { ...session("other"), startId: "other-start" }], open));
  expect(open).not.toHaveBeenCalled();
  ui.rerender(view([session("old"), { ...session("new"), startId: START_ID }], open));
  expect(open).toHaveBeenCalledWith("new");
});

it("clicking the Codex chip starts codex in the chosen folder with an empty prompt", async () => {
  render(view([session("old")], vi.fn()));
  await userEvent.click(await screen.findByRole("button", { name: "Codex" }));
  expect(m.post).toHaveBeenCalledWith("/desktop-sessions/start?source=web", expect.objectContaining({ tool: "codex", folder: "/Users/alice/code", prompt: "" }));
});

it("keeps an uncertain launch pending so a second click cannot duplicate it", async () => {
  m.post.mockRejectedValueOnce(new Error("connection dropped"));
  render(view());
  await screen.findByRole("button", { name: "Claude Code", pressed: true });
  await userEvent.click(await screen.findByRole("button", { name: "Claude Code" }));
  expect(await screen.findByText(/Could not confirm whether your Mac started this session/)).toBeInTheDocument();
  expect(startBtn()).toBeDisabled();
});

it("keeps a retryable Mac outcome pending because the native launch may have happened", async () => {
  render(view());
  await screen.findByRole("button", { name: "Claude Code", pressed: true });
  await userEvent.click(await screen.findByRole("button", { name: "Claude Code" }));
  emit({ operationId: START_ID, state: "failed", updatedAt: "now", result: null,
    error: { code: "desktop_timeout", message: "Mac reply was lost", retryable: true } });
  expect(await screen.findByText(/Could not confirm whether your Mac started this session/)).toBeInTheDocument();
  expect(startBtn()).toBeDisabled();
});

it("allows another start when the Mac generation was lost before delivery", async () => {
  render(view());
  await screen.findByRole("button", { name: "Claude Code", pressed: true });
  await userEvent.click(await screen.findByRole("button", { name: "Claude Code" }));
  emit({ operationId: START_ID, state: "failed", updatedAt: "now", result: null,
    error: { code: "desktop_generation_lost", message: "Mac restarted", retryable: true } });
  expect(await screen.findByText("Mac restarted")).toBeInTheDocument();
  expect(startBtn()).toBeEnabled();
});

it("restores an uncertain launch after closing and reopening the web composer", async () => {
  m.post.mockRejectedValueOnce(new Error("connection dropped"));
  const first = render(view());
  await screen.findByRole("button", { name: "Claude Code", pressed: true });
  await userEvent.click(await screen.findByRole("button", { name: "Claude Code" }));
  expect(await screen.findByText(/Could not confirm whether your Mac started this session/)).toBeInTheDocument();
  first.unmount();
  render(view());
  expect(await screen.findByText(/Could not confirm whether your Mac started this session/)).toBeInTheDocument();
  expect(startBtn()).toBeDisabled();
  expect(m.post).toHaveBeenCalledTimes(1);
});

it("does not carry an unresolved start into a different workspace", async () => {
  m.post.mockRejectedValueOnce(new Error("connection dropped"));
  const ui = render(view());
  await screen.findByRole("button", { name: "Claude Code", pressed: true });
  await userEvent.click(await screen.findByRole("button", { name: "Claude Code" }));
  expect(await screen.findByText(/Could not confirm whether your Mac started this session/)).toBeInTheDocument();
  auth.orgId = "org-b";
  ui.rerender(view());
  await waitFor(() => expect(startBtn()).toBeEnabled());
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
  await userEvent.click(await screen.findByRole("button", { name: "Codex" }));
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
  expect(screen.queryByRole("button", { name: "Claude Code" })).not.toBeInTheDocument(); // no chips when blocked
});

it("sharing off: one line pointing at the switch, composer disabled", async () => {
  mockTargets({ enabled: true, devices: [MAC] }, { ...OPTIONS, sharing: false });
  render(view());
  expect(await screen.findByText(/Session sharing is off on Alice's MacBook/)).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Settings → Native terminal sessions" })).toHaveAttribute("href", "/settings");
  expect(screen.queryByRole("button", { name: "Claude Code" })).not.toBeInTheDocument(); // no chips when blocked
});

it("blocks an older Mac that cannot advertise safe permission defaults", async () => {
  mockTargets({ enabled: true, devices: [MAC] }, { ...OPTIONS, tools: OPTIONS.tools.map((t) => ({ id: t.id, label: t.label, modeLabel: t.modeLabel, models: t.models, modes: t.modes })) });
  render(view());
  expect(await screen.findByText(/Update Lightbulb on Alice's MacBook to choose safe session settings/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Claude Code" })).not.toBeInTheDocument(); // no chips when blocked
});

it("keeps the Mac chip when the selected Mac is blocked, so another Mac can be picked", async () => {
  m.get.mockImplementation(async (path: string) => path === "/desktop-sessions/targets" ? { enabled: true, devices: [MAC, MAC2] }
    : { ...OPTIONS, sharing: path.includes("mac_2") });
  render(view());
  expect(await screen.findByText(/Session sharing is off on Alice's MacBook/)).toBeInTheDocument();
  await userEvent.selectOptions(screen.getByLabelText("Mac"), "mac_2");
  expect(await screen.findByRole("button", { name: "Claude Code", pressed: true })).toBeInTheDocument();
  expect(startBtn()).toBeEnabled();
});

it.each([
  ["LAUNCH_FAILED", "Terminal didn't open: LSOpenURLsWithRole() failed", "Terminal didn't open: LSOpenURLsWithRole() failed"],
  // Only a pre-fix desktop app answers DENIED: it could never record an approval.
  ["DENIED", "The start was not approved on the Mac.", "Your Lightbulb desktop app needs an update to start sessions. Update it on your Mac, then try again."],
])("says what failed on the Mac (%s, even beating the HTTP response) and lets you retry", async (code, message, shown) => {
  m.post.mockImplementation(async () => { await new Promise((r) => setTimeout(r, 0)); emit({ operationId: START_ID, state: "failed", updatedAt: "now", result: null, error: { code, message, retryable: false } }); return {}; });
  render(view());
  await screen.findByRole("button", { name: "Claude Code", pressed: true });
  await userEvent.click(await screen.findByRole("button", { name: "Claude Code" }));
  expect(await screen.findByText(shown)).toBeInTheDocument();
  expect(startBtn()).toBeEnabled();
});

it("names the Mac by its own name, never the pairing's user id", async () => {
  mockTargets({ enabled: true, devices: [{ ...MAC, name: "Your Mac" }] }, { ...OPTIONS, name: "PP's MacBook Pro" });
  render(view());
  await screen.findByRole("button", { name: "Claude Code", pressed: true });
  await userEvent.click(await screen.findByRole("button", { name: "Claude Code" }));
  expect(await screen.findByText("Starting on PP's MacBook Pro · ~/code…")).toBeInTheDocument();
  emit({ operationId: START_ID, state: "succeeded", updatedAt: "now", result: {}, error: null });
  expect(await screen.findByText("Opened on PP's MacBook Pro · ~/code. Waiting for the session…")).toBeInTheDocument();
});

it("renders no first-message box and no Start button", async () => {
  render(view());
  await screen.findByRole("button", { name: "Claude Code" });
  expect(screen.queryByRole("button", { name: "Start" })).not.toBeInTheDocument();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
});

const desktop = (invoke: (command: string, args?: unknown) => Promise<unknown>) => {
  (window as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke: vi.fn(invoke) } };
  return (window as unknown as { __TAURI__: { core: { invoke: ReturnType<typeof vi.fn> } } }).__TAURI__.core.invoke;
};

it("inside the desktop app it starts a real terminal on this Mac with the chosen tool and folder", async () => {
  mockTargets({ enabled: false, devices: [] });
  const invoke = desktop(async (command) => command === "terminal_options" ? { ...OPTIONS, name: "This Mac" }
    : command === "terminal_panel" ? { live: true } : { holderId: "ab12cd34", tool: "codex" });
  render(<NewSessionButton onOpen={vi.fn()} />);
  expect(await screen.findByRole("button", { name: "New session" })).toBeInTheDocument();
  render(view());
  await userEvent.click(await screen.findByRole("button", { name: "Codex", pressed: false }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("terminal_start", { params: {
    startId: START_ID, tool: "codex", folder: "/Users/alice/code", model: null, mode: "codex-default", effort: null, prompt: "",
  } }));
  expect(m.post).not.toHaveBeenCalled();
  expect(await screen.findByText("Running on this Mac. It moves to Sessions when it reports in.")).toBeInTheDocument();
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("terminal_panel", expect.objectContaining({ holderId: "ab12cd34" })));
});

it("a local start that fails shows why", async () => {
  desktop(async (command) => { if (command === "terminal_options") return OPTIONS; throw new Error("That folder is not one of this Mac's approved folders."); });
  render(view());
  await userEvent.click(await screen.findByRole("button", { name: "Claude Code" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("That folder is not one of this Mac's approved folders.");
});

it("an older desktop app without terminals keeps the remote start", async () => {
  desktop(async () => { throw new Error("unknown command"); });
  render(view());
  await userEvent.click(await screen.findByRole("button", { name: "Claude Code" }));
  await waitFor(() => expect(m.post).toHaveBeenCalledWith("/desktop-sessions/start?source=web", expect.objectContaining({ deviceId: "mac_1" })));
});

it("a start nobody could confirm can be dismissed, so the view is not locked for hours", async () => {
  localStorage.setItem("lightbulb.newSession.pending.alice:org-a", JSON.stringify({ startId: "old", scope: "alice:org-a", startedAt: Date.now() - 60_000, phase: "opened" }));
  render(view());
  expect(await screen.findByText(/Could not confirm whether your Mac started this session/)).toBeInTheDocument();
  expect(startBtn()).toBeDisabled();
  await userEvent.click(screen.getByRole("button", { name: "Dismiss and start another" }));
  await waitFor(() => expect(startBtn()).toBeEnabled());
  expect(localStorage.getItem("lightbulb.newSession.pending.alice:org-a")).toBeNull();
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
