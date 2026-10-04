import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { useEffect, useState } from "react";
import { SWRConfig } from "swr";
import { ApiError } from "@lightbulb/shared";

const mockGet = vi.fn();
const mockDownload = vi.fn();
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: mockGet, downloadBlob: mockDownload }) }));
vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ organization: { id: "org_1" } }) }));
const live = { onEvent: vi.fn(() => () => {}) };
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => live }));
vi.mock("@/hooks/useAgents", () => ({ useAgents: () => ({ agents: [{ id: "a1", name: "Finance" }] }) }));
const nav = vi.hoisted(() => ({ urls: [] as string[], setItem: (() => {}) as (item: string | null) => void }));
vi.mock("@/lib/shellNav", () => ({
  navigateShell: (url: string) => {
    nav.urls.push(url);
    const params = new URL(url, "http://x").searchParams;
    if (params.get("panel") === "mail") nav.setItem(params.get("item"));
  },
}));

import { MailPanel } from "../MailPanel";
import { useNewMailCount } from "../useTeamMail";

const ADDR = "agents@acme.thelightbulb.company";
const MEETING = "11111111-1111-4111-8111-111111111111";
const T1 = 1790553600;
const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString();

const received = {
  key: "k2", direction: "in", from: "pat@acme.com", to: ADDR, subject: "Quarterly numbers",
  at: hoursAgo(1), agent_id: "a1", text: "Here are the numbers.", truncated: true,
};
const sent = {
  key: "k1", direction: "out", from: ADDR, to: "vendor@x.y", subject: "Invoice question",
  at: hoursAgo(2), agent_id: "a1", text: "", truncated: false,
};
const meeting = {
  id: MEETING, title: "Weekly sync", organizer: "pat@acme.com", invited_by: null, status: "ended",
  start_at: T1, end_at: T1 + 1800, repeats: false, transcripts: [{ start_at: T1 }],
};
const page = { address: ADDR, meetings: [meeting], messages: [received, sent], next: null };

function Harness({ initial = null }: { initial?: string | null }) {
  const [item, setItem] = useState<string | null>(initial);
  useEffect(() => { nav.setItem = setItem; }, []);
  return <MailPanel item={item} />;
}

function renderMail(initial: string | null = null) {
  return render(<SWRConfig value={{ provider: () => new Map() }}><Harness initial={initial} /></SWRConfig>);
}

const row = (key: string) => document.querySelector<HTMLElement>(`[data-mail-key="${key}"]`)!;

