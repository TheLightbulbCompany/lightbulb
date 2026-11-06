import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const lk = vi.hoisted(() => ({
  admin: true,
  attrs: {} as Record<string, string>,
  participants: [] as { identity: string; name?: string; isLocal?: boolean }[],
  tracks: [] as unknown[],
  huddle: {
    agentNotice: null as null | { kind: string; agentId: string; at: number }, talkStart: vi.fn(),
    clearAgentNotice: vi.fn((at: number) => { if (lk.huddle.agentNotice?.at === at) lk.huddle.agentNotice = null; }), lightbulb: true, error: null, dismissError: vi.fn(), join: vi.fn(), leave: vi.fn(), roomId: "r1", roomName: "general", room: {} },
}));
vi.mock("livekit-client", () => ({ Track: { Source: { Microphone: "microphone" } } }));
vi.mock("@livekit/components-react", () => ({
  RoomContext: { Provider: ({ children }: { children: React.ReactNode }) => <>{children}</> },
  BarVisualizer: ({ state, track }: { state?: string; track?: { publication: { trackName: string } } }) =>
    <span data-testid="bars" data-state={state ?? "all"} data-track={track?.publication.trackName} />,
  StartAudio: () => null,
  TrackToggle: (props: React.ButtonHTMLAttributes<HTMLButtonElement>) => <button type="button" {...props} />,
  useParticipants: () => lk.participants, useIsSpeaking: () => false, useTracks: () => lk.tracks,
  useParticipantAttributes: () => ({ attributes: lk.attrs }),
}));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => lk.admin }));
vi.mock("@/lib/shellNav", () => ({ navigateShell: vi.fn() }));
vi.mock("../HuddleProvider", () => ({ CEO_IDENTITY: "agent-ceo", useHuddle: () => lk.huddle }));
import { navigateShell } from "@/lib/shellNav";
import { LiveStrip } from "../LiveStrip";

