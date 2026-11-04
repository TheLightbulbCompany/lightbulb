import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { AppList } from "../AppList";
import type { AppRow } from "@/hooks/useApps";

const app = (over: Partial<AppRow> = {}): AppRow => ({
  app_id: "a1", name: "Invoices", room_id: "r1", room_name: "finance", state: "ready", open_to: "room",
  budget_usd: 20, preview_url: null, live_url: "https://x", live_version: 1, last_opened_at: null,
  spend_month_cents: 320, created_by_name: "Dana", can_open: true, ...over,
});
const props = { loading: false, error: null, onOpen: vi.fn(), onRetry: vi.fn() };

describe("AppList", () => {
  it("renders one row per app with who can open it", () => {
    render(<AppList {...props} apps={[
      app(), app({ app_id: "a2", name: "Pipeline", open_to: "workspace" }),
      app({ app_id: "a3", name: "Share", open_to: "invited" }), app({ app_id: "a4", name: "Site", open_to: "public" }),
    ]} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
    expect(screen.getAllByText(/asked by Dana in #finance/)).toHaveLength(4);
    expect(screen.getAllByText("#finance").length).toBeGreaterThan(0);
    expect(screen.getByText("everyone here")).toBeTruthy();
    expect(screen.getByText("invited people")).toBeTruthy();
    expect(screen.getByText("anyone with the link")).toBeTruthy();
    expect(screen.getAllByText("$3.20 spent this month").length).toBe(4);
    fireEvent.click(screen.getByRole("button", { name: /Invoices/ }));
    expect(props.onOpen).toHaveBeenCalledWith("a1");
  });

  it("a stuck app's row says where it stopped, not its usual status", () => {
    render(<AppList {...props} apps={[app({ state: "building", stuck: { at: "2026-11-03T00:00:00Z", step: "building" } })]} />);
    expect(screen.getByText("Stopped at building. Nothing has moved for 5 minutes.")).toBeTruthy();
    expect(screen.queryByText(/Building the new version/)).toBeNull();
  });

  it("greys an app the viewer cannot open and names its room", () => {
    const onOpen = vi.fn();
    render(<AppList {...props} onOpen={onOpen} apps={[app({ can_open: false })]} />);
    expect(screen.getByText("ask in #finance")).toBeTruthy();
    const row = screen.getByText("Invoices").closest("[aria-disabled]")!;
    expect(row.getAttribute("aria-disabled")).toBe("true");
    fireEvent.click(row);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("an admin can open a row the viewer cannot; a member cannot", () => {
    const onOpen = vi.fn();
    const { unmount } = render(<AppList {...props} canManage onOpen={onOpen} apps={[app({ can_open: false })]} />);
    fireEvent.click(screen.getByRole("button", { name: /Invoices/ }));
    expect(onOpen).toHaveBeenCalledWith("a1");
    unmount();
    render(<AppList {...props} onOpen={onOpen} apps={[app({ can_open: false })]} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByRole("group").getAttribute("aria-disabled")).toBe("true");
  });

  it("building apps are listed but not openable", () => {
    const onOpen = vi.fn();
    render(<AppList {...props} onOpen={onOpen} apps={[app({ state: "building", live_version: null }), app({ app_id: "a2", name: "Pipeline", state: "building" })]} />);
    expect(screen.getByText("The agent is making the preview. It'll post in #finance when it's ready.")).toBeTruthy(); // first build
    expect(screen.getByText("Building the new version. It'll show here and in #finance when it's ready.")).toBeTruthy(); // a publish or a change
    fireEvent.click(screen.getByText("Invoices"));
    expect(onOpen).not.toHaveBeenCalled();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("shows a title-cased display name, not the slug, and hides zero spend", () => {
    render(<AppList {...props} apps={[app({ name: "workshop-signup", spend_month_cents: 0 }), app({ app_id: "a2", name: "x-y", display_name: "Fancy Name", spend_month_cents: 0 })]} />);
    expect(screen.getByText("Workshop Signup")).toBeTruthy();
    expect(screen.getByText("Fancy Name")).toBeTruthy();
    expect(screen.queryByText("workshop-signup")).toBeNull();
    expect(screen.queryByText(/spent this month/)).toBeNull();
  });

  it("hides the spend line when spend is null", () => {
    render(<AppList {...props} apps={[app({ spend_month_cents: null })]} />);
    expect(screen.queryByText(/spent this month/)).toBeNull();
    expect(screen.queryByText(/NaN|\$0/)).toBeNull();
  });

  it("awaiting row names who asked when requested_by is present", () => {
    render(<AppList {...props} apps={[app({ state: "awaiting_approval", requested_by: "Dana" })]} />);
    expect(screen.getByText("Waiting for an owner to approve Dana's request: the cards are in Activity, under Needs you.")).toBeTruthy();
  });

  it("a row with no known asker says where it came from, never 'someone'", () => {
    render(<AppList {...props} apps={[app({ created_by_name: undefined, requested_by: undefined })]} />);
    expect(screen.getByText("from #finance")).toBeTruthy();
    expect(screen.queryByText(/someone/)).toBeNull();
  });

  it.each([
    ["awaiting_approval", "Waiting for an owner to approve: the cards are in Activity, under Needs you."],
    ["provisioning", "Setting up its hosting and data, usually a few minutes."],
    ["failed", "Something went wrong. Ask the agent in #finance."],
  ] as const)("%s row shows its status line", (state, line) => {
    render(<AppList {...props} apps={[app({ state })]} />);
    expect(screen.getByText(line)).toBeTruthy();
  });

  it("loading and empty copy", () => {
    const { unmount } = render(<AppList {...props} loading apps={[]} />);
    expect(screen.getByText("Loading apps…")).toBeTruthy();
    unmount();
    render(<AppList {...props} apps={[]} />);
    expect(screen.getByText(/No apps yet\. In any room, ask an agent for one, like "a sign-up page for our workshop", and it appears here\./)).toBeTruthy();
  });

  it("a load failure says what happened, Try again retries, raw text is under Details", () => {
    const onRetry = vi.fn();
    render(<AppList {...props} onRetry={onRetry} error={new Error("503 upstream")} apps={[]} />);
    expect(screen.getByRole("alert").textContent).toContain("We couldn't load the apps.");
    expect(screen.getByText("Details").closest("details")!.textContent).toContain("503 upstream");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalled();
  });
});
