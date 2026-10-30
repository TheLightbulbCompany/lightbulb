import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

let hook: Record<string, unknown> = {};
vi.mock("@/hooks/useApps", () => ({ useApps: () => hook }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => false }));
vi.mock("../../workspaceController", () => ({ updatePanelParams: vi.fn() }));
vi.mock("@/components/canvas/AppView", () => ({ AppView: () => <div>app view</div> }));
import { CanvasDockPanel } from "../CanvasDockPanel";

const row = (over: Record<string, unknown> = {}) => ({ app_id: "a1", name: "Invoices", room_id: "r", state: "ready", open_to: "room",
  budget_usd: 1, preview_url: null, live_url: null, live_version: 1, last_opened_at: null, spend_month_cents: 0, can_open: true, ...over });
const base = { apps: [], loading: false, error: null, refresh: vi.fn(), open: vi.fn() };
const panel = (app?: string) => render(<CanvasDockPanel {...({ id: "p", params: { app } } as unknown as React.ComponentProps<typeof CanvasDockPanel>)} />);

beforeEach(() => { hook = { ...base }; });

describe("CanvasDockPanel deep link", () => {
  it("a failed /apps load shows the retry alert, not 'not in this workspace'", () => {
    hook = { ...base, error: new Error("x") };
    panel("a1");
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.queryByText(/isn't in this workspace/)).toBeNull();
  });

  it("a building app is named, not opened", () => {
    hook = { ...base, apps: [row({ state: "building" })] };
    panel("a1");
    expect(screen.getByText(/Invoices · building/)).toBeTruthy();
    expect(screen.queryByText("app view")).toBeNull();
    expect(screen.getByRole("button", { name: "All apps" })).toBeTruthy();
  });

  it("a ready app renders the view", () => {
    hook = { ...base, apps: [row()] };
    panel("a1");
    expect(screen.getByText("app view")).toBeTruthy();
  });
});
