import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { AppList } from "../AppList";
import type { AppRow } from "@/hooks/useApps";

const openPanel = vi.fn();
vi.mock("@/components/workspace/workspaceController", () => ({ openPanel: (...a: unknown[]) => openPanel(...a) }));

const app = (over: Partial<AppRow> = {}): AppRow => ({
  app_id: "a1", name: "Invoices", room_id: "r1", room_name: "finance", state: "live", open_to: "room",
  budget_usd: 20, preview_url: null, live_url: "https://x", live_version: 1, last_opened_at: null,
  spend_month_cents: 320, created_by_name: "Dana", can_open: true, ...over,
});
const props = { loading: false, error: null, onOpen: vi.fn(), onRetry: vi.fn() };
const names = () => screen.getAllByRole("listitem").map((li) => li.querySelector(".app-item__name")!.textContent);

describe("AppList", () => {
  it("one row per app: name, state, who can open it, who asked, spend; a click opens it", () => {
    render(<AppList {...props} apps={[
      app(), app({ app_id: "a2", name: "Pipeline", open_to: "workspace" }),
      app({ app_id: "a3", name: "Share", open_to: "invited" }), app({ app_id: "a4", name: "Site", open_to: "public" }),
    ]} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(4);
    expect(screen.getByText("Open to people in #finance.")).toBeTruthy();
    expect(screen.getByText("Open to everyone here.")).toBeTruthy();
    expect(screen.getByText("Open to invited people.")).toBeTruthy();
    expect(screen.getByText("Open to anyone with the link.")).toBeTruthy();
    expect(screen.getAllByText("Asked by Dana in #finance · $3.20 this month")).toHaveLength(4);
    fireEvent.click(screen.getByRole("button", { name: /Invoices/ }));
    expect(props.onOpen).toHaveBeenCalledWith("a1");
  });

  it("apps that need the person come first, then failures, then work in progress, then the rest", () => {
    render(<AppList {...props} canManage apps={[
      app({ app_id: "l", name: "Live One" }),
      app({ app_id: "b", name: "Building One", state: "building", live_version: null }),
      app({ app_id: "s", name: "Stuck One", state: "building", stuck: { at: "x", step: "building" } }),
      app({ app_id: "n", name: "Needs One", state: "awaiting_approval", live_version: null }),
    ]} />);
    expect(names()).toEqual(["Needs One", "Stuck One", "Building One", "Live One"]);
  });

  it("an app waiting for approval offers Review to a manager, which opens Activity; a member is told who acts", () => {
    const { unmount } = render(<AppList {...props} canManage apps={[app({ state: "awaiting_approval", live_version: null })]} />);
    expect(screen.getByText("Needs approval")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Review" }));
    expect(openPanel).toHaveBeenCalledWith("activity", {}, { pushUrl: true });
    unmount();
    render(<AppList {...props} apps={[app({ state: "awaiting_approval", live_version: null })]} />);
    expect(screen.getByText("Waiting for an owner to approve its hosting and data.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
  });

  it("an app only a person can unblock says what and where, before any other state", () => {
    const msg = "Setting up services needs a services account — add one from the Stripe card in Settings → Connections.";
    const { unmount } = render(<AppList {...props} canManage apps={[app({ state: "awaiting_approval", live_version: null, setup_blocker: { message: msg }, stuck: { at: "x", step: "building" } })]} />);
    expect(screen.getByText("Needs setup")).toBeTruthy();
    expect(screen.getByText(msg)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open Connections" }).getAttribute("href")).toBe("/settings/connections");
    expect(screen.queryByText("Stopped")).toBeNull();
    unmount();
    render(<AppList {...props} apps={[app({ state: "awaiting_approval", live_version: null, setup_blocker: { message: msg } })]} />);
    expect(screen.queryByRole("link", { name: "Open Connections" })).toBeNull(); // only someone who can manage it
  });

  it("a stuck app says it stopped and where, not its usual status", () => {
    render(<AppList {...props} apps={[app({ state: "building", stuck: { at: "2026-11-03T00:00:00Z", step: "building" } })]} />);
    expect(screen.getByText("Stopped")).toBeTruthy();
    expect(screen.getByText("Nothing has moved for 5 minutes at building. Open it to ask the agent.")).toBeTruthy();
    expect(screen.queryByText("Updating")).toBeNull();
  });

  it("an app waiting for approval reads Needs approval even when nothing has moved for a while", () => {
    render(<AppList {...props} canManage apps={[app({ state: "awaiting_approval", live_version: null, stuck: { at: "x", step: "building" } })]} />);
    expect(screen.getByText("Needs approval")).toBeTruthy();
    expect(screen.queryByText("Stopped")).toBeNull();
  });

  it("tiles take the first and last word, numbers skipped", () => {
    render(<AppList {...props} apps={[app({ name: "run-12-jobs" }), app({ app_id: "a2", name: "run-1-harbor" }), app({ app_id: "a3", name: "crm" })]} />);
    expect([...document.querySelectorAll(".app-tile")].map((t) => t.textContent)).toEqual(["RJ", "RH", "C"]);
  });

  it("a building app's tile shows the running ring; a live one's does not", () => {
    render(<AppList {...props} apps={[app({ state: "building", live_version: null }), app({ app_id: "a2", name: "Done" })]} />);
    expect([...document.querySelectorAll(".app-tile")].map((t) => t.hasAttribute("data-running"))).toEqual([true, false]);
  });

  it.each([
    [{ state: "building", live_version: null }, "Building", "The agent is making the first version."],
    [{ state: "building" }, "Updating", "The agent is building a new version. The live app keeps working."],
    [{ state: "building", live_version: null, publishing: true }, "Publishing", "Publishing. It'll be live in a minute or two."],
    [{ state: "provisioning", live_version: null }, "Setting up", "Setting up hosting and data, usually a few minutes."],
    [{ state: "ready", live_version: null }, "Ready to try", "Try the preview, then publish it."],
    [{ state: "ready", live_version: null, agent_working: true }, "First version", "Try it now; the agent is still improving it."],
    [{ state: "failed", live_version: null }, "Failed", "The last build didn't work. Open it to ask the agent."],
    [{ preview_newer: true }, "Live", "A newer version is ready to publish."],
    [{ publish_failed: true }, "Live", "The last update didn't go live. People still see the version before it."],
  ] as const)("%o reads %s: %s", (over, label, next) => {
    render(<AppList {...props} apps={[app(over as Partial<AppRow>)]} />);
    const row = screen.getByRole("listitem");
    expect(within(row).getByText(label)).toBeTruthy();
    expect(within(row).getByText(next)).toBeTruthy();
  });

  it("filters: In progress and Live, with counts", () => {
    render(<AppList {...props} apps={[app(), app({ app_id: "a2", name: "Draft", state: "ready", live_version: null })]} />);
    const filters = screen.getByRole("group", { name: "Show" });
    expect(filters.textContent).toBe("All2In progress1Live1");
    fireEvent.click(within(filters).getByRole("button", { name: /In progress/ }));
    expect(names()).toEqual(["Draft"]);
    fireEvent.click(within(filters).getByRole("button", { name: /Live/ }));
    expect(names()).toEqual(["Invoices"]);
  });

  it("greys an app the viewer cannot open and says where to ask", () => {
    const onOpen = vi.fn();
    render(<AppList {...props} onOpen={onOpen} apps={[app({ can_open: false })]} />);
    expect(screen.getByText("Ask in #finance to open it.")).toBeTruthy();
    const row = screen.getByRole("group", { name: "Invoices" });
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
    expect(screen.queryByRole("button", { name: /Invoices/ })).toBeNull();
  });

  it("an app from a DM or the CLI names no room", () => {
    render(<AppList {...props} apps={[app({ room_id: null, room_name: undefined, open_to: "me", spend_month_cents: null })]} />);
    expect(screen.getByText("Asked by Dana")).toBeTruthy();
    expect(screen.getByText("Open to only its creator and admins.")).toBeTruthy();
    expect(screen.queryByText(/#room/)).toBeNull();
  });

  it("shows a title-cased display name, initials on the tile, and hides zero or unknown spend", () => {
    render(<AppList {...props} apps={[app({ name: "workshop-signup", spend_month_cents: 0 }), app({ app_id: "a2", name: "x-y", display_name: "Fancy Name", spend_month_cents: null })]} />);
    expect(screen.getByText("Workshop Signup")).toBeTruthy();
    expect(screen.getByText("Fancy Name")).toBeTruthy();
    expect([...document.querySelectorAll(".app-tile")].map((t) => t.textContent)).toEqual(["WS", "FN"]);
    expect(screen.queryByText(/this month|NaN|\$0/)).toBeNull();
  });

  it("a row with no known asker says where it came from, never 'someone'", () => {
    render(<AppList {...props} apps={[app({ created_by_name: undefined, requested_by: undefined, spend_month_cents: null })]} />);
    expect(screen.getByText("From #finance")).toBeTruthy();
    expect(screen.queryByText(/someone/)).toBeNull();
  });

  it("says when it last changed", () => {
    render(<AppList {...props} apps={[app({ updated_at: new Date(Date.now() - 2 * 3600_000).toISOString(), spend_month_cents: null })]} />);
    expect(screen.getByText("Asked by Dana in #finance · 2 hours ago")).toBeTruthy();
  });

  it("loading and empty copy", () => {
    const { unmount } = render(<AppList {...props} loading apps={[]} />);
    expect(screen.getByText("Loading apps…")).toBeTruthy();
    unmount();
    render(<AppList {...props} apps={[]} />);
    expect(screen.getByText("No apps yet")).toBeTruthy();
    expect(screen.queryByRole("group", { name: "Show" })).toBeNull();
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
