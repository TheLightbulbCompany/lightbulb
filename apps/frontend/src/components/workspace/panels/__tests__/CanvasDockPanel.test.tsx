import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

let hook: Record<string, unknown> = {};
vi.mock("@/hooks/useApps", () => ({ useApps: () => hook }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => false }));
vi.mock("../../workspaceController", () => ({ updatePanelParams: vi.fn() }));
vi.mock("@/components/canvas/AppView", () => ({ AppView: () => <div>app view</div>, AskAgent: () => <button type="button">Ask the agent to fix it</button> }));
vi.mock("@/components/canvas/AppConversation", () => ({ AppConversation: ({ app }: { app: { app_id: string } }) => <aside aria-label="Conversation">{app.app_id}</aside> }));
import { CanvasDockPanel } from "../CanvasDockPanel";

const row = (over: Record<string, unknown> = {}) => ({ app_id: "a1", name: "Invoices", room_id: "r", room_name: "finance", state: "ready", open_to: "room",
  budget_usd: 1, preview_url: null, live_url: null, live_version: 1, last_opened_at: null, spend_month_cents: 0, can_open: true, ...over });
const base = { apps: [], loading: false, error: null, refresh: vi.fn(), open: vi.fn() };
const panel = (app?: string) => render(<CanvasDockPanel {...({ id: "p", params: { app } } as unknown as React.ComponentProps<typeof CanvasDockPanel>)} />);

beforeEach(() => { hook = { ...base }; });

describe("CanvasDockPanel deep link", () => {
  it("a failed /apps load shows the retry alert, not 'not in this workspace'", () => {
    hook = { ...base, error: new Error("x") };
    panel("a1");
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    expect(screen.queryByText(/isn't in this workspace/)).toBeNull();
  });

  it("a stuck building app says where it stopped, like the list and the view", () => {
    hook = { ...base, apps: [row({ state: "building", stuck: { at: "2026-11-03T00:00:00Z", step: "building" } })] };
    panel("a1");
    expect(screen.getByText("Stopped at building. Nothing has moved for 5 minutes.")).toBeTruthy();
    expect(screen.queryByText(/making the preview|Building the new version/)).toBeNull();
  });

  it("a stopped app offers the ask button to someone who can manage it, and not to others", () => {
    const stuck = { state: "building", stuck: { at: "2026-11-03T00:00:00Z", step: "building" } };
    hook = { ...base, apps: [row({ ...stuck, can_publish: true })] };
    const { unmount } = panel("a1");
    expect(screen.getByRole("button", { name: "Ask the agent to fix it" })).toBeTruthy();
    unmount();
    hook = { ...base, apps: [row({ ...stuck, can_publish: false })] };
    panel("a1");
    expect(screen.queryByRole("button", { name: "Ask the agent to fix it" })).toBeNull();
  });

  it("a building app that is not stopped offers no ask button", () => {
    hook = { ...base, apps: [row({ state: "building", can_publish: true })] };
    panel("a1");
    expect(screen.queryByRole("button", { name: "Ask the agent to fix it" })).toBeNull();
  });

  it("a building app is named, not opened", () => {
    hook = { ...base, apps: [row({ state: "building" })] };
    panel("a1");
    expect(screen.getByText("Invoices")).toBeTruthy();
    expect(screen.getByText(/(making the preview|Building the new version)\. It'll (post|show here and) in #finance/)).toBeTruthy();
    expect(screen.queryByText("app view")).toBeNull();
    expect(screen.getByRole("button", { name: "All apps" })).toBeTruthy();
    // The builder talks while it builds: its conversation is beside the status.
    expect(screen.getByRole("complementary", { name: "Conversation" }).textContent).toBe("a1");
  });

  it("an app the viewer can't open shows no conversation", () => {
    hook = { ...base, apps: [row({ state: "building", can_open: false })] };
    panel("a1");
    expect(screen.queryByRole("complementary", { name: "Conversation" })).toBeNull();
  });

  it.each([
    ["awaiting_approval", /Waiting for an owner to approve: the cards are in Activity, under Needs you/],
    ["provisioning", /Setting up its hosting and data/],
    ["failed", /Something went wrong\. Ask the agent in #finance/],
  ] as const)("%s shows its status line in the open view", (state, re) => {
    hook = { ...base, apps: [row({ state, live_version: null })] };
    panel("a1");
    expect(screen.getByText(re)).toBeTruthy();
    expect(screen.queryByText("app view")).toBeNull();
  });

  it("a failed app that still has a preview gets the full view, so Publish can be retried", () => {
    hook = { ...base, apps: [row({ state: "failed", live_version: null, preview_url: "https://p" })] };
    panel("a1");
    expect(screen.getByText("app view")).toBeTruthy();
  });

  it("a ready app renders the view", () => {
    hook = { ...base, apps: [row()] };
    panel("a1");
    expect(screen.getByText("app view")).toBeTruthy();
  });
});
