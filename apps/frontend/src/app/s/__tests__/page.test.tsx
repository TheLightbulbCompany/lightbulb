import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@lightbulb/shared";

const createSessionCopy = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), BACKEND_URL: "https://api.test/api/v1", useApi: () => ({}) }));
vi.mock("@lightbulb/shared", async (orig) => ({ ...(await orig<typeof import("@lightbulb/shared")>()), createMessagingClient: () => ({ createSessionCopy }) }));
const clerk = vi.hoisted(() => ({ isLoaded: true, isSignedIn: false, orgId: null as string | null }));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: clerk.isLoaded, isSignedIn: clerk.isSignedIn, orgId: clerk.orgId, getToken: async () => (clerk.isSignedIn ? "jwt" : null) }),
  SignUpButton: ({ children }: { children: React.ReactNode }) => children,
}));
const push = vi.hoisted(() => vi.fn());
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));
vi.mock("@/components/messaging/NativeSessionTranscript", () => ({ NativeSessionTranscript: ({ events }: { events: { text: string }[] }) => <div>{events.map(e => e.text).join("|")}</div> }));

import LinkPage, { go, parseConfirm } from "../page";

const TOKEN = "t".repeat(43);
const PAGE = { title: "Fix login", ownerFirstName: "Prasiddha", provider: "claude", state: "ended", continuable: true,
  code: { visibility: "private" }, me: { signedIn: false, member: false } };
const fetchMock = vi.fn();

function answer(page = PAGE) {
  fetchMock.mockImplementation(async (url: string) => ({
    ok: true, status: 200,
    json: async () => (url.endsWith("/open") ? page : { events: [{ seq: 1, text: "hello" }], nextAfterSeq: null }),
  }));
}

describe("/s", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset(); createSessionCopy.mockReset(); push.mockReset();
    Object.assign(clerk, { isSignedIn: false, orgId: null });
    window.sessionStorage.clear();
    window.history.replaceState(null, "", `/s#${TOKEN}`);
  });

  it("lifts the token out of the URL, posts it in the body, and shows the ended page", async () => {
    answer();
    render(<LinkPage />);
    expect(await screen.findByText("Fix login")).toBeInTheDocument();
    expect(window.location.hash).toBe("");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ token: TOKEN });
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBeUndefined();
    expect(screen.getByText("Code is in a private repository")).toBeInTheDocument();
    expect(screen.getByText("hello")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign up to continue" })).toBeInTheDocument();
  });

  it("names an owner with no name as Someone", async () => {
    answer({ ...PAGE, ownerFirstName: "Someone" });
    render(<LinkPage />);
    expect(await screen.findByText("Someone's Claude Code session · Ended")).toBeInTheDocument();
  });

  it("a signed-in person with no workspace is sent to onboarding and back", async () => {
    Object.assign(clerk, { isSignedIn: true, orgId: null });
    answer({ ...PAGE, me: { signedIn: true, member: false } });
    render(<LinkPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Create your free workspace" }));
    expect(push).toHaveBeenCalledWith("/onboarding?returnTo=%2Fs");
  });

  it("shows the command for a signed-in person with a workspace", async () => {
    Object.assign(clerk, { isSignedIn: true, orgId: "org_new" });
    answer({ ...PAGE, me: { signedIn: true, member: false } });
    render(<LinkPage />);
    expect(await screen.findByText(`lightbulb continue '${window.location.origin}/s#${TOKEN}'`)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Get Lightbulb for Mac" })).toHaveAttribute("href", "/api/download/desktop");
  });

  it("in CLI mode, Continue makes the copy and hands the code to the loopback only", async () => {
    Object.assign(clerk, { isSignedIn: true, orgId: "org_new" });
    window.history.replaceState(null, "", `/s?port=53123&challenge=${"c".repeat(43)}#${TOKEN}`);
    answer({ ...PAGE, me: { signedIn: true, member: false } });
    createSessionCopy.mockResolvedValue({ code: "k".repeat(43) });
    const to = vi.spyOn(go, "to").mockImplementation(() => {});
    render(<LinkPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Continue in Claude Code" }));
    expect(createSessionCopy).toHaveBeenCalledWith(TOKEN, "c".repeat(43));
    expect(to).toHaveBeenCalledWith(`http://127.0.0.1:53123/done?code=${"k".repeat(43)}`);
  });

  it("says the server's words when Continue is refused, and keeps the page", async () => {
    Object.assign(clerk, { isSignedIn: true, orgId: "org_new" });
    window.history.replaceState(null, "", `/s?port=53123&challenge=${"c".repeat(43)}#${TOKEN}`);
    answer({ ...PAGE, me: { signedIn: true, member: false } });
    createSessionCopy.mockRejectedValue(new ApiError(429, { code: "RATE_LIMITED" }, "Too many copies of this session. Try again later."));
    const to = vi.spyOn(go, "to").mockImplementation(() => {});
    render(<LinkPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Continue in Claude Code" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Too many copies of this session. Try again later.");
    expect(screen.getByText("Fix login")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue in Claude Code" })).toBeEnabled();
    expect(to).not.toHaveBeenCalled();
  });

  it("refuses a confirm with a bad port or challenge", () => {
    expect(parseConfirm(`?port=53123&challenge=${"c".repeat(43)}`)).toEqual({ port: 53123, challenge: "c".repeat(43) });
    for (const bad of ["?port=80&challenge=" + "c".repeat(43), "?port=99999&challenge=" + "c".repeat(43), "?port=5000&challenge=short", "?port=5000.5&challenge=" + "c".repeat(43)]) {
      expect(parseConfirm(bad)).toBeNull();
    }
  });

  it("a closed link says so", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 404, json: async () => ({ code: "LOCAL_SESSION_NOT_FOUND", message: "This link was turned off or is not for you." }) });
    render(<LinkPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("This link was turned off or is not for you.");
  });
});
