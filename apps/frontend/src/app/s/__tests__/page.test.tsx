import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";

// A freshly shared Claude session opens before the Mac commits its pick-up snapshot: the page
// must re-open itself while it is not continuable, and stop once it is.

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, useApi: () => ({}), BACKEND_URL: "http://api.test" };
});
const router = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));
// The terminal itself has its own tests; here it is what the page gives it.
const terminal = vi.hoisted(() => ({ props: null as null | Record<string, unknown> }));
vi.mock("@/components/messaging/SessionTerminalView", () => ({
  SessionTerminalView: (props: Record<string, unknown>) => { terminal.props = props; return <div data-testid="terminal" />; },
}));
const sockets = vi.hoisted(() => ({ made: [] as Array<{ opts: { getToken: () => Promise<string | null> }; channels: string[][]; closed: boolean }> }));
vi.mock("@lightbulb/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@lightbulb/shared")>();
  class AppSyncEventsClient {
    channels: string[][] = []; closed = false;
    constructor(public opts: { getToken: () => Promise<string | null> }) { sockets.made.push(this); }
    setChannels(next: string[]) { this.channels.push(next); }
    close() { this.closed = true; }
  }
  return { ...actual, AppSyncEventsClient };
});
const auth = vi.hoisted(() => ({ isSignedIn: false }));
const clerk = vi.hoisted(() => ({ setActive: vi.fn(async () => undefined) }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: auth.isSignedIn, orgId: null, getToken: async () => "jwt" }),
  useClerk: () => ({ setActive: clerk.setActive }),
  SignInButton: ({ children }: { children: React.ReactNode }) => children,
  SignUpButton: ({ children }: { children: React.ReactNode }) => children,
}));

import SessionLinkPageRoute, { fullOf } from "../page";
import { ApiError } from "@/lib/api";

const model = (over: object) => ({
  provider: "claude", state: "live", ownerFirstName: "Ada", code: { visibility: "none" },
  continuable: false, me: { signedIn: false, member: false }, ...over,
});

afterEach(() => { auth.isSignedIn = false; vi.useRealTimers(); vi.unstubAllGlobals(); window.sessionStorage.clear(); router.replace.mockClear(); terminal.props = null; sockets.made.length = 0; });

