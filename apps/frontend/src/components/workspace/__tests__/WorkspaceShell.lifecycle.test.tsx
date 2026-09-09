import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEffect, useState, type ReactNode } from "react";
import type { DockviewApi, DockviewReadyEvent, SerializedDockview } from "dockview-react";

const state = vi.hoisted(() => ({
  userId: "user-a" as string | null,
  orgId: "org-a" as string | null,
  signedIn: true,
  phase: "ready",
  defaultId: "friday-a",
  settled: true,
  recovering: false,
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ userId: state.userId, isSignedIn: state.signedIn }),
  useOrganization: () => ({ organization: state.orgId ? { id: state.orgId } : null }),
  useUser: () => ({ user: null, isLoaded: true }),
}));
vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams(window.location.search) }));
vi.mock("@/hooks/useOnboardingGate", () => ({ useOnboardingGate: () => state.phase }));
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [{ id: state.defaultId }], defaultId: state.defaultId, isSettled: state.settled }),
}));
vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));
vi.mock("../useBrowserToolAutoOpen", () => ({ useBrowserToolAutoOpen: () => {} }));
vi.mock("@/components/chat/shell/useUserSyncOnce", () => ({ useUserSyncOnce: () => {} }));
vi.mock("@/components/chat/shell/useCheckoutReturn", () => ({ useCheckoutReturn: () => ({}) }));
vi.mock("@/components/chat/shell/useGetStartedDerived", () => ({ useGetStartedDerived: () => ({ chatReady: false }) }));
vi.mock("@/components/marketplace/drawer/MarketplaceDrawerProvider", () => ({ useMarketplaceDrawer: () => ({}) }));
vi.mock("@/components/chat/InlineTeamsNav", () => ({ InlineTeamsNavProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock("@/components/teams/TeamsEventsProvider", () => ({ TeamsEventsProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock("@/components/teams/command-palette/CommandPaletteProvider", () => ({ CommandPaletteProvider: ({ children }: { children: ReactNode }) => children }));
vi.mock("@/components/teams/TeamsPanelRouter", () => ({ preloadTeamsPanels: () => {} }));
vi.mock("@/components/desktop/DesktopPermissionsGate", () => ({ DesktopPermissionsGate: () => null }));
vi.mock("@/components/chat/TrialBanner", () => ({ TrialBanner: () => null }));
vi.mock("@/components/chat/OutOfCreditsBanner", () => ({ OutOfCreditsBanner: () => null }));
vi.mock("@/components/chat/LaunchJoinBanner", () => ({ LaunchJoinBanner: () => null }));
vi.mock("@/components/chat/LaunchJoinModal", () => ({ LaunchJoinModal: () => null }));
vi.mock("@/components/getstarted/GetStarted", () => ({ GetStarted: () => null }));
vi.mock("../panelComponents", () => ({ PANEL_COMPONENTS: {} }));
vi.mock("../WorkspaceHeader", () => ({ WorkspaceHeader: () => null }));
vi.mock("../WorkspaceSidebar", () => ({
  WorkspaceSidebar: ({ onRecoveryReprovision }: { onRecoveryReprovision: () => void }) => <button onClick={onRecoveryReprovision}>Recover</button>,
}));

// Keep the real ProvisioningStepper: its actual loader branch must destroy
// the dock, whereas changing only its recovery trigger must preserve it.
vi.mock("@/hooks/useProvisioningState", () => ({ useProvisioningState: () => ({
  phase: "normal", blocked: null, refresh: vi.fn(),
  container: {
    container_status: state.recovering ? "provisioning" : "running",
    container_substatus: state.recovering ? "task_running" : "gateway_healthy",
    runtime_phase: state.recovering ? "provisioning" : "ready",
    lifecycle: state.recovering ? "provisioning" : "ready",
    onboarded: true, teams_state: "ready",
  },
}) }));
vi.mock("@/hooks/useGateway", () => ({ useGateway: () => ({ isConnected: true, onEvent: () => () => {} }) }));
vi.mock("@/hooks/useGatewayRpc", () => ({ useGatewayRpc: () => ({ data: { ok: true }, error: null }) }));
vi.mock("@/hooks/useBilling", () => ({ useBilling: () => ({ isLoading: false, isSubscribed: true, account: { provider_choice: "bedrock_claude" }, refresh: vi.fn() }) }));
vi.mock("@/hooks/useCredits", () => ({ useCredits: () => ({}), isCreditsBlocked: () => false }));
vi.mock("@/hooks/useLandingBriefReplay", () => ({ useLandingBriefReplay: () => ({ replaying: false, orgDraftVersion: 0 }) }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn().mockResolvedValue({}), post: vi.fn().mockResolvedValue({}) }), ApiError: class extends Error {} }));
vi.mock("swr", () => ({ default: () => ({}), useSWRConfig: () => ({ mutate: vi.fn() }) }));
vi.mock("@/components/onboarding/FirstRunFunnel", () => ({ FirstRunFunnel: () => null }));

import { WorkspaceShell } from "../WorkspaceShell";
import { _resetControllerForTests, getWorkspaceSnapshot, openPanel } from "../workspaceController";
import { readLayout, writeLayout } from "../layoutStorage";

const log: string[] = [];
const docks: ReturnType<typeof makeDock>[] = [];
function layout(panels: SerializedDockview["panels"]): SerializedDockview {
  return { panels, grid: { root: { type: "branch", data: [] }, height: 600, width: 800, orientation: "HORIZONTAL" as SerializedDockview["grid"]["orientation"] } };
}
function makeDock() {
  const id = docks.length;
  const listeners = new Map<string, Set<() => void>>();
  const lateLayout: Array<() => void> = [];
  let alive = true;
  let active: string | undefined;
  let saved = layout({});
  const event = (name: string) => (fn: () => void) => {
    if (!listeners.has(name)) listeners.set(name, new Set());
    listeners.get(name)!.add(fn);
    if (name === "layout") lateLayout.push(fn);
    return { dispose: () => { listeners.get(name)!.delete(fn); } };
  };
  const emit = (name: string) => listeners.get(name)?.forEach((fn) => fn());
  const panel = (panelId: string) => ({
    ...saved.panels[panelId],
    api: {
      setActive: () => { active = panelId; log.push(`focus:${id}:${panelId}`); emit("active"); },
      setTitle: (title: string) => { saved.panels[panelId].title = title; },
      updateParameters: (params: object) => { Object.assign(saved.panels[panelId].params ??= {}, params); },
    },
  });
  const api = {
    get panels() { return Object.keys(saved.panels).map(panel); },
    get activePanel() { return active ? panel(active) : undefined; },
    groups: [],
    getPanel: (panelId: string) => saved.panels[panelId] ? panel(panelId) : undefined,
    addPanel: (opts: { id: string; component: string; params: object; title: string }) => {
      saved.panels[opts.id] = { id: opts.id, contentComponent: opts.component, params: opts.params, title: opts.title };
      active = opts.id; emit("add"); return panel(opts.id);
    },
    fromJSON: (value: SerializedDockview) => { log.push(`restore:${id}`); saved = structuredClone(value); active = Object.keys(saved.panels)[0]; emit("add"); },
    toJSON: () => { log.push(`serialize:${id}:${alive}`); if (!alive) throw new Error("dead dock"); return saved; },
    clear: () => { saved = layout({}); },
    onDidAddGroup: event("group"), onDidAddPanel: event("add"),
    onDidActivePanelChange: event("active"), onDidRemovePanel: event("remove"), onDidLayoutChange: event("layout"),
  };
  return {
    api: api as unknown as DockviewApi,
    emitLayout: () => emit("layout"),
    lateLayout: () => lateLayout.forEach((fn) => fn()),
    listenerCount: () => [...listeners.values()].reduce((n, set) => n + set.size, 0),
    dispose: () => { log.push(`destroy:${id}:${getWorkspaceSnapshot().ready}`); alive = false; },
  };
}
vi.mock("dockview-react", () => ({
  themeLight: { name: "light", className: "dockview-theme-light" },
  DockviewReact: function DockBoundary({ onReady }: { onReady: (event: DockviewReadyEvent) => void }) {
    const [draft, setDraft] = useState("");
    useEffect(() => {
      const dock = makeDock(); docks.push(dock);
      log.push(`mount:${docks.length - 1}:${getWorkspaceSnapshot().ready}`);
      onReady({ api: dock.api } as DockviewReadyEvent);
      return () => dock.dispose();
      // Match DockviewReact's mount-only onReady contract.
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);
    return <input aria-label="Draft" value={draft} onChange={(e) => setDraft(e.target.value)} />;
  },
}));

beforeEach(() => {
  vi.useFakeTimers();
  Object.assign(state, { userId: "user-a", orgId: "org-a", signedIn: true, phase: "ready", defaultId: "friday-a", settled: true, recovering: false });
  docks.length = 0; log.length = 0;
  localStorage.clear(); sessionStorage.clear(); _resetControllerForTests();
  window.history.replaceState({}, "", "/chat");
  window.matchMedia = vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe("authenticated workspace lifecycle", () => {
  it.each(["owner", "same-org user", "personal"])("flushes and detaches before restoring on %s turnover", (turnover) => {
    writeLayout("org-b", layout({
      browser: { id: "browser", contentComponent: "browser" },
      "chat:friday-a": { id: "chat:friday-a", contentComponent: "chat", params: { agentId: "friday-a" } },
    }));
    const view = render(<WorkspaceShell />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "old draft" } });
    openPanel("teams-issues"); docks[0].emitLayout();
    if (turnover === "owner") state.orgId = "org-b";
    else if (turnover === "personal") state.orgId = null;
    else state.userId = "user-b";
    state.defaultId = "friday-b";
    window.history.replaceState({}, "", "/chat?agent=friday-b");
    view.rerender(<WorkspaceShell />);
    expect(docks).toHaveLength(2);
    expect(screen.getByRole("textbox")).toHaveValue("");
    expect(readLayout("org-a")?.panels["teams-issues"]).toBeDefined();
    expect(log.indexOf("serialize:0:true")).toBeLessThan(log.indexOf("destroy:0:false"));
    expect(log.indexOf("destroy:0:false")).toBeLessThan(log.indexOf("mount:1:false"));
    expect(docks[0].listenerCount()).toBe(0);
    expect(getWorkspaceSnapshot().focusedChatAgentId).toBe("friday-b");
    if (turnover === "owner") {
      expect(log).toContain("restore:1");
      expect(getWorkspaceSnapshot().openPanelIds).toEqual(["browser", "chat:friday-b"]);
    } else {
      expect(getWorkspaceSnapshot().openPanelIds).not.toContain("chat:friday-b");
    }
    const stored = localStorage.getItem("isol8.workspace.layout.v3:org-a");
    act(() => { docks[0].lateLayout(); vi.advanceTimersByTime(600); });
    expect(log.filter((entry) => entry.startsWith("serialize:0:"))).toEqual(["serialize:0:true"]);
    expect(localStorage.getItem("isol8.workspace.layout.v3:org-a")).toBe(stored);
    openPanel("teams-agents"); docks[1].emitLayout();
    act(() => vi.advanceTimersByTime(600));
    expect(readLayout(state.orgId ?? state.userId!)?.panels["teams-agents"]).toBeDefined();
    expect(getWorkspaceSnapshot().ready).toBe(true);
  });

  it.each(["signed-out", "missing-user", "loading", "redirecting", "auto-activating"])("tears down and never uses an anonymous scope when %s", (phase) => {
    const view = render(<WorkspaceShell />);
    docks[0].emitLayout();
    if (phase === "signed-out") { state.signedIn = false; state.userId = null; state.orgId = null; }
    else if (phase === "missing-user") state.userId = null;
    else state.phase = phase;
    view.rerender(<WorkspaceShell />);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(getWorkspaceSnapshot().ready).toBe(false);
    expect(readLayout("org-a")).not.toBeNull();
    expect(localStorage.getItem("isol8.workspace.layout.v3:anon")).toBeNull();
    expect(docks[0].listenerCount()).toBe(0);
  });

  it.each([false, true])("preserves drafts/trigger identity and restores current refs after the real loader (settled=%s)", (settled) => {
    const view = render(<WorkspaceShell />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "keep typing" } }); input.focus();
    view.rerender(<WorkspaceShell />);
    expect(screen.getByRole("textbox")).toBe(input);
    expect(input).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Recover" }));
    expect(screen.getByRole("textbox")).toBe(input);
    expect(input).toHaveValue("keep typing");
    expect(input).toHaveFocus();
    docks[0].emitLayout(); state.recovering = true;
    view.rerender(<WorkspaceShell />);
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(getWorkspaceSnapshot().ready).toBe(false);
    expect(log).toContain("serialize:0:true");
    state.recovering = false; state.defaultId = "friday-new"; state.settled = settled;
    window.history.replaceState({}, "", "/chat?agent=friday-new");
    view.rerender(<WorkspaceShell />);
    expect(docks).toHaveLength(2);
    expect(log).toContain("restore:1");
    expect(getWorkspaceSnapshot().openPanelIds).not.toContain("chat:friday-new");
    expect(log.filter((entry) => entry.startsWith("focus:1:"))).toHaveLength(settled ? 1 : 0);
    state.settled = true; view.rerender(<WorkspaceShell />);
    expect(getWorkspaceSnapshot().focusedChatAgentId).toBe("friday-new");
    expect(log.filter((entry) => entry.startsWith("focus:1:"))).toEqual(["focus:1:chat:__default__"]);
    view.rerender(<WorkspaceShell />);
    expect(log.filter((entry) => entry.startsWith("focus:1:"))).toHaveLength(1);
  });

  it("reconciles a generic URL command once per real restore and preserves the existing history behavior", () => {
    window.history.replaceState({}, "", "/chat?panel=teams-issues&new=task");
    const replace = vi.spyOn(window.history, "replaceState");
    const push = vi.spyOn(window.history, "pushState");
    const view = render(<WorkspaceShell />);
    expect(getWorkspaceSnapshot().activePanelId).toBe("teams-issues");
    expect(window.location.search).toBe("?panel=teams-issues");
    expect(replace).toHaveBeenCalledTimes(1);
    view.rerender(<WorkspaceShell />);
    expect(log.filter((entry) => entry === "focus:0:teams-issues")).toHaveLength(0);
    docks[0].emitLayout(); state.recovering = true; view.rerender(<WorkspaceShell />);
    state.recovering = false; view.rerender(<WorkspaceShell />);
    expect(log.filter((entry) => entry === "focus:1:teams-issues")).toHaveLength(1);
    expect(getWorkspaceSnapshot().activePanelId).toBe("teams-issues");
    expect(replace).toHaveBeenCalledTimes(1);
    expect(push).not.toHaveBeenCalled();
  });
});
