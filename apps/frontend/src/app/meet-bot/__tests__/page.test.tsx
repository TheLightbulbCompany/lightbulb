import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const lk = vi.hoisted(() => ({ publishData: vi.fn(), publishTrack: vi.fn(), attach: vi.fn(() => document.createElement("audio")), onConnect: undefined as (() => void) | undefined, handlers: {} as Record<string, (...a: unknown[]) => void>, remote: [] as { attributes: Record<string, string>; identity?: string; trackPublications?: Map<string, unknown> }[] }));
vi.mock("livekit-client", () => {
  class Room {
    remoteParticipants = { values: () => lk.remote.map((p) => ({ trackPublications: new Map(), ...p })) };
    localParticipant = { publishTrack: lk.publishTrack, publishData: lk.publishData };
    startAudio = vi.fn(async () => {});
    on(ev: string, fn: (...a: unknown[]) => void) { lk.handlers[ev] = fn; return this; }
    connect = vi.fn(async () => { lk.onConnect?.(); });
    disconnect = vi.fn();
  }
  return {
    Room, Track: { Kind: { Audio: "audio" }, Source: { Microphone: "microphone" } },
    RoomEvent: { TrackUnsubscribed: "trackUnsubscribed", Disconnected: "disconnected", TrackSubscribed: "trackSubscribed", ParticipantAttributesChanged: "attrs", ParticipantConnected: "joined", TrackPublished: "trackPublished" },
  };
});
import MeetBotPage from "../page";

const reply = (status: number, agents: unknown[] = [], extra: Record<string, unknown> = {}) => vi.fn(async () => ({ status, ok: status < 400, json: async () => ({ serverUrl: "wss://x", roomName: "r", token: "t", agents, ...extra }) }));
const track = { kind: "audio", attach: lk.attach, detach: vi.fn(() => [] as HTMLElement[]) };
const subscribe = (identity: string, trackName: string) => { lk.onConnect = () => lk.handlers.trackSubscribed?.(track, { kind: "audio", trackName }, { identity }); };

beforeEach(() => {
  vi.clearAllMocks(); lk.handlers = {}; lk.onConnect = undefined; lk.remote = [];
  window.location.hash = "#t=abc";
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: vi.fn(async () => ({ getAudioTracks: () => [{}] })) } });
});

const A = (id: string, status: string, extra: Record<string, string> = {}) =>
  Object.fromEntries([[`lightbulb.agent.${id}.status`, status], ...Object.entries(extra).map(([k, v]) => [`lightbulb.agent.${id}.${k}`, v])]);

it("names the speaking agent and attaches the subscribed audio track", async () => {
  lk.remote = [{ attributes: { ...A("Gauge", "speaking"), "lightbulb.speaking_agent": "Gauge", ...A("Forge", "listening") } }];
  global.fetch = reply(200, [{ id: "Gauge", name: "Gauge Prime" }]) as never;
  subscribe("agent-host", "agent-Gauge");
  render(<MeetBotPage />);
  await waitFor(() => expect(screen.getByText("Speaking")).toBeInTheDocument());
  expect(screen.getByText("Gauge Prime")).toBeInTheDocument();
  expect(screen.getByText("Forge")).toBeInTheDocument();
  expect(screen.getByText("Say an agent's name to ask")).toBeInTheDocument();
  expect(lk.publishData).not.toHaveBeenCalled();
  expect(lk.attach).toHaveBeenCalled();
});

it("shows a chip per state in order: speaking, hand up, working, idle; never says tap", async () => {
  lk.remote = [{ attributes: {
    ...A("idle1", "idle"), ...A("work", "working", { working: "drafting the post" }),
    ...A("hand", "has_news", { hand: "the Q3 numbers" }), ...A("bare", "has_news"), ...A("talk", "speaking"), "lightbulb.speaking_agent": "talk",
  } }];
  global.fetch = reply(200) as never;
  const { container } = render(<MeetBotPage />);
  await screen.findByText("Say an agent's name to ask");
  const chips = [...container.querySelectorAll("li")].map((li) => li.lastElementChild?.textContent) // the last child skips the avatar initials;
  expect(chips).toEqual([
    "talk · Speaking", "hand · Hand up: the Q3 numbers", "bare · Hand up: has something for you",
    "work · Working: drafting the post", "idle1",
  ]);
  expect(container.textContent).not.toMatch(/tap/i);
});

it("falls back to plain Working when the task text is absent", async () => {
  lk.remote = [{ attributes: A("w", "working") }];
  global.fetch = reply(200) as never;
  render(<MeetBotPage />);
  expect(await screen.findByText("Working")).toBeInTheDocument();
});

