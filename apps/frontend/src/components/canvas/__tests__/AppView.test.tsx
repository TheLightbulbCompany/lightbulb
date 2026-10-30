import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AppRow } from "@/hooks/useApps";

const open = vi.fn();
const refresh = vi.fn();
const publish = vi.fn();
let admin = false;
vi.mock("@/hooks/useApps", () => ({ useApps: () => ({ open, refresh, publish }) }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => admin }));
import { AppView } from "../AppView";

const app = (over: Partial<AppRow> = {}): AppRow => ({
  app_id: "a1", name: "Invoices", room_id: "r1", room_name: "finance", state: "ready", open_to: "room",
  budget_usd: 20, preview_url: null, live_url: "https://x", live_version: 3, last_opened_at: null,
  spend_month_cents: 0, can_open: true, ...over,
});

beforeEach(() => { vi.restoreAllMocks(); open.mockReset(); refresh.mockReset(); publish.mockReset(); admin = false; });

describe("AppView", () => {
  it("requests a pass and sets it as the frame src", async () => {
    open.mockResolvedValue("https://x.apps.test/?pass=1");
    render(<AppView app={app()} onBack={vi.fn()} />);
    expect(screen.getByText("Opening…")).toBeTruthy();
    const frame = await screen.findByTitle("Invoices");
    expect(frame.getAttribute("src")).toBe("https://x.apps.test/?pass=1");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts allow-same-origin allow-forms allow-popups");
    expect(open).toHaveBeenCalledWith("a1", "live");
    expect(screen.queryByText("v3")).toBeNull();
  });

  it("a 403 says who can open it and where to ask; raw text only under Details", async () => {
    open.mockRejectedValue(Object.assign(new Error("Forbidden"), { status: 403 }));
    render(<AppView app={app({ name: "workshop-signup" })} onBack={vi.fn()} />);
    expect((await screen.findByRole("alert")).textContent).toContain("Only #finance can open Workshop Signup. Ask in #finance.");
    expect(screen.getByText("Details").closest("details")!.textContent).toContain("Forbidden");
    expect(screen.getByRole("heading", { name: "Workshop Signup" })).toBeTruthy();
    expect(screen.queryByText("workshop-signup")).toBeNull();
  });

  it("any other open failure offers Try again, which opens again", async () => {
    open.mockRejectedValueOnce(new Error("boom")).mockResolvedValueOnce("https://x/ok");
    render(<AppView app={app()} onBack={vi.fn()} />);
    expect((await screen.findByRole("alert")).textContent).toContain("Invoices didn't open. Try again in a moment.");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect((await screen.findByTitle("Invoices")).getAttribute("src")).toBe("https://x/ok");
    expect(open).toHaveBeenCalledTimes(2);
  });

  it("shows refusal and greys the row when open is forbidden", async () => {
    open.mockRejectedValue(new Error("Ask in #finance for access"));
    render(<AppView app={app()} onBack={vi.fn()} />);
    expect((await screen.findByRole("alert")).textContent).toContain("Invoices didn't open");
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

  it("the creator (can_publish) sees Settings; a member gets the muted line", async () => {
    open.mockResolvedValue("https://x/");
    const { unmount } = render(<AppView app={app({ can_publish: true })} onBack={vi.fn()} />);
    expect(await screen.findByRole("button", { name: "Settings" })).toBeTruthy();
    unmount();
    render(<AppView app={app({ created_by_name: "Dana" })} onBack={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Settings" })).toBeNull();
    expect(screen.getByText("Open to #finance. Only Dana or a workspace admin can change who can open it.")).toBeTruthy();
  });

  it("the muted line falls back when the creator is unknown", () => {
    open.mockResolvedValue("https://x/");
    render(<AppView app={app({ open_to: "workspace" })} onBack={vi.fn()} />);
    expect(screen.getByText(/Open to people in this workspace\. Only the person who asked for it or a workspace admin/)).toBeTruthy();
  });

  it("admins see the Settings button", async () => {
    open.mockResolvedValue("https://x/");
    const { unmount } = render(<AppView app={app()} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    unmount();
    admin = true;
    render(<AppView app={app()} onBack={vi.fn()} />);
    expect(await screen.findByRole("button", { name: "Settings" })).toBeTruthy();
  });

  it("open in own tab requests a fresh pass", async () => {
    open.mockResolvedValueOnce("https://x/one").mockResolvedValueOnce("https://x/two");
    const tab = { location: { href: "" }, close: vi.fn(), opener: {} };
    const win = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    render(<AppView app={app()} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    fireEvent.click(screen.getByRole("button", { name: "Open in its own tab" }));
    expect(win).toHaveBeenCalledWith("", "_blank"); // opened synchronously, before the pass is minted
    await waitFor(() => expect(tab.location.href).toBe("https://x/two"));
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

  it("before the first publish: no switch, just 'Preview: not live yet.'", async () => {
    open.mockResolvedValue("https://x/");
    render(<AppView app={app({ live_version: null })} onBack={vi.fn()} />);
    expect(screen.getByText("Preview: not live yet.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Preview" })).toBeNull();
  });

  it("Preview | Live is a segmented control marking the current view, with the preview line", async () => {
    open.mockImplementation(async (id: string, t = "live") => `https://${id}.${t}.test/`);
    render(<AppView app={app({ preview_url: "https://p" })} onBack={vi.fn()} />);
    expect((await screen.findByTitle("Invoices")).getAttribute("src")).toBe("https://a1.live.test/");
    const pressed = (n: string) => screen.getByRole("button", { name: n }).getAttribute("aria-pressed");
    expect([pressed("Preview"), pressed("Live")]).toEqual(["false", "true"]);
    expect(screen.queryByText(/You're looking at the preview/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(screen.getByTitle("Invoices").getAttribute("src")).toBe("https://a1.preview.test/"));
    expect([pressed("Preview"), pressed("Live")]).toEqual(["true", "false"]);
    expect(screen.getByText("You're looking at the preview. Only you and #finance see it until it's published.")).toBeTruthy();
    expect(open).toHaveBeenLastCalledWith("a1", "preview");
    fireEvent.click(screen.getByRole("button", { name: "Live" }));
    await waitFor(() => expect(screen.getByTitle("Invoices").getAttribute("src")).toBe("https://a1.live.test/"));
  });

  it("a public app's muted line and 403 say 'anyone with the link', not the workspace", async () => {
    open.mockRejectedValue(Object.assign(new Error("Forbidden"), { status: 403 }));
    render(<AppView app={app({ open_to: "public" })} onBack={vi.fn()} />);
    expect(screen.getByText(/^Open to anyone with the link\./)).toBeTruthy();
    expect((await screen.findByRole("alert")).textContent).toContain("Only anyone with the link can open");
  });

  it("a pending app shows its status line in the view", () => {
    open.mockResolvedValue("https://x/");
    render(<AppView app={app({ state: "provisioning", live_version: null })} onBack={vi.fn()} />);
    expect(screen.getByText("Setting up its hosting and data, usually a few minutes.")).toBeTruthy();
    expect(screen.queryByText("Preview: not live yet.")).toBeNull();
  });

  it("falls back to live when the preview goes away", async () => {
    open.mockImplementation(async (id: string, t = "live") => `https://${id}.${t}.test/`);
    const { rerender } = render(<AppView app={app({ preview_url: "https://p" })} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(screen.getByTitle("Invoices").getAttribute("src")).toBe("https://a1.preview.test/"));
    rerender(<AppView app={app({ preview_url: null })} onBack={vi.fn()} />);
    await waitFor(() => expect(screen.getByTitle("Invoices").getAttribute("src")).toBe("https://a1.live.test/"));
  });

  it("open-in-tab failure keeps the frame, shows an alert and refreshes", async () => {
    open.mockResolvedValueOnce("https://x/one").mockRejectedValueOnce(new Error("No longer allowed"));
    const tab = { location: { href: "" }, close: vi.fn(), opener: {} };
    vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    render(<AppView app={app()} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    fireEvent.click(screen.getByRole("button", { name: "Open in its own tab" }));
    expect((await screen.findByRole("alert")).textContent).toContain("No longer allowed");
    expect(screen.getByTitle("Invoices")).toBeTruthy();
    expect(tab.close).toHaveBeenCalled();
    expect(refresh).toHaveBeenCalled();
  });

  describe("publish", () => {
    const ready = (over: Partial<AppRow> = {}) => app({ can_publish: true, preview_url: "https://p", live_version: 3, ...over });
    const clickPublish = async () => {
      fireEvent.click(await screen.findByRole("button", { name: "Publish" }));
      fireEvent.click(screen.getAllByRole("button", { name: "Publish" })[0]); // the confirm
    };
    beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); open.mockResolvedValue("https://x/"); });
    afterEach(() => vi.useRealTimers());

    it("members without can_publish don't see Publish", async () => {
      render(<AppView app={ready({ can_publish: false })} onBack={vi.fn()} />);
      await screen.findByTitle("Invoices");
      expect(screen.queryByRole("button", { name: "Publish" })).toBeNull();
    });

    it("Publish asks first, naming the audience; Cancel posts nothing", async () => {
      render(<AppView app={ready({ open_to: "workspace" })} onBack={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Publish" }));
      expect(screen.getByText("Make this preview live for everyone in the workspace? You can undo it from the room.")).toBeTruthy();
      expect(publish).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      expect(screen.getByRole("button", { name: "Publish" })).toBeTruthy();
      expect(publish).not.toHaveBeenCalled();
    });

    it.each([
      ["room", "Live — people in #finance can open it"],
      ["workspace", "Live — everyone in the workspace"],
      ["invited", "Live — people you invited"],
    ] as const)("Live line for %s names who can open it and shows no address", async (open_to, line) => {
      publish.mockResolvedValue({ app_id: "a1", state: "building" });
      const { rerender } = render(<AppView app={ready({ open_to })} onBack={vi.fn()} />);
      await clickPublish();
      await screen.findByText("Publishing…");
      rerender(<AppView app={ready({ open_to, live_version: 4, live_url: "https://inv.apps.test" })} onBack={vi.fn()} />);
      expect(await screen.findByText(line)).toBeTruthy();
      expect(screen.queryByText("https://inv.apps.test")).toBeNull();
      expect(screen.queryByRole("button", { name: "Copy link" })).toBeNull();
    });

    it("publish shows progress then the live address", async () => {
      publish.mockResolvedValue({ app_id: "a1", state: "building" });
      const { rerender } = render(<AppView app={ready()} onBack={vi.fn()} />);
      await clickPublish();
      expect(await screen.findByText("Publishing…")).toBeTruthy();
      expect(publish).toHaveBeenCalledWith("a1");
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(refresh).toHaveBeenCalledTimes(1);
      expect(screen.getByText("Publishing…")).toBeTruthy(); // first read: still version 3
      rerender(<AppView app={ready({ open_to: "public", live_version: 4, live_url: "https://inv.apps.test", state: "building" })} onBack={vi.fn()} />);
      const link = await screen.findByRole("link", { name: "https://inv.apps.test" });
      expect(screen.getByRole("button", { name: "Copy link" })).toBeTruthy();
      expect(screen.getByRole("link", { name: "Visit" })).toBeTruthy();
      expect(link.getAttribute("target")).toBe("_blank");
      expect(link.getAttribute("rel")).toContain("noopener");
      expect(screen.getByText(/Live — anyone with the link/)).toBeTruthy();
      await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
      expect(refresh).toHaveBeenCalledTimes(1); // polling stopped
    });

    it("publish failure: plain words, Try again, raw text under Details", async () => {
      publish.mockRejectedValueOnce(new Error("The build failed: missing env")).mockResolvedValueOnce({ app_id: "a1", state: "building" });
      render(<AppView app={ready()} onBack={vi.fn()} />);
      await clickPublish();
      expect((await screen.findAllByRole("alert")).map((a) => a.textContent).join()).toContain("This version didn't go live. People still see the last live version. Try again, or ask the agent in #finance to fix it.");
      expect(screen.getByText("Details").closest("details")!.textContent).toContain("The build failed: missing env");
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      expect(await screen.findByText("Publishing…")).toBeTruthy();
      expect(publish).toHaveBeenCalledTimes(2);
    });

    it("a failed state while publishing shows the row's error", async () => {
      publish.mockResolvedValue({ app_id: "a1", state: "building" });
      const { rerender } = render(<AppView app={ready()} onBack={vi.fn()} />);
      await clickPublish();
      await screen.findByText("Publishing…");
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); }); // a read after the click makes "failed" trustworthy
      rerender(<AppView app={ready({ state: "failed", error: { message: "Deploy rejected" } })} onBack={vi.fn()} />);
      expect((await screen.findAllByRole("alert")).map((a) => a.textContent).join()).toContain("Deploy rejected");
      expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    });

    it("polling stops on unmount", async () => {
      publish.mockResolvedValue({ app_id: "a1", state: "building" });
      const { unmount } = render(<AppView app={ready()} onBack={vi.fn()} />);
      await clickPublish();
      await screen.findByText("Publishing…");
      unmount();
      await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
      expect(refresh).not.toHaveBeenCalled();
    });

    it("gives up after 10 minutes with a Refresh", async () => {
      publish.mockResolvedValue({ app_id: "a1", state: "building" });
      render(<AppView app={ready()} onBack={vi.fn()} />);
      await clickPublish();
      await screen.findByText("Publishing…");
      await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60 * 1000 + 1000); });
      expect(screen.getByText("Still publishing — check back in a minute")).toBeTruthy();
      const n = refresh.mock.calls.length;
      fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
      expect(refresh).toHaveBeenCalledTimes(n + 1);
    });

    it("retry after a failed row waits for a fresh read, then shows the live address", async () => {
      publish.mockResolvedValue({ app_id: "a1", state: "building" });
      const failedRow = ready({ state: "failed", error: { message: "Deploy rejected" } });
      const { rerender } = render(<AppView app={failedRow} onBack={vi.fn()} />);
      // failed rows are not publishable; start from a ready row, fail, then retry
      rerender(<AppView app={ready()} onBack={vi.fn()} />);
      await clickPublish();
      await screen.findByText("Publishing…");
      rerender(<AppView app={failedRow} onBack={vi.fn()} />);
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
      expect(await screen.findByText("Publishing…")).toBeTruthy(); // stale failed row is not trusted
      expect(publish).toHaveBeenCalledTimes(2);
      refresh.mockClear();
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(refresh).toHaveBeenCalledTimes(1);
      rerender(<AppView app={ready({ live_version: 4 })} onBack={vi.fn()} />);
      expect(await screen.findByText(/^Live — /)).toBeTruthy();
    });

    it("a first publish is done when live_url appears", async () => {
      publish.mockResolvedValue({ app_id: "a1", state: "building" });
      const first = (o: Partial<AppRow> = {}) => ready({ live_version: null, live_url: null, ...o });
      const { rerender } = render(<AppView app={first()} onBack={vi.fn()} />);
      await clickPublish();
      await screen.findByText("Publishing…");
      rerender(<AppView app={first({ open_to: "public", live_url: "https://inv.apps.test" })} onBack={vi.fn()} />);
      expect(await screen.findByRole("link", { name: "https://inv.apps.test" })).toBeTruthy();
    });

    it("header and settings share one publish: one POST, both show progress", async () => {
      admin = true;
      publish.mockResolvedValue({ app_id: "a1", state: "building" });
      render(<AppView app={ready()} onBack={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Settings" }));
      expect(screen.getAllByRole("button", { name: "Publish" })).toHaveLength(2);
      fireEvent.click(screen.getAllByRole("button", { name: "Publish" })[0]);
      fireEvent.click(screen.getAllByRole("button", { name: "Publish" })[0]); // the confirm
      await waitFor(() => expect(screen.getAllByText("Publishing…")).toHaveLength(2));
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(publish).toHaveBeenCalledTimes(1);
      expect(refresh).toHaveBeenCalledTimes(1); // one interval
    });

    it("offers Publish again after going live when a newer preview is ready", async () => {
      publish.mockResolvedValue({ app_id: "a1", state: "building" });
      const { rerender } = render(<AppView app={ready()} onBack={vi.fn()} />);
      await clickPublish();
      await screen.findByText("Publishing…");
      rerender(<AppView app={ready({ live_version: 4 })} onBack={vi.fn()} />);
      await screen.findByText(/^Live — /);
      fireEvent.click(screen.getByRole("button", { name: "Publish" }));
      fireEvent.click(screen.getAllByRole("button", { name: "Publish" })[0]);
      expect(publish).toHaveBeenCalledTimes(2);
    });
  });
});
