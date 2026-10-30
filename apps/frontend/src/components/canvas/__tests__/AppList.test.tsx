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
    expect(screen.getByText("people you invited")).toBeTruthy();
    expect(screen.getByText("anyone with the link")).toBeTruthy();
    expect(screen.getAllByText("$3.20 this month").length).toBe(4);
    fireEvent.click(screen.getByRole("button", { name: /Invoices/ }));
    expect(props.onOpen).toHaveBeenCalledWith("a1");
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

  it("building apps are listed but not openable", () => {
    const onOpen = vi.fn();
    render(<AppList {...props} onOpen={onOpen} apps={[app({ state: "building" })]} />);
    expect(screen.getByText("building")).toBeTruthy();
    fireEvent.click(screen.getByText("Invoices"));
    expect(onOpen).not.toHaveBeenCalled();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("empty state copy", () => {
    render(<AppList {...props} apps={[]} />);
    expect(screen.getByText("No apps yet. Ask an agent in a room to build one.")).toBeTruthy();
  });

  it("retry calls refresh", () => {
    const onRetry = vi.fn();
    render(<AppList {...props} onRetry={onRetry} error={new Error("x")} apps={[]} />);
    expect(screen.getByRole("alert")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalled();
  });
});
