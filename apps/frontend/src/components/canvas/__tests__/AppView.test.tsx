import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { AppRow } from "@/hooks/useApps";

const open = vi.fn();
const refresh = vi.fn();
const publish = vi.fn();
const askAgent = vi.fn();
const rollback = vi.fn();
let admin = false;
const openConversation = vi.fn(async () => ({ roomId: "conv-1" }));
vi.mock("@/hooks/useApps", () => ({ useApps: () => ({ open, refresh, publish, askAgent, rollback, openConversation, listInvites: async () => [] }) }));
vi.mock("../AppConversation", () => ({ AppConversation: ({ app, shown }: { app: AppRow; shown?: boolean }) => <aside aria-label="Conversation" data-shown={String(shown)}>{app.app_id}</aside> }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => admin }));
import { AppView } from "../AppView";

const app = (over: Partial<AppRow> = {}): AppRow => ({
  app_id: "a1", name: "Invoices", room_id: "r1", room_name: "finance", state: "ready", open_to: "room",
  budget_usd: 20, preview_url: null, live_url: "https://x", live_version: 3, last_opened_at: null,
  spend_month_cents: 0, can_open: true, ...over,
});

beforeEach(() => { vi.restoreAllMocks(); open.mockReset(); refresh.mockReset(); publish.mockReset(); askAgent.mockReset(); rollback.mockReset(); openConversation.mockClear(); admin = false; });

