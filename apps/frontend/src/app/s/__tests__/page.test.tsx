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
vi.mock("@/components/messaging/NativeSessionTranscript", () => ({ NativeSessionTranscript: () => null }));

import SessionLinkPageRoute from "../page";

const model = (over: object) => ({
  provider: "claude", state: "live", ownerFirstName: "Ada", code: { visibility: "none" },
  continuable: false, me: { signedIn: false, member: false }, ...over,
});

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); window.sessionStorage.clear(); });

describe("session link page", () => {
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
});