const agent = (id: string, displayName: string, listenMode = "active") => ({ kind: "agent", id, displayName, listenMode }) as never;
const trio = [agent("g", "Gauge"), agent("f", "Forge"), agent("n", "Nova")];
const status = (id: string, s: string) => ({ [`lightbulb.agent.${id}.status`]: s });
const view = (members: unknown[] = trio) => <LiveStrip roomId="r1" members={members as never} />;
const setMotion = (reduce: boolean) => {
  window.matchMedia = (() => ({ matches: reduce, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;
};

beforeEach(() => {
  setMotion(false); vi.clearAllMocks();
  lk.attrs = {}; lk.tracks = []; lk.huddle.lightbulb = true; lk.huddle.agentNotice = null; lk.huddle.error = null as never; lk.admin = true;
  lk.participants = [{ identity: "agent-ceo" }, { identity: "u1", name: "Ana", isLocal: true }];
});

it("an observe agent gets a tile like any other, each named for its state", () => {
  render(view([agent("g", "Gauge"), agent("o", "Ozzy", "observe"), agent("n", "Nova", "mentions")]));
  const group = screen.getByRole("group", { name: "Agents in Live" });
  expect(within(group).getAllByRole("button").map((b) => b.getAttribute("aria-label"))).toEqual(["Gauge, agent", "Ozzy, agent", "Nova, agent"]);
});

it("clicking an idle tile asks that agent in; the floor-holder's tile does nothing and its bars speak on its track", () => {
  lk.attrs = { "lightbulb.speaking_agent": "g", ...status("g", "speaking") };
  lk.tracks = [{ participant: { identity: "agent-ceo" }, publication: { trackName: "agent-g" } }];
  render(view());
  fireEvent.click(screen.getByRole("button", { name: /^Gauge/ }));
  expect(lk.huddle.talkStart).not.toHaveBeenCalled();
  expect(within(screen.getByRole("button", { name: /^Gauge/ })).getByTestId("bars")).toHaveAttribute("data-state", "speaking");
  expect(within(screen.getByRole("button", { name: /^Gauge/ })).getByTestId("bars")).toHaveAttribute("data-track", "agent-g");
  fireEvent.click(screen.getByRole("button", { name: /^Forge/ }));
  expect(lk.huddle.talkStart).toHaveBeenCalledWith("f");
});

it("a joining agent shows connecting bars and says it is joining", () => {
  lk.attrs = { "lightbulb.speaking_agent": "n", ...status("n", "joining") };
  render(view());
  const nova = screen.getByRole("button", { name: /^Nova/ });
  expect(nova).toHaveAttribute("title", "Nova is joining…");
  expect(within(nova).getByTestId("bars")).toHaveAttribute("data-state", "connecting");
});

it("six agents: the speaker, the joiner and one more, then +3", () => {
  const six = ["a", "b", "c", "d", "e", "f"].map((id) => agent(id, id.toUpperCase()));
  lk.attrs = { "lightbulb.speaking_agent": "e", ...status("e", "speaking"), ...status("f", "joining") };
  render(view(six));
  const names = within(screen.getByRole("group", { name: "Agents in Live" })).getAllByRole("button").map((b) => b.getAttribute("aria-label")?.split(",")[0]);
  expect(names.slice(0, 2)).toEqual(["E", "F"]);
  expect(names).toHaveLength(3);
  expect(screen.getByText("+3")).toBeInTheDocument();
});

it("the queue numbers every tile but the floor-holder", () => {
  lk.attrs = { "lightbulb.speaking_agent": "g", ...status("g", "speaking"), "lightbulb.queue": "g,f,n" };
  const { container } = render(view());
  expect([...container.querySelectorAll(".live-queue-badge")].map((b) => b.textContent)).toEqual(["2", "3"]);
  expect(within(screen.getByRole("button", { name: /^Gauge/ })).queryByText(/^\d$/)).toBeNull();
});

it("has news shows a hand pip and says it has something", () => {
  lk.attrs = status("g", "has_news");
  const { container } = render(view());
  expect(screen.getByRole("button", { name: /^Gauge/ }).getAttribute("aria-label")).toContain("has something");
  expect(container.querySelector(".live-pip svg")).not.toBeNull();
});

it("reduced motion: no track and static bars", () => {
  setMotion(true);
  lk.attrs = status("g", "listening");
  lk.tracks = [{ participant: { identity: "agent-ceo" }, publication: { trackName: "agent-g" } }];
  render(view());
  const bars = within(screen.getByRole("button", { name: /^Gauge/ })).getByTestId("bars");
  expect(bars).toHaveAttribute("data-state", "idle");
  expect(bars).not.toHaveAttribute("data-track");
});

it("renders without the worker, tiles inert", () => {
  lk.participants = [{ identity: "u1", name: "Ana", isLocal: true }];
  render(view());
  expect(screen.getByRole("button", { name: /^Gauge/ })).toBeDisabled();
  expect(screen.getByText("Agents are joining…")).toBeInTheDocument();
});

it("no listening agent in the room, no agent group", () => {
  lk.huddle.lightbulb = false;
  render(view());
  expect(screen.queryByRole("group", { name: "Agents in Live" })).toBeNull();
  expect(screen.queryByTestId("bars")).toBeNull();
});

it("narrow header: one tile, +2 and a people count", () => {
  let cb: ResizeObserverCallback = () => {};
  vi.stubGlobal("ResizeObserver", class { constructor(c: ResizeObserverCallback) { cb = c; } observe() {} disconnect() {} });
  try {
    lk.participants = [{ identity: "agent-ceo" }, { identity: "u1", name: "Ana", isLocal: true }, { identity: "u2", name: "Ben" }, { identity: "u3", name: "Cy" }];
    render(<div className="room-header">{view()}</div>);
    act(() => cb([{ contentRect: { width: 500 } } as ResizeObserverEntry], {} as ResizeObserver));
    expect(within(screen.getByRole("group", { name: "Agents in Live" })).getAllByRole("button")).toHaveLength(1);
    expect(screen.getByText("+2")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument(); // people, not the worker
  } finally {
    vi.unstubAllGlobals();
  }
});

it("the live region names a new speaker and ignores a working-only change", () => {
  const { container, rerender } = render(view());
  const region = () => container.querySelector('[aria-live="polite"]')!;
  lk.attrs = { "lightbulb.speaking_agent": "g", ...status("g", "speaking") };
  rerender(view());
  expect(region()).toHaveTextContent("Gauge is speaking");
  lk.attrs = { ...lk.attrs, ...status("f", "working"), "lightbulb.agent.f.working": "drafting" };
  rerender(view());
  expect(region()).toHaveTextContent("Gauge is speaking");
});

const openPopover = () => fireEvent.click(screen.getByRole("button", { name: /Details/ }));
const rows = () => screen.getAllByRole("list", { name: "Agents in Live" })[0];

it("popover says who is speaking and who is next; a row tap asks that agent in", () => {
  lk.attrs = { "lightbulb.speaking_agent": "g", ...status("g", "speaking"), "lightbulb.queue": "g,f" };
  render(view());
  openPopover();
  expect(document.querySelector(".live-happening")).toHaveTextContent("Gauge is speaking · Next: Forge");
  fireEvent.click(within(rows()).getByRole("button", { name: /^Forge/ }));
  expect(lk.huddle.talkStart).toHaveBeenCalledWith("f");
  fireEvent.click(within(rows()).getByRole("button", { name: /^Gauge, agent, speaking/ }));
  expect(lk.huddle.talkStart).toHaveBeenCalledTimes(1);
});

it("popover lists an observe agent as an askable row, and the listened-to speaker", () => {
  lk.attrs = { ...status("g", "listening"), "lightbulb.listening_to": "Aden" };
  render(view([...trio, agent("p", "Pulse", "observe")]));
  openPopover();
  expect(within(rows()).getByRole("button", { name: /^Pulse, agent/ })).toBeInTheDocument();
  expect(screen.queryByText(/can't be asked here/)).toBeNull();
  expect(within(rows()).getByText("Gauge — listening to Aden")).toBeInTheDocument();
});

it("hint: one agent, and a colliding name is 'hey Nova'", () => {
  render(view([agent("g", "Gauge")]));
  openPopover();
  expect(document.querySelector(".live-hint")).toHaveTextContent('Say "Gauge, …" or tap Gauge to talk.');
});

it("a colliding agent name reads 'hey Nova, …'", () => {
  render(view([agent("n", "Nova"), { kind: "human", id: "h", displayName: "Nova Smith", listenMode: null } as never]));
  openPopover();
  expect(document.querySelector(".live-hint")).toHaveTextContent('"hey Nova, …"');
});

it("a people-only room has no hint", () => {
  lk.huddle.lightbulb = false;
  render(view([]));
  openPopover();
  expect(document.querySelector(".live-hint")).toBeNull();
  expect(document.querySelector(".live-hint-inline")).toBeNull();
});

it("the inline hint shows at count 0 and counts the session, hides at 3, and survives a throwing storage", () => {
  const store = new Map<string, string>();
  const ls = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
  vi.stubGlobal("localStorage", ls);
  try {
    store.set("lightbulb.live.hintSessions", "0");
    lk.huddle.room = {};
    const a = render(view());
    expect(a.container.querySelector(".live-hint-inline")).not.toBeNull();
    expect(store.get("lightbulb.live.hintSessions")).toBe("1");
    a.unmount();
    render(view()).unmount(); // a room change remounts the strip inside the same Live: not another session
    expect(store.get("lightbulb.live.hintSessions")).toBe("1");
    store.set("lightbulb.live.hintSessions", "3");
    const b = render(view());
    expect(b.container.querySelector(".live-hint-inline")).toBeNull();
    b.unmount();
    vi.stubGlobal("localStorage", { getItem: () => { throw new Error("no"); }, setItem: () => { throw new Error("no"); } });
    const c = render(view());
    expect(c.container.querySelector(".live-hint-inline")).not.toBeNull();
  } finally {
    vi.unstubAllGlobals();
  }
});

it("ambiguous name: the tile is marked and a chip shows for 6 s", () => {
  vi.useFakeTimers();
  try {
    lk.huddle.agentNotice = { kind: "ambiguous_name", agentId: "n", at: 1 };
    const { rerender } = render(view());
    expect(screen.getByRole("button", { name: /^Nova/ })).toHaveAttribute("data-ambiguous", "true");
    expect(screen.getByRole("status")).toHaveTextContent('Meant Nova the agent? Say "hey Nova" or tap');
    act(() => { vi.advanceTimersByTime(6000); });
    expect(lk.huddle.clearAgentNotice).toHaveBeenCalledWith(1);
    rerender(view());
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByRole("button", { name: /^Nova/ })).not.toHaveAttribute("data-ambiguous");
  } finally { vi.useRealTimers(); }
});

it("voice failed: marked with the failed line until the agent leaves idle, or a tap", () => {
  lk.huddle.agentNotice = { kind: "voice_failed", agentId: "n", at: 2 };
  const { rerender } = render(view());
  expect(screen.getByRole("button", { name: /^Nova/ })).toHaveAttribute("data-failed", "true");
  expect(screen.getByRole("button", { name: /^Nova/ })).toHaveAttribute("title", "Nova couldn't join. Try again.");
  lk.attrs = status("n", "joining");
  rerender(view());
  expect(lk.huddle.clearAgentNotice).toHaveBeenCalledWith(2);
  lk.attrs = {};
  rerender(view());
  expect(screen.getByRole("button", { name: /^Nova/ })).not.toHaveAttribute("data-failed");
});

it("voice failed that lands while the status still reads joining waits for idle, then clears on the next join", () => {
  lk.attrs = status("n", "joining");
  lk.huddle.agentNotice = { kind: "voice_failed", agentId: "n", at: 4 };
  const { rerender } = render(view());
  expect(lk.huddle.clearAgentNotice).not.toHaveBeenCalled();
  lk.attrs = {};
  rerender(view());
  expect(screen.getByRole("button", { name: /^Nova/ })).toHaveAttribute("data-failed", "true");
  expect(lk.huddle.clearAgentNotice).not.toHaveBeenCalled();
  lk.attrs = status("n", "joining");
  rerender(view());
  expect(lk.huddle.clearAgentNotice).toHaveBeenCalledWith(4);
});

it("voice failed clears on a tap", () => {
  lk.huddle.agentNotice = { kind: "voice_failed", agentId: "n", at: 3 };
  const { rerender } = render(view());
  fireEvent.click(screen.getByRole("button", { name: /^Nova/ }));
  expect(lk.huddle.talkStart).toHaveBeenCalledWith("n");
  expect(lk.huddle.clearAgentNotice).toHaveBeenCalledWith(3);
  rerender(view());
  expect(screen.getByRole("button", { name: /^Nova/ })).not.toHaveAttribute("data-failed");
});

it("workspace_not_ready says the agent is waking, not the join", () => {
  const { rerender } = render(view());
  lk.huddle.error = { code: "workspace_not_ready" } as never;
  rerender(view());
  expect(screen.getByRole("alert")).toHaveTextContent("Gauge is waking up");
});

it.each([
  [true, "free_minutes_used", "Plans"], [true, "plan_inactive", "Plans"], [true, "credits_low", "Add credits"],
  [false, "free_minutes_used", null], [false, "credits_low", null],
])("voice limit (admin %s, %s): taps shut, CTA %s", (admin, reason, cta) => {
  lk.admin = admin;
  const { rerender } = render(view());
  lk.huddle.error = { code: "voice_limit", reason } as never;
  rerender(view());
  const g = screen.getAllByRole("button", { name: /^Gauge/ })[0];
  expect(g).toHaveAttribute("aria-disabled", "true");
  fireEvent.click(g);
  expect(lk.huddle.talkStart).not.toHaveBeenCalled();
  const alert = screen.getByRole("alert");
  if (!cta) { expect(within(alert).queryByRole("button", { name: /Add credits/ })).toBeNull(); expect(within(alert).queryByRole("link")).toBeNull(); return; }
  fireEvent.click(within(alert).getByText(cta));
  if (cta === "Add credits") expect(navigateShell).toHaveBeenCalledWith("/chat?panel=llm");
  else expect(within(alert).getByText("Plans")).toHaveAttribute("href", "/settings?panel=billing");
});

it("voice_busy shuts the taps", () => {
  const { rerender } = render(view());
  lk.huddle.error = { code: "voice_busy" } as never;
  rerender(view());
  fireEvent.click(screen.getAllByRole("button", { name: /^Gauge/ })[0]);
  expect(lk.huddle.talkStart).not.toHaveBeenCalled();
});

it("45 s without the worker shows agents-absent with Retry, which re-joins", () => {
  vi.useFakeTimers();
  try {
    lk.participants = [{ identity: "u1", name: "Ana", isLocal: true }];
    render(view());
    act(() => { vi.advanceTimersByTime(45_000); });
    expect(screen.queryByText("Agents are joining…")).toBeNull();
    fireEvent.click(within(screen.getByRole("alert")).getByText("Retry"));
    expect(lk.huddle.join).toHaveBeenCalledWith("r1", "general");
  } finally { vi.useRealTimers(); }
});

it("an agent leaving is announced", () => {
  const { container, rerender } = render(view());
  rerender(view([agent("g", "Gauge"), agent("f", "Forge")]));
  expect(container.querySelector('[aria-live="polite"]')).toHaveTextContent("Nova left");
});
