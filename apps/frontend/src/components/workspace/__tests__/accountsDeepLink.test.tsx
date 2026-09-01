// `?panel=teams-accounts` — the WHOLE desktop deep-link chain, not the registry.
//
// Why this file exists: two deep-link bugs shipped green because every test
// called `describePanel` directly, while the real desktop path is
// URL → deriveReconcilerAction → openPanel → dockview.addPanel →
// TeamsDockPanel(params) → TeamsPanelRouter → the panel component. A registry
// entry proves the FIRST hop only. This walks all of them, feeding each hop
// the literal output of the one before it — the addPanel record dockview would
// have received is what TeamsDockPanel is then mounted with.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { SWRConfig } from "swr";
import type { DockviewApi } from "dockview-react";

vi.mock("@/lib/shellNav", () => ({
  pushSearchParams: vi.fn(),
  replaceSearchParams: vi.fn(),
}));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsWorkspaceStatus: () => ({ kind: "ready" }),
}));
vi.mock("@/components/teams/TeamsStatusOverlays", () => ({
  ProvisioningOverlay: () => null,
  SubscribeOverlay: () => null,
  ErrorOverlay: () => null,
}));

// AccountsPanel's own dependencies. The panel itself is NOT stubbed — the
// point of this suite is that the real component mounts at the end of the
// chain.
const get = vi.fn(async () => ({
  money_moves: true,
  outreach: true,
  publishing: true,
  provisioning: true,
}));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get, patch: vi.fn(), post: vi.fn() }), BACKEND_URL: "" }));
vi.mock("@/lib/notify", () => ({
  notifyApiError: vi.fn(),
  notifySuccess: vi.fn(),
  notifyError: vi.fn(),
}));
vi.mock("@/components/settings/CompanyStripeSection", () => ({
  CompanyStripeSection: () => <div data-testid="company-stripe" />,
}));
vi.mock("@/components/settings/ConnectionsList", () => ({
  ConnectionsList: () => <div data-testid="connections-list" />,
}));
vi.mock("@/components/settings/ProvisionedResources", () => ({
  useProvisioningRail: () => ({
    byProvider: new Map(),
    workflowsByProvider: new Map(),
    accountByProvider: new Map(),
    error: null,
    providers: [],
    refresh: vi.fn(),
  }),
}));
vi.mock("@/hooks/useConnections", () => ({ useConnections: () => ({ connections: [] }) }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => true }));

import { deriveReconcilerAction } from "../workspaceShellReconciler";
import { openPanel, attachDockviewApi, _resetControllerForTests } from "../workspaceController";
import { PANEL_COMPONENT_KEYS } from "../panelRegistry";
import { TeamsDockPanel } from "../panels/TeamsDockPanel";

type AddPanelCall = {
  id: string;
  component: string;
  title: string;
  params: Record<string, string>;
};

// Trimmed fake DockviewApi — only openPanel's add path is exercised.
function makeFakeApi() {
  const calls: AddPanelCall[] = [];
  const api = {
    getPanel: () => undefined,
    addPanel: vi.fn((opts: AddPanelCall) => {
      calls.push(opts);
      return { id: opts.id, api: { setActive: vi.fn(), setTitle: vi.fn(), updateParameters: vi.fn() } };
    }),
    onDidActivePanelChange: () => ({ dispose: vi.fn() }),
    onDidAddPanel: () => ({ dispose: vi.fn() }),
    onDidRemovePanel: () => ({ dispose: vi.fn() }),
    onDidLayoutChange: () => ({ dispose: vi.fn() }),
    panels: [] as unknown[],
    groups: [] as unknown[],
  };
  return { api: api as unknown as DockviewApi, calls };
}

function makeDockProps(params: Record<string, string>) {
  return {
    api: {
      id: "teams-accounts",
      onDidParametersChange: () => ({ dispose: vi.fn() }),
      updateParameters: vi.fn(),
    },
    containerApi: {},
    params,
  } as unknown as Parameters<typeof TeamsDockPanel>[0];
}

beforeEach(() => {
  _resetControllerForTests();
  vi.clearAllMocks();
});

describe("?panel=teams-accounts — reconciler → dockview → mounted panel", () => {
  it("walks the whole chain and ends with AccountsPanel on screen", async () => {
    // 1. The reconciler turns the URL into a command + panel key.
    const decision = deriveReconcilerAction("?panel=teams-accounts", null);
    expect(decision).not.toBeNull();
    expect(decision!.command?.descriptor.panelId).toBe("teams-accounts");
    expect(decision!.panelKey).toBe("teams-accounts");

    // 2. openPanel hands dockview a component the shell actually registers.
    const { api, calls } = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openPanel(decision!.panelKey!, decision!.durableParams, {
      oneShots: decision!.command!.oneShots,
    });
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({
      id: "teams-accounts",
      component: "teams",
      title: "Accounts",
      params: { teamsKey: "accounts" },
    });
    expect(PANEL_COMPONENT_KEYS.has(calls[0].component)).toBe(true);

    // 3. Mount the dock panel with the EXACT params dockview recorded — the
    // hop the two shipped deep-link bugs broke.
    render(
      <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
        <TeamsDockPanel {...makeDockProps(calls[0].params)} />
      </SWRConfig>,
    );

    // 4. The real AccountsPanel is on screen (code-split via next/dynamic, so
    // findBy, not getBy) — not TeamsPanelRouter's "Unknown panel" fallback.
    expect(await screen.findByRole("heading", { name: "Accounts", level: 1 })).toBeInTheDocument();
    expect(screen.queryByText(/Unknown panel/)).not.toBeInTheDocument();
  });
});