describe("MailPanel", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockDownload.mockReset();
    nav.urls = [];
    window.localStorage.clear();
  });
  afterEach(() => vi.restoreAllMocks());

  it("lists mail in both directions and meetings", async () => {
    mockGet.mockResolvedValue(page);
    renderMail();
    expect(await screen.findByText("pat@acme.com")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith("/agent-mail/inbox");
    expect(screen.getByText("vendor@x.y")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Received" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Sent" })).toBeInTheDocument();
    expect(screen.getByText("Weekly sync")).toBeInTheDocument();
    expect(screen.getByText("Ended · Transcript")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Today" })).toBeInTheDocument();
  });

  it("fetches the full text only for a cut mail, and keeps the cut text when that fails", async () => {
    mockGet.mockImplementation((url: string) =>
      url.startsWith("/agent-mail/message") ? Promise.reject(new ApiError(404, {})) : Promise.resolve(page));
    renderMail();
    fireEvent.click(await screen.findByText("Invoice question"));
    expect(await screen.findByText("The text of this mail was not kept.")).toBeInTheDocument();
    expect(mockGet).not.toHaveBeenCalledWith(expect.stringContaining("/agent-mail/message"));

    act(() => nav.setItem(null));
    fireEvent.click(await screen.findByText("Quarterly numbers"));
    expect(await screen.findByText("Here are the numbers.")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith("/agent-mail/message?key=k2");
    expect(await screen.findByText("Showing the first 2,000 characters.")).toBeInTheDocument();
    expect(screen.getByText(/Handled by Finance/)).toBeInTheDocument();
  });

  it("narrow: list, then detail, then Escape back to the list with focus on the row", async () => {
    mockGet.mockResolvedValue({ ...page, messages: [{ ...received, truncated: false }, sent] });
    renderMail();
    await screen.findByText("Quarterly numbers");
    fireEvent.click(row("mail:k2"));
    expect(nav.urls.at(-1)).toBe("/chat?panel=mail&item=mail%3Ak2");
    expect(await screen.findByRole("heading", { name: "Quarterly numbers" })).toBeInTheDocument();
    expect(screen.queryByText("vendor@x.y")).toBeNull();
    fireEvent.keyDown(screen.getByRole("button", { name: "Mail" }), { key: "Escape" });
    await waitFor(() => expect(row("mail:k2")).toHaveFocus());
    expect(nav.urls.at(-1)).toBe("/chat?panel=mail");
  });

  it("wide: shows the newest row in the detail without pushing a URL", async () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 1000 } as DOMRect);
    mockGet.mockResolvedValue({ ...page, messages: [{ ...received, truncated: false }, sent] });
    renderMail();
    expect(await screen.findByRole("heading", { name: "Quarterly numbers" })).toBeInTheDocument();
    expect(row("mail:k2")).toHaveAttribute("aria-current", "true");
    expect(screen.getByText("vendor@x.y")).toBeInTheDocument(); // the list stays beside it
    expect(nav.urls).toEqual([]);
  });

  it("roves focus with arrows, j and End, and opens only on Enter", async () => {
    mockGet.mockResolvedValue({ ...page, messages: [{ ...received, truncated: false }, sent] });
    renderMail();
    await screen.findByText("Quarterly numbers");
    const keys = [...document.querySelectorAll<HTMLElement>("[data-mail-key]")].map((el) => el.dataset.mailKey!);
    expect(keys.map((k) => row(k).tabIndex)).toEqual([0, -1, -1]);
    row(keys[0]).focus();
    fireEvent.keyDown(row(keys[0]), { key: "ArrowDown" });
    expect(row(keys[1])).toHaveFocus();
    fireEvent.keyDown(row(keys[1]), { key: "k" });
    expect(row(keys[0])).toHaveFocus();
    fireEvent.keyDown(row(keys[0]), { key: "j" });
    fireEvent.keyDown(row(keys[1]), { key: "End" });
    expect(row(keys[2])).toHaveFocus();
    expect(row(keys[2]).tabIndex).toBe(0);
    expect(nav.urls).toEqual([]); // moving focus opens nothing
    fireEvent.keyDown(row(keys[2]), { key: "Home", metaKey: true });
    expect(row(keys[2])).toHaveFocus(); // modifiers are left alone
    fireEvent.click(row(keys[2])); // Enter/Space on a button is a click
    expect(nav.urls.at(-1)).toContain(`item=${encodeURIComponent(keys[2])}`);
  });

  it("finds in the transcript, counts matches, and ignores j/k in the field", async () => {
    mockGet.mockResolvedValue(page);
    mockDownload.mockResolvedValue({ text: async () => "[20:00] Pat: Budget first\n[20:01] Sam: no budget talk\n[20:02] Pat: ok" });
    renderMail(`meeting:${MEETING}:${T1}`);
    const find = await screen.findByRole("searchbox", { name: "Find in transcript" });
    expect(mockDownload).toHaveBeenCalledWith(`/agent-mail/meetings/${MEETING}/transcript?start_at=${T1}`);
    expect(screen.getAllByText("Pat")).toHaveLength(2);
    fireEvent.change(find, { target: { value: "budget" } });
    expect(screen.getByRole("status")).toHaveTextContent("2 matches");
    expect(document.querySelectorAll("mark")).toHaveLength(2);
    expect(screen.queryByText("ok")).toBeNull();
    find.focus();
    fireEvent.keyDown(find, { key: "j" });
    expect(find).toHaveFocus();
    fireEvent.change(find, { target: { value: "" } });
    expect(screen.getByText("ok")).toBeInTheDocument();
  });

  it("dots mail newer than this device's mark, then moves the mark", async () => {
    window.localStorage.setItem("lightbulb.mail.seen.org_1", hoursAgo(5));
    mockGet.mockResolvedValue(page);
    renderMail();
    await screen.findByText("pat@acme.com");
    expect(row("mail:k2")).toHaveAttribute("data-new", "true");
    expect(row("mail:k1")).not.toHaveAttribute("data-new");
    await waitFor(() => expect(window.localStorage.getItem("lightbulb.mail.seen.org_1")).toBe(received.at));
    expect(row("mail:k2")).toHaveAttribute("data-new", "true"); // the dot stays while open
  });

  it("shows nothing as new on a device's first visit, and stores the newest received time", async () => {
    mockGet.mockResolvedValue(page);
    renderMail();
    await screen.findByText("pat@acme.com");
    expect(row("mail:k2")).not.toHaveAttribute("data-new");
    await waitFor(() => expect(window.localStorage.getItem("lightbulb.mail.seen.org_1")).toBe(received.at));
    expect(row("mail:k2")).not.toHaveAttribute("data-new");
  });

  it("shows the address and how to use it when nothing has arrived", async () => {
    mockGet.mockResolvedValue({ address: ADDR, meetings: [], messages: [], next: null });
    renderMail();
    expect(await screen.findByText(/Mail to this address goes to your agents/)).toBeInTheDocument();
    expect(screen.getAllByText(ADDR).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("button", { name: "Copy address" }).length).toBeGreaterThan(0);
  });

  it("creates the address on a 409, saying so while it is set up", async () => {
    let addressReady = false;
    mockGet.mockImplementation((url: string) => {
      if (url === "/agent-mail/address") {
        return addressReady ? Promise.resolve({ address: ADDR }) : Promise.reject(new ApiError(409, {}));
      }
      return addressReady
        ? Promise.resolve({ address: ADDR, meetings: [], messages: [], next: null })
        : Promise.reject(new ApiError(409, { detail: { status: "no_address" } }));
    });
    const { unmount } = renderMail();
    expect(await screen.findByText("Your workspace address is being set up…")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledWith("/agent-mail/address");
    unmount();

    // Reading the address creates it; the inbox then answers with the empty mailbox.
    addressReady = true;
    let inboxCalls = 0;
    mockGet.mockImplementation((url: string) => {
      if (url === "/agent-mail/address") return Promise.resolve({ address: ADDR });
      inboxCalls += 1;
      return inboxCalls === 1
        ? Promise.reject(new ApiError(409, { detail: { status: "no_address" } }))
        : Promise.resolve({ address: ADDR, meetings: [], messages: [], next: null });
    });
    renderMail();
    expect(await screen.findByText(/Mail to this address goes to your agents/)).toBeInTheDocument();
    expect(screen.getAllByText(ADDR).length).toBeGreaterThan(0);
  });

  it("says when mail could not load, and Retry reads again", async () => {
    mockGet.mockRejectedValueOnce(new Error("500"));
    renderMail();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't load mail.");
    mockGet.mockResolvedValue(page);
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("pat@acme.com")).toBeInTheDocument();
  });

  it("keeps the rail badge empty on a device's first load, then counts mail past the mark", async () => {
    mockGet.mockResolvedValue(page);
    const wrapper = ({ children }: { children: React.ReactNode }) =>
      <SWRConfig value={{ provider: () => new Map() }}>{children}</SWRConfig>;
    const first = renderHook(() => useNewMailCount(), { wrapper });
    await waitFor(() => expect(window.localStorage.getItem("lightbulb.mail.seen.org_1")).toBe(received.at));
    expect(first.result.current).toBe(0);
    first.unmount();

    const mark = hoursAgo(5);
    window.localStorage.setItem("lightbulb.mail.seen.org_1", mark);
    const later = renderHook(() => useNewMailCount(), { wrapper });
    await waitFor(() => expect(later.result.current).toBe(1));
    expect(window.localStorage.getItem("lightbulb.mail.seen.org_1")).toBe(mark); // only the panel moves it
  });
});