describe("AppView", () => {
  it("shows the app's conversation beside the preview, with no Conversation | App switch when wide", async () => {
    open.mockResolvedValue("https://x.apps.test/?pass=1");
    const { container } = render(<AppView app={app()} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    const split = container.querySelector(".app-view__split")!;
    expect(split.querySelector(".app-view__main iframe")).not.toBeNull();
    expect(within(split as HTMLElement).getByRole("complementary", { name: "Conversation" }).textContent).toBe("a1");
    const view = container.querySelector(".app-view")!;
    expect(view.getAttribute("data-pane")).toBe("preview");
    expect(view.hasAttribute("data-narrow")).toBe(false); // jsdom has no width: wide, both shown
    expect(screen.queryByRole("button", { name: "Conversation" })).toBeNull(); // the switch is for narrow panels only
    expect(screen.getByRole("complementary", { name: "Conversation" }).getAttribute("data-shown")).toBe("true");
  });

  it("in a narrow panel the conversation counts as on screen only on its pane, and Settings shows the app pane", async () => {
    const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(500);
    open.mockResolvedValue("https://x.apps.test/?pass=1");
    const { container } = render(<AppView app={app({ can_publish: true })} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    const view = container.querySelector(".app-view")!;
    const conversation = () => screen.getByRole("complementary", { name: "Conversation" }).getAttribute("data-shown");
    expect(view.hasAttribute("data-narrow")).toBe(true);
    expect(conversation()).toBe("false"); // the app pane is showing: its messages still notify
    fireEvent.click(screen.getByRole("button", { name: "Conversation" }));
    expect(conversation()).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    expect(view.getAttribute("data-pane")).toBe("preview"); // Settings lives on the app pane
    expect(conversation()).toBe("false");
    width.mockRestore();
  });

  it("an app from no room, shared with only its creator, names no room when refused", async () => {
    open.mockRejectedValue(Object.assign(new Error("not shared"), { status: 403 }));
    render(<AppView app={app({ room_id: null, room_name: undefined, open_to: "me", created_by_name: "Dana" })} onBack={vi.fn()} />);
    expect(await screen.findByText(/Only its creator and workspace admins can open Invoices\. Ask Dana to share it\./)).toBeTruthy();
  });

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

  it("an app with only a preview opens the preview and says how to make it live", async () => {
    open.mockResolvedValue("https://p.apps.test/?pass=1");
    render(<AppView app={app({ live_version: null, live_url: null, preview_url: "https://p", can_publish: true })} onBack={vi.fn()} />);
    const frame = await screen.findByTitle("Invoices");
    expect(frame.getAttribute("src")).toBe("https://p.apps.test/?pass=1");
    expect(open).toHaveBeenCalledWith("a1", "preview");
    expect(screen.getByText(/You're looking at the preview\. It isn't live until it's published: press Publish, or say “ship it” in the conversation\./)).toBeTruthy();
    expect(screen.queryByRole("group", { name: "View" })).toBeNull();
  });

  it("a stuck app says where it stopped instead of the preview notice", async () => {
    open.mockResolvedValue("https://p.apps.test/?pass=1");
    render(<AppView app={app({ live_version: null, live_url: null, preview_url: "https://p", stuck: { at: "2026-11-03T00:00:00Z", step: "building" } })} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    expect(screen.getByText("Stopped at building. Nothing has moved for 5 minutes.")).toBeTruthy();
    expect(screen.queryByText(/looking at the preview/)).toBeNull();
  });

  it("a stuck app offers a manager Ask the agent under the stopped line, and asks with no error", async () => {
    admin = true;
    open.mockResolvedValue("https://p.apps.test/?pass=1");
    askAgent.mockResolvedValue(undefined);
    render(<AppView app={app({ live_version: null, live_url: null, preview_url: "https://p", stuck: { at: "2026-11-03T00:00:00Z", step: "setting up hosting" } })} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    const line = screen.getByText("Stopped at setting up hosting. Nothing has moved for 5 minutes.");
    const button = screen.getByRole("button", { name: "Ask the agent to fix it" });
    expect(line.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.click(button);
    await screen.findByText("Asked the agent in the conversation. It'll answer there.");
    expect(askAgent).toHaveBeenCalledWith("a1", undefined);
  });

  it("a member who can't manage the app sees the stopped line but no Ask button", async () => {
    open.mockResolvedValue("https://p.apps.test/?pass=1");
    render(<AppView app={app({ stuck: { at: "2026-11-03T00:00:00Z", step: "building" } })} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    expect(screen.getByText("Stopped at building. Nothing has moved for 5 minutes.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Ask the agent to fix it" })).toBeNull();
  });

  it("a stuck building app shows only the stuck line, not the making-the-preview line", async () => {
    open.mockResolvedValue("https://p.apps.test/?pass=1");
    render(<AppView app={app({ state: "building", live_version: null, live_url: null, preview_url: null, stuck: { at: "2026-11-03T00:00:00Z", step: "building" } })} onBack={vi.fn()} />);
    expect(screen.getByText(/^Stopped at building\./)).toBeTruthy();
    expect(screen.queryByText(/making the preview/)).toBeNull();
  });

  it("while the agent is still working, the preview says it's a first version", async () => {
    open.mockResolvedValue("https://a1.preview.test/");
    render(<AppView app={app({ live_version: null, live_url: null, preview_url: "https://p", agent_working: true })} onBack={vi.fn()} />);
    await waitFor(() => expect(screen.getByTitle("Invoices")).toBeTruthy());
    expect(screen.getByText("This is a first version. The agent is still working on it and will post in the conversation when it's ready.")).toBeTruthy();
    expect(screen.queryByText(/looking at the preview/)).toBeNull();
  });

  it("no first-version notice over a building preview or a live app's preview", async () => {
    open.mockResolvedValue("https://a1.preview.test/");
    const { unmount } = render(<AppView app={app({ state: "building", live_version: null, live_url: null, preview_url: "https://p", agent_working: true })} onBack={vi.fn()} />);
    await waitFor(() => expect(screen.getByTitle("Invoices")).toBeTruthy());
    expect(screen.queryByText(/first version/)).toBeNull();
    unmount();
    render(<AppView app={app({ preview_url: "https://p", agent_working: true })} onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Preview" }));
    await waitFor(() => expect(screen.getByText(/looking at the preview/)).toBeTruthy());
    expect(screen.queryByText(/first version/)).toBeNull();
  });

  it("stuck wins over the first-version notice", async () => {
    open.mockResolvedValue("https://a1.preview.test/");
    render(<AppView app={app({ live_version: null, live_url: null, preview_url: "https://p", agent_working: true, stuck: { at: "2026-11-03T00:00:00Z", step: "building" } })} onBack={vi.fn()} />);
    await waitFor(() => expect(screen.getByTitle("Invoices")).toBeTruthy());
    expect(screen.queryByText(/first version/)).toBeNull();
    expect(screen.queryByText(/looking at the preview/)).toBeNull();
  });

  it("stuck: null keeps the usual preview notice", async () => {
    open.mockResolvedValue("https://p.apps.test/?pass=1");
    render(<AppView app={app({ live_version: null, live_url: null, preview_url: "https://p", stuck: null })} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    expect(screen.getByText(/You're looking at the preview\./)).toBeTruthy();
    expect(screen.queryByText(/Stopped at/)).toBeNull();
  });

  it("a failed publish says so in words, keeps the build error under Details, and leaves Publish available", async () => {
    open.mockResolvedValue("https://p.apps.test/?pass=1");
    render(<AppView app={app({ state: "failed", live_version: null, live_url: null, preview_url: "https://p", can_publish: true, error: { message: 'Command "npx drizzle-kit migrate && next build" exited with 1' } })} onBack={vi.fn()} />);
    await screen.findByTitle("Invoices");
    const alert = screen.getAllByRole("alert").map((a) => a.textContent).join();
    expect(alert).toContain("The last publish didn't land. Press Publish to try again, or ask the agent in the conversation.");
    expect(alert).toContain('Command "npx drizzle-kit migrate && next build" exited with 1');
    expect(screen.getByRole("button", { name: "Publish" })).toBeTruthy();
    expect(screen.queryByText("Something went wrong. Ask the agent in finance.")).toBeNull();
  });

  it("a 403 says who can open it and where to ask; raw text only under Details", async () => {
    open.mockRejectedValue(Object.assign(new Error("Forbidden"), { status: 403 }));
    render(<AppView app={app({ name: "workshop-signup" })} onBack={vi.fn()} />);
    expect((await screen.findByRole("alert")).textContent).toContain("Only finance can open Workshop Signup. Ask in finance.");
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
    open.mockRejectedValue(new Error("Ask in finance for access"));
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
    expect(screen.getByText("Open to finance. Only Dana or a workspace admin can change who can open it.")).toBeTruthy();
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
    expect(screen.getByText("You're looking at the preview. It isn't live until it's published. The data here is examples; the live app starts empty.")).toBeTruthy();
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

  it("an app with nothing built yet asks for no pass and says what's coming", () => {
    render(<AppView app={app({ state: "requested", preview_url: null, live_url: null, live_version: null })} onBack={vi.fn()} />);
    expect(open).not.toHaveBeenCalled();
    expect(screen.getByText(/^The agent is making the preview\./)).toBeTruthy();
    expect(screen.queryByText(/You're looking at the preview/)).toBeNull();
    expect(screen.queryByText("Opening…")).toBeNull();
    expect(screen.queryByRole("button", { name: "Open in its own tab" })).toBeNull();
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
      expect(screen.getByText("Make this preview live for everyone in the workspace? You can put back the version live now from here.")).toBeTruthy();
      expect(publish).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      expect(screen.getByRole("button", { name: "Publish" })).toBeTruthy();
      expect(publish).not.toHaveBeenCalled();
    });

    it("an app shared with only its creator says so grammatically in the confirm", async () => {
      render(<AppView app={ready({ open_to: "me" })} onBack={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Publish" }));
      expect(screen.getByText("Make this preview live just for its creator and workspace admins? You can put back the version live now from here.")).toBeTruthy();
    });

    it.each([
      ["room", "Live — people in finance can open it"],
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
      expect((await screen.findAllByRole("alert")).map((a) => a.textContent).join()).toContain("This version didn't go live. People still see the last live version. Try again, or ask the agent in its conversation to fix it.");
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

    it("a publish that fails on a live app ends Publishing…, with the row's error", async () => {
      publish.mockResolvedValue({ app_id: "a1", state: "building" });
      const { rerender } = render(<AppView app={ready()} onBack={vi.fn()} />);
      await clickPublish();
      await screen.findByText("Publishing…");
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      rerender(<AppView app={ready({ publish_failed: true, error: { message: "Deploy rejected" } })} onBack={vi.fn()} />);
      expect(screen.queryByText("Publishing…")).toBeNull();
      expect(screen.getByText(/This version didn't go live\. People still see the last live version\./)).toBeTruthy();
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

  describe("ask the agent to fix it", () => {
    const failed = (over: Partial<AppRow> = {}) => app({ state: "failed", live_version: null, live_url: null, preview_url: "https://p", can_publish: true, error: { message: "Build broke" }, ...over });
    const ask = () => screen.findByRole("button", { name: "Ask the agent to fix it" });
    beforeEach(() => open.mockResolvedValue("https://p.apps.test/?pass=1"));

    it("posts once, then says so and takes the button away", async () => {
      let finish = () => {};
      askAgent.mockReturnValue(new Promise<void>((r) => { finish = r; }));
      render(<AppView app={failed()} onBack={vi.fn()} />);
      fireEvent.click(await ask());
      const busy = screen.getByRole("button", { name: "Asking…" }) as HTMLButtonElement;
      expect(busy.disabled).toBe(true);
      fireEvent.click(busy);
      await act(async () => finish());
      expect(askAgent).toHaveBeenCalledTimes(1);
      expect(askAgent).toHaveBeenCalledWith("a1", undefined); // the backend reads the stored error
      // The conversation is opened first, so an older app with only its room gets one and the ask lands in it.
      expect(openConversation).toHaveBeenCalledWith("a1");
      expect(openConversation.mock.invocationCallOrder[0]).toBeLessThan(askAgent.mock.invocationCallOrder[0]);
      expect(screen.getByRole("status").textContent).toBe("Asked the agent in the conversation. It'll answer there.");
      expect(screen.queryByRole("button", { name: "Ask the agent to fix it" })).toBeNull();
    });

    it("someone outside the room is told why, and the raw text stays under Details", async () => {
      askAgent.mockRejectedValue(Object.assign(new Error("Only members"), { status: 403 }));
      render(<AppView app={failed()} onBack={vi.fn()} />);
      fireEvent.click(await ask());
      await screen.findByText(/You can't post in this app's conversation\./);
      expect(screen.getAllByText("Details").some((d) => d.closest("details")!.textContent!.includes("Only members"))).toBe(true);
    });

    it("any other failure can be tried again", async () => {
      askAgent.mockRejectedValueOnce(new Error("boom")).mockResolvedValueOnce(undefined);
      render(<AppView app={failed()} onBack={vi.fn()} />);
      fireEvent.click(await ask());
      await screen.findByText(/We couldn't ask\. Try again\./);
      fireEvent.click(await ask());
      expect((await screen.findByRole("status")).textContent).toContain("Asked the agent");
    });

    it("a member who can't manage the app gets no button", async () => {
      render(<AppView app={failed({ can_publish: false })} onBack={vi.fn()} />);
      await screen.findByTitle("Invoices");
      expect(screen.queryByRole("button", { name: "Ask the agent to fix it" })).toBeNull();
    });

    it("no agent in the room says what to do, not Try again", async () => {
      askAgent.mockRejectedValue(Object.assign(new Error("There is no agent"), { status: 409, body: { detail: { code: "no_agent" } } }));
      render(<AppView app={failed()} onBack={vi.fn()} />);
      fireEvent.click(await ask());
      const alert = await screen.findByText(/There's no agent in the conversation to ask\. Add one to the workspace\./);
      expect(alert.textContent).not.toContain("We couldn't ask");
      expect(screen.queryByText(/We couldn't ask/)).toBeNull();
    });

    it("a live app with a failed publish says so and offers it", async () => {
      render(<AppView app={app({ can_publish: true, publish_failed: true, error: { message: "Build broke" } })} onBack={vi.fn()} />);
      await ask();
      expect(screen.getByRole("alert").textContent).toContain("The last version didn't go live. People still see the version before it.");
      expect(screen.queryByText(/Something went wrong/)).toBeNull();
    });

    it("a healthy app has no button", async () => {
      render(<AppView app={app({ can_publish: true })} onBack={vi.fn()} />);
      await screen.findByTitle("Invoices");
      expect(screen.queryByRole("button", { name: "Ask the agent to fix it" })).toBeNull();
    });

    it("a refused publish sends the refusal the person saw, and a retry hides the button while it runs", async () => {
      publish.mockRejectedValueOnce(new Error("That change deletes data")).mockReturnValueOnce(new Promise(() => {}));
      askAgent.mockResolvedValue(undefined);
      render(<AppView app={app({ can_publish: true, preview_url: "https://p" })} onBack={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Publish" }));
      fireEvent.click(screen.getAllByRole("button", { name: "Publish" })[0]);
      fireEvent.click(await ask());
      await waitFor(() => expect(askAgent).toHaveBeenCalledWith("a1", "That change deletes data"));
      fireEvent.click(screen.getByRole("button", { name: "Try again" }));
      await screen.findByText("Publishing…");
      expect(screen.queryByRole("button", { name: "Ask the agent to fix it" })).toBeNull();
    });
  });

  describe("put back the previous version", () => {
    const AT = "2026-10-02T12:00:00+00:00";
    const DATE = new Date(AT).toLocaleDateString(undefined, { dateStyle: "medium" });
    const two = (over: Partial<AppRow> = {}) => app({ can_publish: true, previous_version: { n: 2, at: AT }, ...over });
    const button = () => screen.findByRole("button", { name: "Put back the previous version" });
    beforeEach(() => open.mockResolvedValue("https://x.apps.test/?pass=1"));

    it("confirms in plain words first; Cancel changes nothing", async () => {
      render(<AppView app={two({ display_name: "Workshop Sign-up" })} onBack={vi.fn()} />);
      fireEvent.click(await button());
      expect(screen.getByText(`Workshop Sign-up goes back to the version live on ${DATE}. Its data stays as it is.`)).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
      expect(rollback).not.toHaveBeenCalled();
      await button();
    });

    it("goes back to the version it named, says so and reloads the frame", async () => {
      rollback.mockResolvedValue(undefined);
      render(<AppView app={two()} onBack={vi.fn()} />);
      fireEvent.click(await button());
      expect(open).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getByRole("button", { name: "Put it back" }));
      await screen.findByText(`Invoices is back to the version live on ${DATE}.`);
      expect(rollback).toHaveBeenCalledWith("a1", 2);
      await waitFor(() => expect(open).toHaveBeenCalledTimes(2));
    });

    it("keeps the line when the first version comes back and the button goes", async () => {
      rollback.mockResolvedValue(undefined);
      const { rerender } = render(<AppView app={two()} onBack={vi.fn()} />);
      fireEvent.click(await button());
      fireEvent.click(screen.getByRole("button", { name: "Put it back" }));
      await screen.findByText(/is back to the version/);
      rerender(<AppView app={two({ live_version: 1, previous_version: null })} onBack={vi.fn()} />);
      expect(screen.getByText(/is back to the version/)).toBeTruthy();
      expect(screen.queryByRole("button", { name: "Put back the previous version" })).toBeNull();
    });

    it("a failure says nothing changed, raw text under Details, and can be retried", async () => {
      rollback.mockRejectedValue(new Error("vercel 502"));
      render(<AppView app={two()} onBack={vi.fn()} />);
      fireEvent.click(await button());
      fireEvent.click(screen.getByRole("button", { name: "Put it back" }));
      expect((await screen.findByRole("alert")).textContent).toContain("Invoices didn't change. Try again.");
      expect(screen.getByText("Details").closest("details")!.textContent).toContain("vercel 502");
      await button();
    });

    it("shows only for someone who manages an app with an earlier version", async () => {
      render(<AppView app={two({ can_publish: false })} onBack={vi.fn()} />);
      await screen.findByTitle("Invoices");
      expect(screen.queryByRole("button", { name: "Put back the previous version" })).toBeNull();
      cleanup();
      render(<AppView app={app({ can_publish: true, previous_version: null })} onBack={vi.fn()} />);
      await screen.findByTitle("Invoices");
      expect(screen.queryByRole("button", { name: "Put back the previous version" })).toBeNull();
    });

    it("hides while publishing, and a put-back after a publish doesn't read as still publishing", async () => {
      publish.mockResolvedValue({ app_id: "a1", state: "building" });
      rollback.mockResolvedValue(undefined);
      const { rerender } = render(<AppView app={two({ live_version: 3, preview_url: "https://p" })} onBack={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Publish" }));
      fireEvent.click(screen.getAllByRole("button", { name: "Publish" })[0]);
      await screen.findByText("Publishing…");
      expect(screen.queryByRole("button", { name: "Put back the previous version" })).toBeNull();
      rerender(<AppView app={two({ live_version: 4, previous_version: { n: 3, at: AT } })} onBack={vi.fn()} />);
      fireEvent.click(await button());
      fireEvent.click(screen.getByRole("button", { name: "Put it back" }));
      await screen.findByText(/is back to the version/);
      rerender(<AppView app={two({ live_version: 3 })} onBack={vi.fn()} />);
      expect(screen.queryByText("Publishing…")).toBeNull();
    });

    it("says a rebuild takes a few minutes, in the confirm and after it", async () => {
      rollback.mockResolvedValue({ app_id: "a1", state: "building", rebuilding: true });
      render(<AppView app={two({ previous_version: { n: 2, at: AT, rebuild: true } })} onBack={vi.fn()} />);
      fireEvent.click(await button());
      expect(screen.getByRole("group", { name: "Confirm putting back" }).textContent).toContain("takes a few minutes");
      fireEvent.click(screen.getByRole("button", { name: "Put it back" }));
      await screen.findByText(`Invoices is going back to the version live on ${DATE}. This takes a few minutes.`);
      expect(screen.queryByText(/is back to the version/)).toBeNull();
    });

    it("an instant put-back doesn't mention a rebuild", async () => {
      render(<AppView app={two({ previous_version: { n: 2, at: AT, rebuild: false } })} onBack={vi.fn()} />);
      fireEvent.click(await button());
      expect(screen.getByRole("group", { name: "Confirm putting back" }).textContent).not.toContain("few minutes");
    });

    it("says an agent's publish in flight will be cancelled", async () => {
      render(<AppView app={two({ state: "building", publishing: true })} onBack={vi.fn()} />);
      fireEvent.click(await button());
      expect(screen.getByRole("group", { name: "Confirm putting back" }).textContent).toContain("The publish in progress will be cancelled.");
    });

    it("a version that can't be rebuilt doesn't say Try again", async () => {
      rollback.mockRejectedValue(Object.assign(new Error("source gone"), { status: 409, body: { detail: { code: "version_unavailable" } } }));
      render(<AppView app={two({ previous_version: { n: 2, at: AT, rebuild: true } })} onBack={vi.fn()} />);
      fireEvent.click(await button());
      fireEvent.click(screen.getByRole("button", { name: "Put it back" }));
      const alert = await screen.findByRole("alert");
      expect(alert.textContent).toContain("That version can't be put back any more");
      expect(alert.textContent).not.toContain("Try again");
    });

    it("the line goes when a later version lands", async () => {
      rollback.mockResolvedValue(undefined);
      const { rerender } = render(<AppView app={two()} onBack={vi.fn()} />);
      fireEvent.click(await button());
      fireEvent.click(screen.getByRole("button", { name: "Put it back" }));
      await screen.findByText(/is back to the version/);
      rerender(<AppView app={two({ live_version: 2, previous_version: { n: 1, at: AT } })} onBack={vi.fn()} />);
      expect(screen.getByText(/is back to the version/)).toBeTruthy(); // the put-back's own drop keeps it
      rerender(<AppView app={two({ live_version: 4 })} onBack={vi.fn()} />);
      await waitFor(() => expect(screen.queryByText(/is back to the version/)).toBeNull());
    });

    it("the line goes when a publish starts", async () => {
      rollback.mockResolvedValue(undefined);
      publish.mockResolvedValue({ app_id: "a1", state: "building" });
      render(<AppView app={two({ state: "live", preview_url: "https://p", preview_newer: true })} onBack={vi.fn()} />);
      fireEvent.click(await button());
      fireEvent.click(screen.getByRole("button", { name: "Put it back" }));
      await screen.findByText(/is back to the version/);
      fireEvent.click(screen.getByRole("button", { name: "Publish" }));
      fireEvent.click(screen.getAllByRole("button", { name: "Publish" })[0]);
      await screen.findByText("Publishing…");
      expect(screen.queryByText(/is back to the version/)).toBeNull();
    });

    it("a live app can publish a newer preview, also after a put-back", async () => {
      publish.mockResolvedValue({ app_id: "a1", state: "building" });
      rollback.mockResolvedValue(undefined);
      const row = (over: Partial<AppRow> = {}) => two({ state: "live", preview_url: "https://p", preview_newer: true, ...over });
      const { rerender } = render(<AppView app={row()} onBack={vi.fn()} />);
      await screen.findByRole("button", { name: "Publish" });
      fireEvent.click(await button());
      fireEvent.click(screen.getByRole("button", { name: "Put it back" }));
      await screen.findByText(/is back to the version/);
      rerender(<AppView app={row({ live_version: 2, previous_version: { n: 1, at: AT } })} onBack={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Publish" }));
      fireEvent.click(screen.getAllByRole("button", { name: "Publish" })[0]);
      await waitFor(() => expect(publish).toHaveBeenCalledWith("a1"));
    });

    it("a live app showing the preview that is already live has nothing to publish", async () => {
      render(<AppView app={two({ state: "live", preview_url: "https://p", preview_newer: false })} onBack={vi.fn()} />);
      await screen.findByTitle("Invoices");
      expect(screen.queryByRole("button", { name: "Publish" })).toBeNull();
    });
  });
});
