import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AppRow } from "@/hooks/useApps";

const open = vi.fn();
const refresh = vi.fn();
let admin = false;
vi.mock("@/hooks/useApps", () => ({ useApps: () => ({ open, refresh }) }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => admin }));
import { AppView } from "../AppView";

const app = (over: Partial<AppRow> = {}): AppRow => ({
  app_id: "a1", name: "Invoices", room_id: "r1", room_name: "finance", state: "ready", open_to: "room",
  budget_usd: 20, preview_url: null, live_url: "https://x", live_version: 3, last_opened_at: null,
  spend_month_cents: 0, can_open: true, ...over,
});

beforeEach(() => { open.mockReset(); refresh.mockReset(); admin = false; });

describe("AppView", () => {
  it("requests a pass and sets it as the frame src", async () => {
    open.mockResolvedValue("https://x.apps.test/?pass=1");
    render(<AppView app={app()} onBack={vi.fn()} />);
    expect(screen.getByText("Opening…")).toBeTruthy();
    const frame = await screen.findByTitle("Invoices");
    expect(frame.getAttribute("src")).toBe("https://x.apps.test/?pass=1");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts allow-same-origin allow-forms allow-popups");
    expect(open).toHaveBeenCalledWith("a1");
    expect(screen.getByText("v3")).toBeTruthy();
  });

  it("shows refusal and greys the row when open is forbidden", async () => {
    open.mockRejectedValue(new Error("Ask in #finance for access"));
    render(<AppView app={app()} onBack={vi.fn()} />);
    expect((await screen.findByRole("alert")).textContent).toContain("Ask in #finance for access");
    expect(document.querySelector("iframe")).toBeNull();
    expect(refresh).toHaveBeenCalled();
  });

  it("remounts the frame per app", async () => {
    open.mockImplementation(async (id: string) => `https://${id}.apps.test/`);
    const { rerender } = render(<AppView app={app()} onBack={vi.fn()} />);
    const first = await screen.findByTitle("Invoices");
    rerender(<AppView app={app({ app_id: "a2", name: "Pipeline" })} onBack={vi.fn()} />);
    const second = await screen.findByTitle("Pipeline");
    expect(second).not.toBe(first);
    expect(first.isConnected).toBe(false);
    expect(second.getAttribute("src")).toBe("https://a2.apps.test/");
  });

  it("admins see the Settings button, members do not", async () => {
    open.mockResolvedValue("https://x/");
    const { unmount } = render(<AppView app={app()} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    expect(screen.queryByRole("button", { name: "Settings" })).toBeNull();
    unmount();
    admin = true;
    render(<AppView app={app()} onBack={vi.fn()} />);
    expect(await screen.findByRole("button", { name: "Settings" })).toBeTruthy();
  });

  it("open in own tab requests a fresh pass", async () => {
    open.mockResolvedValueOnce("https://x/one").mockResolvedValueOnce("https://x/two");
    const win = vi.spyOn(window, "open").mockReturnValue(null);
    render(<AppView app={app()} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    fireEvent.click(screen.getByRole("button", { name: "Open in its own tab" }));
    await waitFor(() => expect(win).toHaveBeenCalledWith("https://x/two", "_blank", "noopener"));
    expect(open).toHaveBeenCalledTimes(2);
  });

  it("the second app's frame never carries the first app's url", async () => {
    let release!: (u: string) => void;
    open.mockResolvedValueOnce("https://a1.apps.test/").mockImplementationOnce(() => new Promise<string>((r) => { release = r; }));
    const { rerender } = render(<AppView app={app()} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    rerender(<AppView app={app({ app_id: "a2", name: "Pipeline" })} onBack={vi.fn()} />);
    expect(document.querySelector("iframe")).toBeNull();
    expect(screen.getByText("Opening…")).toBeTruthy();
    release("https://a2.apps.test/");
    expect((await screen.findByTitle("Pipeline")).getAttribute("src")).toBe("https://a2.apps.test/");
  });

  it("ignores a stale open() that resolves after an app switch", async () => {
    let late!: (u: string) => void;
    open.mockImplementationOnce(() => new Promise<string>((r) => { late = r; })).mockResolvedValueOnce("https://a2.apps.test/");
    const { rerender } = render(<AppView app={app()} onBack={vi.fn()} />);
    rerender(<AppView app={app({ app_id: "a2", name: "Pipeline" })} onBack={vi.fn()} />);
    await screen.findByTitle("Pipeline");
    late("https://a1.apps.test/");
    await new Promise((r) => setTimeout(r, 0));
    expect(document.querySelectorAll("iframe")).toHaveLength(1);
    expect(screen.getByTitle("Pipeline").getAttribute("src")).toBe("https://a2.apps.test/");
  });

  it("open-in-tab failure keeps the frame, shows an alert and refreshes", async () => {
    open.mockResolvedValueOnce("https://x/one").mockRejectedValueOnce(new Error("No longer allowed"));
    render(<AppView app={app()} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    fireEvent.click(screen.getByRole("button", { name: "Open in its own tab" }));
    expect((await screen.findByRole("alert")).textContent).toContain("No longer allowed");
    expect(screen.getByTitle("Invoices")).toBeTruthy();
    expect(refresh).toHaveBeenCalled();
  });
});
