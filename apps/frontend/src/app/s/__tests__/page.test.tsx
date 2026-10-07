import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";

// A freshly shared Claude session opens before the Mac commits its pick-up snapshot: the page
// must re-open itself while it is not continuable, and stop once it is.

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, useApi: () => ({}), BACKEND_URL: "http://api.test" };
});
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: false, orgId: null, getToken: vi.fn() }),
  SignInButton: ({ children }: { children: React.ReactNode }) => children,
  SignUpButton: ({ children }: { children: React.ReactNode }) => children,
}));

import SessionLinkPageRoute from "../page";

const model = (over: object) => ({
  provider: "claude", state: "live", ownerFirstName: "Ada", code: { visibility: "none" },
  continuable: false, me: { signedIn: false, member: false }, ...over,
});

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); window.sessionStorage.clear(); });

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

  it("shows a refused link (off, expired, not found) in the same card, with the sign-in offer", async () => {
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 410, json: async () => ({ message: "This link has expired." }) }) as Response));
    render(<SessionLinkPageRoute />);
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(inCard().textContent).toBe("This link has expired.");
    expect(screen.getByRole("button", { name: "Sign in to open this session" }).closest(".session-state-panel")).not.toBeNull();
  });

  it("re-opens on every tick while the session is live", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    const opens = [model({}), model({ title: "Fix the bug", continuable: true })];
    const fetchMock = vi.fn(async (url: string) => {
      const body = url.endsWith("/open") ? (opens.shift() ?? opens[0] ?? model({ title: "Fix the bug", continuable: true })) : { events: [], nextAfterSeq: null };
      return { ok: true, json: async () => body } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("A shared session")).toBeTruthy();
    const openCalls = () => fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/open")).length;
    expect(openCalls()).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByText("Fix the bug")).toBeTruthy();
    expect(openCalls()).toBe(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(openCalls()).toBe(5); // a live session keeps being re-opened, ready or not
  });

  it("follows the session to ended, and shows the refusal when the link is turned off", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    const opens: Array<{ ok: boolean; body: object }> = [
      { ok: true, body: model({ continuable: true }) },
      { ok: true, body: model({ continuable: true, state: "ended" }) },
    ];
    const fetchMock = vi.fn(async (url: string) => {
      if (!url.endsWith("/open")) return { ok: true, json: async () => ({ events: [], nextAfterSeq: null }) } as Response;
      const o = opens.shift() ?? { ok: false, body: { message: "This link is off." } };
      return { ok: o.ok, status: o.ok ? 200 : 404, json: async () => o.body } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText(/· Live/)).toBeTruthy();
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByText(/· Ended/)).toBeTruthy();
    const openCalls = () => fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/open")).length;
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(openCalls()).toBe(2); // ended: the poll stopped
  });

  it("stops polling and shows the refusal when the link is turned off", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    const opens: Array<{ ok: boolean; body: object }> = [{ ok: true, body: model({ continuable: true }) }];
    const fetchMock = vi.fn(async (url: string) => {
      if (!url.endsWith("/open")) return { ok: true, json: async () => ({ events: [], nextAfterSeq: null }) } as Response;
      const o = opens.shift() ?? { ok: false, body: { message: "This link is off." } };
      return { ok: o.ok, status: o.ok ? 200 : 404, json: async () => o.body } as Response;
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByRole("alert").textContent).toBe("This link is off.");
    const openCalls = () => fetchMock.mock.calls.filter(c => String(c[0]).endsWith("/open")).length;
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
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
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
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
    expect(screen.getByText(/The screen is gone/)).toBeTruthy();
    expect(fetchMock.mock.calls.some(c => String(c[0]).endsWith("/events"))).toBe(false);
  });
  it("tells a member to open a live session in Lightbulb, and a nonmember only that it is live", async () => {
    vi.useFakeTimers();
    window.sessionStorage.setItem("lightbulb:session-link", JSON.stringify({ token: "tok" }));
    let member = true;
    vi.stubGlobal("fetch", vi.fn(async (_url: string) => ({ ok: true, status: 200, json: async () => model({ state: "live", sessionId: "s1", me: { signedIn: member, member } }) } as Response)));
    const first = render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText(/Open it there to watch its terminal/)).toBeTruthy();
    first.unmount();
    member = false;
    render(<SessionLinkPageRoute />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("This session is live.")).toBeTruthy();
    expect(screen.queryByText(/Open it there/)).toBeNull();
  });
});