it("shows six chips and collapses the rest into +N more", async () => {
  lk.remote = [{ attributes: Object.assign({}, ...["a", "b", "c", "d", "e", "f", "g", "h"].map((id) => A(id, "idle"))) }];
  global.fetch = reply(200) as never;
  const { container } = render(<MeetBotPage />);
  expect(await screen.findByText("+2 more")).toBeInTheDocument();
  expect(container.querySelectorAll("li")).toHaveLength(7);
});

it("shows the expired-link state on 401", async () => {
  global.fetch = reply(401) as never;
  render(<MeetBotPage />);
  expect(await screen.findByText("This link has expired.")).toBeInTheDocument();
});

it("shows the expired state when the URL has no ticket", async () => {
  window.location.hash = "";
  global.fetch = reply(200) as never;
  render(<MeetBotPage />);
  expect(await screen.findByText("This link has expired.")).toBeInTheDocument();
  expect(global.fetch).not.toHaveBeenCalled();
});

it.each([
  ["free_minutes_used", "Today's free voice minutes are used up. The agents can speak again tomorrow."],
  ["credits_low", "Voice is paused: the workspace's credits are low."],
  ["plan_inactive", "Voice is off: the workspace's plan is not active."],
  ["something_new", "The agents cannot speak right now."],
])("says why the agents are silent: %s", async (reason, text) => {
  lk.remote = [{ attributes: { ...A("Forge", "listening"), "lightbulb.voice_limit": reason } }];
  global.fetch = reply(200) as never;
  render(<MeetBotPage />);
  const line = await screen.findByText(text);
  expect(line).toHaveAttribute("role", "status");
  expect(line.style.color).toBe("var(--alert)");
  expect(screen.getByText("Forge")).toBeInTheDocument();
  expect(screen.queryByText("Say an agent's name to ask")).not.toBeInTheDocument();
});

it("keeps the normal footer when the limit attribute is empty", async () => {
  lk.remote = [{ attributes: { ...A("Forge", "listening"), "lightbulb.voice_limit": "" } }];
  global.fetch = reply(200) as never;
  render(<MeetBotPage />);
  await screen.findByText("Say an agent's name to ask");
  expect(screen.queryByText(/cannot speak|voice/i)).not.toBeInTheDocument();
});

const published = async () => { render(<MeetBotPage />); await waitFor(() => expect(screen.queryByText("Connecting…")).not.toBeInTheDocument()); };

it("publishes the mixed audio when mixedAudio is true or absent", async () => {
  global.fetch = reply(200, [], { mixedAudio: true }) as never;
  await published();
  expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1);
  expect(lk.publishTrack).toHaveBeenCalledTimes(1);
});

it("publishes the mixed audio from an older backend that sends no mixedAudio", async () => {
  global.fetch = reply(200) as never;
  await published();
  expect(lk.publishTrack).toHaveBeenCalledTimes(1);
});

it("opens no microphone and publishes nothing when mixedAudio is false", async () => {
  global.fetch = reply(200, [], { mixedAudio: false }) as never;
  await published();
  expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
  expect(lk.publishTrack).not.toHaveBeenCalled();
});

it("never plays a Meet person's audio back into the Meet", async () => {
  global.fetch = reply(200) as never;
  subscribe("meet:room:p1", "agent-forge");
  await published();
  expect(lk.attach).not.toHaveBeenCalled();
});

it("plays a track named agent-*", async () => {
  global.fetch = reply(200) as never;
  subscribe("agent-host", "agent-forge");
  await published();
  expect(lk.attach).toHaveBeenCalledTimes(1);
});

it("ignores a track with another name from a non-meet participant", async () => {
  global.fetch = reply(200) as never;
  subscribe("someone", "microphone");
  await published();
  expect(lk.attach).not.toHaveBeenCalled();
});

it("subscribes only to agent-* publications, present at connect or published later", async () => {
  const setSubscribed = vi.fn();
  const pub = (trackName: string) => ({ kind: "audio", trackName, setSubscribed });
  lk.remote = [{ attributes: {}, identity: "agent-host", trackPublications: new Map([["a", pub("agent-forge")], ["b", pub("other")]]) }];
  global.fetch = reply(200) as never;
  await published();
  expect(setSubscribed).toHaveBeenCalledTimes(1);
  lk.handlers.trackPublished(pub("agent-gauge"), { identity: "agent-host" });
  lk.handlers.trackPublished(pub("microphone"), { identity: "meet:room:p1" });
  expect(setSubscribed).toHaveBeenCalledTimes(2);
});
