import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { AppRow } from "@/hooks/useApps";

const apps = vi.hoisted(() => ({ list: [] as AppRow[] }));
vi.mock("@/hooks/useApps", () => ({ useApps: () => ({ apps: apps.list }) }));
const openPanel = vi.fn();
vi.mock("@/components/workspace/workspaceController", () => ({ openPanel: (...a: unknown[]) => openPanel(...a) }));

import { RequestApp } from "../RequestApp";

const app = (over: Partial<AppRow> = {}): AppRow => ({
  app_id: "a1", name: "workshop-signup", room_id: "r1", state: "ready", open_to: "room", budget_usd: 0,
  preview_url: "https://p", live_url: null, live_version: null, last_opened_at: null, spend_month_cents: null,
  can_open: true, request: { task_id: "t1", identifier: "LB-7" }, ...over,
});

describe("RequestApp", () => {
  it("shows the app this request made, where it stands, and opens it in Canvas", () => {
    apps.list = [app(), app({ app_id: "a2", name: "other", request: { task_id: "t2" } })];
    render(<RequestApp taskId="t1" />);
    expect(screen.getByText("Workshop Signup")).toBeInTheDocument();
    expect(screen.getByText(/Ready to try/)).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull(); // a preview opens through a pass, in Canvas
    fireEvent.click(screen.getByRole("button", { name: "Open in Canvas" }));
    expect(openPanel).toHaveBeenCalledWith("canvas", { app: "a1" }, { pushUrl: true });
  });

  it("a public live app gets its address; a request that made no app shows nothing", () => {
    apps.list = [app({ state: "live", live_version: 1, open_to: "public", live_url: "https://live" })];
    const { container, rerender } = render(<RequestApp taskId="t1" />);
    expect(screen.getByRole("link", { name: "Open the app" })).toHaveAttribute("href", "https://live");
    rerender(<RequestApp taskId="t9" />);
    expect(container).toBeEmptyDOMElement();
  });
});