describe("session link page", () => {
  // The acceptance finding: an incomplete link was one unstyled line in the top-left corner.
  const inCard = () => {
    const alert = screen.getByRole("alert");
    const card = alert.closest<HTMLElement>(".session-state-panel")!;
    expect(card).not.toBeNull();
    // The prod finding: one line near the top of an empty page. It is a bordered card in the middle of the viewport.
    expect(card.className).toContain("border");
    expect(card.parentElement?.className).toContain("content-center");
    expect(card.closest("main")?.className).toContain("min-h-dvh");
    expect(screen.getByRole("link", { name: "Lightbulb" }).getAttribute("href")).toBe("/");
    return alert;
  };

  it("shows an incomplete link in the page's card, under the product name", () => {
    render(<SessionLinkPageRoute />);
    expect(inCard().textContent).toBe("This link is incomplete. Ask for it again.");
  });

  // ISOA-732: a signed-out person who opens a workspace link gets the same refusal as a turned-off one.
  // The page leads with the step that can work, in words that are true in both cases.
  const refuse = () => vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 404, json: async () => ({ message: "This link was turned off or is not for you." }) }) as Response));
  it("leads a signed-out refusal (workspace link, off, expired, not found) with sign-in, in the same card", async () => {
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    refuse();
    render(<SessionLinkPageRoute />);
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(inCard().textContent).toBe("Sign in to open this session.");
    expect(screen.getByText("If it was shared with your workspace, it opens after you sign in. Otherwise this link was turned off.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Sign in" }).closest(".session-state-panel")).not.toBeNull();
    // The link is still in this tab for the round trip back to /s.
    expect(window.sessionStorage.getItem("lightbulb:session-link")).toContain("tok");
  });

  it("opens the same link after sign-in, with the person's token, and sends a member into the app", async () => {
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    refuse();
    const view = render(<SessionLinkPageRoute />);
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    const fetchMock = vi.fn(async (..._: unknown[]) => ({ ok: true, status: 200, json: async () => model({ state: "ended", title: "Fix login", me: { signedIn: true, member: true }, sessionId: "s1" }) }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    auth.isSignedIn = true;
    view.rerender(<SessionLinkPageRoute />);
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ token: "tok" });
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer jwt");
    expect(router.replace).toHaveBeenCalledWith("/chat?panel=session&session=s1");
    expect(screen.queryByRole("heading", { name: "Fix login" })).toBeNull(); // nothing of the page while it leaves
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("tells a signed-in person who is still refused that it is a dead end, with no sign-in offer", async () => {
    auth.isSignedIn = true;
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    refuse();
    render(<SessionLinkPageRoute />);
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(inCard().textContent).toBe("This link was turned off or is not for you.");
    expect(screen.getByText("You are signed in, so this link was turned off or was shared with a workspace you are not in. Ask the person who sent it for a new link.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
  });

  it("does not offer sign-in for a failure that is not a refusal", async () => {
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 500, json: async () => null }) as Response));
    render(<SessionLinkPageRoute />);
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(inCard().textContent).toBe("Could not open this link. Try again.");
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
  });

  // A link viewer has the terminal up: its own renewal hears trouble, so the page re-opens every 30 s, not 5.
  it("re-opens on every tick while the session is live", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    const opens = [model({}), model({ title: "Fix the bug", continuable: true })];
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      const body = url.endsWith("/open") ? (opens.shift() ?? opens[0] ?? model({ title: "Fix the bug", continuable: true })) : { events: [], nextAfterSeq: null };
      return { ok: true, json: async () => body } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("A shared session")).toBeTruthy();
    const openCalls = () => fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/open")).length;
    expect(openCalls()).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(screen.getByText("Fix the bug")).toBeTruthy();
    expect(openCalls()).toBe(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(90000); });
    expect(openCalls()).toBe(5); // a live session keeps being re-opened, ready or not
  });

  it("follows the session to ended, and shows the refusal when the link is turned off", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    const opens: Array<{ ok: boolean; body: object }> = [
      { ok: true, body: model({ continuable: true }) },
      { ok: true, body: model({ continuable: true, state: "ended" }) },
    ];
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      if (!url.endsWith("/open")) return { ok: true, json: async () => ({ events: [], nextAfterSeq: null }) } as Response;
      const o = opens.shift() ?? { ok: false, body: { message: "This link is off." } };
      return { ok: o.ok, status: o.ok ? 200 : 404, json: async () => o.body } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText(/· Live/)).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(screen.getByText(/· Ended/)).toBeTruthy();
    const openCalls = () => fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/open")).length;
    await act(async () => { await vi.advanceTimersByTimeAsync(90000); });
    expect(openCalls()).toBe(2); // ended: the poll stopped
  });

  // Round 18: the owner narrowed the link and widened it again; the open page never came back.
  it("keeps asking after a refusal, slowly and for ten minutes, and comes back when the link opens again", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    let open = true;
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      if (url.endsWith("/viewing")) return { ok: true, status: 200, json: async () => seat } as Response;
      return open ? { ok: true, status: 200, json: async () => model({ title: "ALPHA18" }) } as Response
        : { ok: false, status: 404, json: async () => ({ message: "This link was turned off or is not for you." }) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    const openCalls = () => fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/open")).length;
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole("heading", { name: "ALPHA18" })).toBeTruthy();
    open = false; // narrowed to My workspace
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(screen.getByRole("alert").textContent).toBe("Sign in to open this session.");
    const refusedAt = openCalls();
    await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
    expect(openCalls()).toBe(refusedAt + 2); // one every 30 s
    open = true; // Anyone with the link again
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(screen.getByRole("heading", { name: "ALPHA18" })).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    // Refused for good: it gives up after ten minutes.
    open = false;
    await act(async () => { await vi.advanceTimersByTimeAsync(30 * 60000); });
    const stopped = openCalls();
    await act(async () => { await vi.advanceTimersByTimeAsync(10 * 60000); });
    expect(openCalls()).toBe(stopped);
  });

  it("stops asking while the tab is hidden, and asks at once when the person comes back", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    let open = false;
    const fetchMock = vi.fn(async () => open ? { ok: true, status: 200, json: async () => model({ title: "ALPHA18", state: "offline" }) } as Response
      : { ok: false, status: 404, json: async () => ({ message: "This link was turned off or is not for you." }) } as Response);
    vi.stubGlobal("fetch", fetchMock);
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(5 * 60000); });
    expect(fetchMock).toHaveBeenCalledTimes(1); // hidden: nothing after the first refusal
    open = true;
    visibility.mockReturnValue("visible");
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole("heading", { name: "ALPHA18" })).toBeTruthy();
    visibility.mockRestore();
  });

  it("shows the refusal when the link is turned off, and asks again only slowly", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    const opens: Array<{ ok: boolean; body: object }> = [{ ok: true, body: model({ continuable: true }) }];
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      if (!url.endsWith("/open")) return { ok: true, json: async () => ({ events: [], nextAfterSeq: null }) } as Response;
      const o = opens.shift() ?? { ok: false, body: { message: "This link is off." } };
      return { ok: o.ok, status: o.ok ? 200 : 404, json: async () => o.body } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    // Signed out, so the same words as any other refusal: sign in, or it was turned off.
    expect(screen.getByRole("alert").textContent).toBe("Sign in to open this session.");
    const openCalls = () => fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/open")).length;
    await act(async () => { await vi.advanceTimersByTimeAsync(29000); });
    expect(openCalls()).toBe(2);
  });

  it("keeps polling through a throttle or a server error, and the poll is a refresh", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    let n = 0;
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      if (!url.endsWith("/open")) return { ok: true, status: 200, json: async () => ({ events: [], nextAfterSeq: null }) } as Response;
      n += 1;
      if (n === 2) return { ok: false, status: 429, json: async () => ({ message: "Too many requests" }) } as Response;
      if (n === 3) return { ok: false, status: 503, json: async () => null } as Response;
      return { ok: true, status: 200, json: async () => model({ continuable: true }) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(90000); });
    expect(screen.queryByRole("alert")).toBeNull();
    const opens = fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/open"));
    expect(opens.length).toBe(4);
    expect(JSON.parse(String(opens[0][1]?.body)).refresh).toBeUndefined();
    expect(JSON.parse(String(opens[1][1]?.body)).refresh).toBe(true);
  });
  it("opens a second link pasted into the same tab", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "first" }));
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (!url.endsWith("/open")) return { ok: true, status: 200, json: async () => ({ events: [], nextAfterSeq: null }) } as Response;
      const token = JSON.parse(String(init?.body)).token;
      return { ok: true, status: 200, json: async () => model({ state: "ended", title: token === "second" ? "The second session" : "The first session" }) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("The first session")).toBeTruthy();
    // Only the fragment changes: the browser fires hashchange and does not reload the page.
    await act(async () => { window.location.hash = "#second"; window.dispatchEvent(new HashChangeEvent("hashchange")); await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("The second session")).toBeTruthy();
    expect(window.location.hash).toBe(""); // the token left the address bar again
  });
  it("shows the state, never a transcript, and does not fetch events", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    const fetchMock = vi.fn(async (_url: string) => ({ ok: true, status: 200, json: async () => model({ state: "ended" }) } as Response));
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    // (Nothing about the screen: nobody sees one here, and the Mac may still keep it for teammates.)
    expect(screen.getByLabelText("Session state").textContent).toBe("Ended");
    expect(fetchMock.mock.calls.some(c => String(c[0]).endsWith("/events"))).toBe(false);
  });
  // The owner's report (2026-10-08): "Anyone with the link" showed a signed-out person only "This
  // session is live.", and someone signed in to another workspace the Continue instructions.
  const seat = { lease: "pub-1.key-1", channel: "/pub/pub-1", watchId: "pub-1", httpEndpoint: "https://events.test/event", realtimeEndpoint: "wss://events.test/event/realtime" };
  it.each([false, true])("shows anyone holding the link the live terminal, view only (signed in elsewhere: %s)", async (signedIn) => {
    vi.useFakeTimers();
    auth.isSignedIn = signedIn;
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => ({ ok: true, status: 200, json: async () => url.endsWith("/viewing") ? seat : model({ title: "Fix login", me: { signedIn, member: false } }) }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole("heading", { name: "Fix login" })).toBeTruthy();
    expect(screen.getByText(/Ada's Claude Code session · Live/)).toBeTruthy();
    expect(screen.getByRole("region", { name: "Live terminal" })).toContainElement(screen.getByTestId("terminal"));
    expect(router.replace).not.toHaveBeenCalled();
    // Watch only by default: no Continue, no "Get Lightbulb for Mac", no command.
    expect(screen.queryByText(/lightbulb continue/)).toBeNull();
    expect(screen.queryByText("Get Lightbulb for Mac")).toBeNull();
    // The seat is taken with the link's token alone, in a body, with no sign-in.
    const viewing = () => fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/viewing"));
    expect(JSON.parse(String(viewing()[0][1]?.body))).toEqual({ token: "tok" });
    expect((viewing()[0][1]?.headers as Record<string, string>).Authorization).toBeUndefined();
    // View only, on the seat's own channel, under an id that is not the session's.
    const props = terminal.props!;
    expect(props.admitted).toBe(false);
    expect(props.input).toBeUndefined();
    expect(props.session).toEqual({ sessionId: "pub-1", generation: "pub-1" });
    expect((props.frames as { channel: string }).channel).toBe("/pub/pub-1");
    expect(await sockets.made[0].opts.getToken()).toBe("pub-1.key-1"); // the lease is the whole credential
    const release = (props.frames as { claim: (c: string, r: () => void) => () => void }).claim("/pub/pub-1", () => {});
    expect(sockets.made[0].channels).toEqual([["/pub/pub-1"]]);
    release();
    // The terminal's own 15 s renewal carries the lease, and `fresh` asks the Mac for a picture.
    await act(async () => { await (props.watch as (id: string, fresh?: boolean) => Promise<void>)("pub-1", true); });
    expect(JSON.parse(String(viewing()[1][1]?.body))).toEqual({ token: "tok", lease: "pub-1.key-1", fresh: true });
  });

  it("re-opens at once when the terminal's seat is refused, and shows what happened", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    let ended = false;
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      if (!url.endsWith("/viewing")) return { ok: true, status: 200, json: async () => model({ state: ended ? "ended" : "live" }) } as Response;
      return ended ? { ok: false, status: 409, json: async () => ({ code: "LOCAL_SESSION_OFFLINE", message: "This session is not running." }) } as Response
        : { ok: true, status: 200, json: async () => seat } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    ended = true;
    await act(async () => { await (terminal.props!.watch as (id: string) => Promise<void>)("pub-1").catch(() => {}); await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByLabelText("Session state").textContent).toBe("Ended"); // no 30 s wait
    expect(screen.queryByTestId("terminal")).toBeNull();
  });

  // Joined inside another viewer's 15 s ask window on an idle session: the server refused the ask
  // and nothing was coming. The page keeps asking until it has a picture.
  it("asks for a picture on every renewal until a snapshot arrives, then stops", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => ({ ok: true, status: 200, json: async () => url.endsWith("/viewing") ? seat : model({}) }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const renew = terminal.props!.watch as (id: string, fresh?: boolean) => Promise<void>;
    const last = () => JSON.parse(String(fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/viewing")).at(-1)![1]?.body));
    await act(async () => { await renew("pub-1", false); });
    expect(last().fresh).toBe(true); // a plain renewal, but still no picture
    const frame = (kind: string) => ({ eventId: kind, type: "workspace.session.screen", payload: { sessionId: "pub-1", generation: "pub-1", epoch: 1, seq: 0, cols: 80, rows: 24, kind, data: "" } });
    const deliver = (sockets.made[0].opts as unknown as { onEvent: (e: unknown) => void }).onEvent;
    deliver(frame("output"));
    await act(async () => { await renew("pub-1", false); });
    expect(last().fresh).toBe(true); // output alone draws nothing on an empty terminal
    deliver(frame("snapshot"));
    await act(async () => { await renew("pub-1", false); });
    expect(last().fresh).toBeUndefined();
  });

  // Codex on #3946: AppSync down for longer than the 45 s lease right after the seat was taken, and
  // the socket retried with the lapsed lease until a reload.
  it("keeps the seat while the channel is not live, so the socket's next attempt has a live lease", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    let lease = "pub-1.key-1";
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => ({ ok: true, status: 200, json: async () => url.endsWith("/viewing") ? { ...seat, lease } : model({}) }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const viewing = () => fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/viewing"));
    expect(viewing()).toHaveLength(1);
    lease = "pub-1.key-2"; // the first lapsed: the server mints another
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(JSON.parse(String(viewing()[1][1]?.body))).toEqual({ token: "tok", lease: "pub-1.key-1", retake: true }); // no `fresh`: nothing can hear a picture yet
    expect(await sockets.made[0].opts.getToken()).toBe("pub-1.key-2");
    // Live: the terminal's own renewal takes over, and the page sends none.
    await act(async () => { (sockets.made[0].opts as unknown as { onStatus: (s: { connected: boolean; liveChannels: string[] }) => void }).onStatus({ connected: true, liveChannels: ["/pub/pub-1"] }); });
    await act(async () => { await vi.advanceTimersByTimeAsync(45000); });
    expect(viewing()).toHaveLength(2);
  });

  // Codex on #3946: a lease connects once and subscribes once (the authorizer consumes it). Before
  // the socket connects again the page asks for a seat it can connect with, and the socket waits.
  it("asks for a usable seat before every further connect, and never offers one lease to two", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    let lease = "pub-1.key-1";
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => ({ ok: true, status: 200, json: async () => url.endsWith("/viewing") ? { ...seat, lease } : model({}) }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const socket = sockets.made[0].opts as unknown as { getToken: () => Promise<string | null>; onStatus: (s: { connected: boolean; liveChannels: string[] }) => void; onError: (kind: string, channel?: string) => void };
    const viewing = () => fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/viewing")).map(c => JSON.parse(String(c[1]?.body)));
    // The first connect, then its subscribe on that connection: the seat just taken, and no request.
    expect(await socket.getToken()).toBe("pub-1.key-1");
    await act(async () => { socket.onStatus({ connected: true, liveChannels: [] }); });
    expect(await socket.getToken()).toBe("pub-1.key-1");
    expect(viewing()).toHaveLength(1);
    await act(async () => { socket.onStatus({ connected: true, liveChannels: ["/pub/pub-1"] }); });
    // The socket drops: the page asks at once, and the reconnect's connect waits for the answer.
    lease = "pub-1.key-2";
    let token!: Promise<string | null>;
    await act(async () => { socket.onStatus({ connected: false, liveChannels: [] }); token = socket.getToken(); });
    expect(viewing().at(-1)).toEqual({ token: "tok", lease: "pub-1.key-1", retake: true });
    expect(await token).toBe("pub-1.key-2"); // never the used lease
    expect(viewing()).toHaveLength(2);
    // That connect fails too (AppSync may or may not have taken the lease): ask again before the next.
    lease = "pub-1.key-3";
    await act(async () => { token = socket.getToken(); await token; });
    expect(viewing().at(-1)).toEqual({ token: "tok", lease: "pub-1.key-2", retake: true });
    expect(await token).toBe("pub-1.key-3");
    // A refused subscribe asks once more.
    await act(async () => { socket.onStatus({ connected: true, liveChannels: [] }); socket.onError("subscribe_error", "/pub/pub-1"); });
    expect(viewing().at(-1)).toEqual({ token: "tok", lease: "pub-1.key-3", retake: true });
  });

  // Codex on #3946: a trade still pending after 15 s was asked for again by the tick or by the
  // tab coming back, and the server gave a new seat for each: one browser, several seats.
  it("asks once while a trade is in flight, however many things ask for it", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    let answer: (() => void) | null = null;
    let slow = false;
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      if (url.endsWith("/viewing") && slow) await new Promise<void>((resolve) => { answer = resolve; });
      return { ok: true, status: 200, json: async () => url.endsWith("/viewing") ? { ...seat, lease: slow ? "pub-1.key-2" : seat.lease } : model({}) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const socket = sockets.made[0].opts as unknown as { getToken: () => Promise<string | null>; onStatus: (s: { connected: boolean; liveChannels: string[] }) => void; onError: (kind: string, channel?: string) => void };
    const viewing = () => fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/viewing"));
    await act(async () => { socket.onStatus({ connected: true, liveChannels: ["/pub/pub-1"] }); });
    slow = true;
    await act(async () => { socket.onStatus({ connected: false, liveChannels: [] }); }); // dropped: the trade starts
    expect(viewing()).toHaveLength(2);
    // Still pending: two ticks of the 15 s keepalive, the tab shown again, and a refused subscribe.
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); socket.onError("subscribe_error", "/pub/pub-1"); });
    expect(viewing()).toHaveLength(2);
    // It answers: the socket gets the one new lease, and a later ask is a new request.
    const token = socket.getToken();
    await act(async () => { answer!(); await vi.advanceTimersByTimeAsync(0); });
    expect(await token).toBe("pub-1.key-2");
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(viewing()).toHaveLength(3);
  });

  // A channel gives out a life total of seats, then the link moves to a new one. A viewer's next
  // renewal answers with a seat there: the page follows with that seat, not a second one.
  it("follows the link to its new channel with the seat the renewal gave it", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    let now = seat;
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => ({ ok: true, status: 200, json: async () => url.endsWith("/viewing") ? now : model({}) }) as Response);
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    now = { ...seat, lease: "pub-2.key-9", channel: "/pub/pub-2", watchId: "pub-2" };
    await act(async () => { await (terminal.props!.watch as (id: string) => Promise<void>)("pub-1"); await vi.advanceTimersByTimeAsync(0); });
    const viewing = fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/viewing")).map(c => JSON.parse(String(c[1]?.body)));
    expect(viewing.at(-1).lease).toBe("pub-2.key-9"); // renewed, so no second seat on the new channel
    expect(sockets.made[0].closed).toBe(true);
    expect((terminal.props!.frames as { channel: string }).channel).toBe("/pub/pub-2");
    expect(terminal.props!.session).toEqual({ sessionId: "pub-2", generation: "pub-2" });
  });

  it("shows the Continue instructions only when the owner turned Continue on", async () => {
    vi.useFakeTimers();
    auth.isSignedIn = true;
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    vi.stubGlobal("fetch", vi.fn(async (url: string) => ({ ok: true, status: 200, json: async () => url.endsWith("/viewing") ? seat : model({ continuable: true, me: { signedIn: true, member: false } }) }) as Response));
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByTestId("terminal")).toBeTruthy();
    expect(screen.getByRole("region", { name: "Continue" })).toBeTruthy();
  });

  it("offers every signed-out viewer their own workspace, but not in the CLI's confirm mode (T16)", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    vi.stubGlobal("fetch", vi.fn(async (url: string) => ({ ok: true, status: 200, json: async () => url.endsWith("/viewing") ? seat : model({}) }) as Response));
    const { unmount } = render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const door = screen.getByRole("region", { name: "Join" });
    expect(door.textContent).toContain("Ada is working with their team and AI employees in Lightbulb.");
    window.sessionStorage.removeItem("lightbulb:workspace_door");
    fireEvent.click(screen.getByRole("button", { name: "Start your own workspace" }));
    // T12: the workspace form sends this door, so workspace_created says {source: session_link}.
    expect(window.sessionStorage.getItem("lightbulb:workspace_door")).toBe("session_link");
    unmount();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok", port: 4321, challenge: "c".repeat(43) }));
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.queryByRole("region", { name: "Join" })).toBeNull();
  });

  it("lets a signed-in outsider ask to join, says it is waiting, and opens the session once allowed (T17)", async () => {
    vi.useFakeTimers();
    auth.isSignedIn = true;
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    let me: object = { signedIn: true, member: false, canAsk: true };
    let extra: object = {};
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      if (url.endsWith("/viewing")) return { ok: true, status: 200, json: async () => seat } as Response;
      if (url.endsWith("/join-request")) return { ok: true, status: 200, json: async () => ({ join: "pending" }) } as Response;
      return { ok: true, status: 200, json: async () => model({ me, ...extra }) } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { screen.getByRole("button", { name: "Ask to join Ada's workspace" }).click(); await vi.advanceTimersByTimeAsync(0); });
    const [, init] = fetchMock.mock.calls.find(([u]) => String(u).endsWith("/join-request"))!;
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ token: "tok" });
    expect(screen.getByRole("region", { name: "Join" }).textContent).toContain("Ada has your request.");
    me = { signedIn: true, member: false, join: "allowed" };
    extra = { sessionId: "s-1", workspaceId: "org_ada" };
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    await act(async () => { screen.getByRole("button", { name: "Open in Lightbulb" }).click(); await vi.advanceTimersByTimeAsync(0); });
    expect(clerk.setActive).toHaveBeenCalledWith({ organization: "org_ada" });
  });

  // Round 18: a signed-out person saw only "Sign up to continue", and nothing about the Mac app or the command.
  it("tells a signed-out person how to continue: the Mac app, then the command; sign-up is the CLI's confirm step", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    vi.stubGlobal("fetch", vi.fn(async (url: string) => ({ ok: true, status: 200, json: async () => url.endsWith("/viewing") ? seat : model({ continuable: true }) }) as Response));
    const { unmount } = render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const steps = screen.getByRole("region", { name: "Continue" });
    expect(steps.querySelector("a")?.getAttribute("href")).toBe("/api/download/desktop");
    expect(steps.textContent).toContain("lightbulb continue '");
    expect(steps.textContent).toContain("It opens this page again, where you sign up, free, and confirm");
    expect(screen.queryByRole("button", { name: "Sign up to continue" })).toBeNull();
    unmount();
    // The page the command opened: here signing up is the next step.
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok", port: 4321, challenge: "c".repeat(43) }));
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole("button", { name: "Sign up to continue" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Continue" }).textContent).not.toContain("lightbulb continue '");
  });

  it("reads a burst refusal with no count as plainly full", () => {
    expect(fullOf(new ApiError(409, { code: "LOCAL_SESSION_VIEWERS_FULL" }, "Too many"))?.note).toBe("This session is full. Try again in a minute.");
    expect(fullOf(new ApiError(409, { code: "LOCAL_SESSION_OFFLINE" }, "x"))).toBeNull();
  });

  it("says why there is no terminal, and tries again later", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    let full = true;
    const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
      if (!url.endsWith("/viewing")) return { ok: true, status: 200, json: async () => model({}) } as Response;
      return full ? { ok: false, status: 409, json: async () => ({ code: "LOCAL_SESSION_VIEWERS_FULL", message: "This session already has 20 people watching by link.", details: { watching: 20 } }) } as Response
        : { ok: true, status: 200, json: async () => seat } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByLabelText("Session state").textContent).toBe("Full: 20 people are watching. Try again in a minute.");
    full = false;
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(screen.getByTestId("terminal")).toBeTruthy();
  });

  it("sends a workspace member into the app's session view, except in the CLI's confirm mode", async () => {
    vi.useFakeTimers();
    auth.isSignedIn = true;
    const open = () => vi.stubGlobal("fetch", vi.fn(async (_url: string) => ({ ok: true, status: 200, json: async () => model({ state: "live", sessionId: "s1", me: { signedIn: true, member: true } }) } as Response)));
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    open();
    const first = render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(router.replace).toHaveBeenCalledWith("/chat?panel=session&session=s1");
    expect(screen.queryByTestId("terminal")).toBeNull();
    first.unmount();
    router.replace.mockClear();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok", port: 4000, challenge: "c".repeat(43) }));
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(router.replace).not.toHaveBeenCalled();
    expect(screen.getByText(/Open it there to watch its terminal/)).toBeTruthy();
    expect(screen.queryByTestId("terminal")).toBeNull();
  });
});
