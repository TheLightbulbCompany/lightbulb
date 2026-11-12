import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { LiveEvent } from "@lightbulb/shared";
import { AccessibilityInfo, Pressable, Text } from "react-native";
import { useTrackVolume } from "@livekit/react-native";

const mockPost = jest.fn();
const mockGet = jest.fn();
const mockPatch = jest.fn();
const mockPush = jest.fn();
const mockRooms: { handlers: Record<string, (...a: unknown[]) => void>; rpc: jest.Mock; mic: jest.Mock; disconnect: jest.Mock }[] = [];
let mockParticipants: { identity: string; name?: string }[] = [];
let mockTracks: unknown[] = [];
let mockEmit: (e: LiveEvent) => void = () => {};

jest.mock("@clerk/expo", () => ({ useAuth: () => ({ getToken: async () => "t" }) }));
const mockLiveEvents = { onEvent: (h: (e: LiveEvent) => void) => { mockEmit = h; return () => {}; }, connectionRevision: 0 };
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => mockLiveEvents }));
jest.mock("expo-router", () => ({ router: { push: (...a: unknown[]) => mockPush(...a), navigate: (...a: unknown[]) => mockPush(...a) } }));
jest.mock("../../../lib/webrtc", () => ({ setupWebRTC: jest.fn() }));
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ post: (...a: unknown[]) => mockPost(...a), get: (...a: unknown[]) => mockGet(...a), patch: (...a: unknown[]) => mockPatch(...a) }),
}));
jest.mock("livekit-client", () => ({
  Track: { Source: { Microphone: "microphone" } },
  RoomEvent: { DataReceived: "data", Disconnected: "disconnected", ParticipantConnected: "pconn", ParticipantDisconnected: "pdisc", ParticipantAttributesChanged: "pattr" },
  Room: class {
    handlers: Record<string, (...a: unknown[]) => void> = {};
    rpc = jest.fn(async () => "");
    mic = jest.fn(async () => {});
    disconnect = jest.fn(async () => {});
    localParticipant = { identity: "user-a", setMicrophoneEnabled: this.mic, performRpc: this.rpc };
    constructor() { mockRooms.push(this); }
    on(ev: string, h: (...a: unknown[]) => void) { this.handlers[ev] = h; return this; }
    connect = async () => {};
  },
}));
jest.mock("@livekit/components-react", () => ({
  RoomContext: require("react").createContext(undefined),
  useIsSpeaking: (p?: { identity: string }) => p?.identity === "user-b",
  useParticipants: () => mockParticipants,
  useTracks: () => mockTracks,
}));
jest.mock("@livekit/react-native", () => ({ registerGlobals: jest.fn(), setupIOSAudioManagement: jest.fn(), useTrackVolume: jest.fn(() => 0) }));

import { liveErrorText } from "@lightbulb/shared";
import { LiveProvider, useLive } from "../../../providers/live";
import { LiveSheet } from "../LiveSheet";

function GoLive() {
  const live = useLive();
  return (
    <>
      <Pressable accessibilityRole="button" onPress={() => void live.join("room-1", "#launch")}><Text>Go live</Text></Pressable>
      <Pressable accessibilityRole="button" onPress={() => live.setOpen(true)}><Text>Show Live</Text></Pressable>
    </>
  );
}
const ui = <LiveProvider><GoLive /><LiveSheet /></LiveProvider>;
const bytes = (s: string) => new TextEncoder().encode(s);
const agent = (id: string, displayName: string, listenMode = "active") => ({ kind: "agent", id, displayName, listenMode });
const members = [agent("gauge", "Gauge"), agent("ceo", "CEO"), agent("pulse", "Pulse", "observe"), { kind: "human", id: "u-a", displayName: "Aden", listenMode: null }];
const K = (id: string, f: string) => `lightbulb.agent.${id}.${f}`;
const setAttrs = async (attributes: Record<string, string>) => {
  await act(async () => { mockRooms[0].handlers.pattr({}, { identity: "agent-ceo", attributes }); });
};
const connectWorker = async () => { await act(async () => { mockRooms[0].handlers.pconn({ identity: "agent-ceo", attributes: {} }); }); };
async function goLive() {
  await render(ui);
  await fireEvent.press(screen.getByText("Go live"));
  await screen.findByLabelText("Leave Live");
  await waitFor(() => expect(screen.getByText("Agents")).toBeTruthy());
  await connectWorker();
}

describe("Live", () => {
  let announce: jest.SpyInstance;
  beforeEach(() => {
    mockRooms.length = 0;
    mockTracks = [];
    mockPost.mockReset().mockResolvedValue({ serverUrl: "wss://lk", roomName: "r", token: "tok" });
    mockGet.mockReset().mockResolvedValue({ members });
    mockPatch.mockReset().mockResolvedValue({});
    mockPush.mockClear();
    mockParticipants = [{ identity: "agent-ceo", name: "Lightbulb" }, { identity: "user-a", name: "Aden" }, { identity: "user-b", name: "Leo" }];
    announce = jest.spyOn(AccessibilityInfo, "announceForAccessibility").mockImplementation(() => {});
    announce.mockClear();
    jest.spyOn(AccessibilityInfo, "isReduceMotionEnabled").mockResolvedValue(false);
    (useTrackVolume as jest.Mock).mockClear();
  });

  it("joins with the mic on; tiles for every agent (observe Pulse too), people with speaking rings", async () => {
    await goLive();
    expect(mockPost).toHaveBeenCalledWith("/huddles/room-1/join", {});
    expect(mockRooms[0].mic).toHaveBeenCalledWith(true);
    expect(screen.getByText("🔴 Live · #launch")).toBeTruthy();
    expect(screen.getAllByLabelText(/^Gauge, agent/).length).toBeGreaterThan(0); // the stage and the row
    expect(screen.getAllByLabelText(/^CEO, agent/)).toHaveLength(1);
    expect(screen.getAllByLabelText(/^Pulse, agent/)).toHaveLength(1); // listen modes are chat-only
    expect(screen.queryByText(/can't be asked here/)).toBeNull();
    expect(screen.getByLabelText("Leo, speaking")).toBeTruthy();
    expect(screen.getByLabelText("Aden")).toBeTruthy();
    expect(screen.getByText("Tap someone or say their name")).toBeTruthy();
    expect(screen.getByText(/Say an agent's name/)).toBeTruthy();
  });

  it("a tap sends talk_start with the id; the floor-holder's tile does nothing; no hold, no talk_end", async () => {
    await goLive();
    await setAttrs({ "lightbulb.speaking_agent": "gauge", [K("gauge", "status")]: "speaking" });
    await fireEvent.press(screen.getByLabelText(/^CEO, agent/));
    expect(mockRooms[0].rpc).toHaveBeenCalledTimes(1);
    expect(mockRooms[0].rpc).toHaveBeenCalledWith({ destinationIdentity: "agent-ceo", method: "talk_start", payload: '{"agentId":"ceo"}' });
    await fireEvent.press(screen.getAllByLabelText(/^Gauge, agent/)[0]);
    expect(mockRooms[0].rpc).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText(/Hold to talk/)).toBeNull();
  });

  it("the floor-holder is on the stage; the next agent is badged and labelled; the line says what's happening", async () => {
    await goLive();
    await setAttrs({ "lightbulb.speaking_agent": "gauge", [K("gauge", "status")]: "speaking", "lightbulb.queue": "gauge,ceo" });
    expect(screen.getByText("Speaking")).toBeTruthy();
    expect(screen.getByText("Gauge is speaking · Next: CEO")).toBeTruthy();
    expect(screen.getByLabelText(/^CEO, agent, next/i)).toBeTruthy();
    expect(screen.getByText("2")).toBeTruthy();
    expect(screen.getByTestId("level-ring")).toBeTruthy();
  });

  it("has_news shows the raised hand", async () => {
    await goLive();
    await setAttrs({ [K("ceo", "status")]: "has_news" });
    expect(screen.getByLabelText(/^CEO, agent, has something/i)).toBeTruthy();
  });

  it("no ring unless speaking, and none under Reduce Motion", async () => {
    (AccessibilityInfo.isReduceMotionEnabled as jest.Mock).mockResolvedValue(true);
    await goLive();
    await setAttrs({ "lightbulb.speaking_agent": "gauge", [K("gauge", "status")]: "speaking" });
    expect(screen.queryByTestId("level-ring")).toBeNull();
    (AccessibilityInfo.isReduceMotionEnabled as jest.Mock).mockResolvedValue(false);
  });

  it("a listening floor-holder has no ring", async () => {
    await goLive();
    await setAttrs({ "lightbulb.speaking_agent": "gauge", [K("gauge", "status")]: "listening" });
    expect(screen.queryByTestId("level-ring")).toBeNull();
  });

  it("with two agent tracks the level comes from the floor-holder's", async () => {
    mockTracks = [{ publication: { trackName: "agent-ceo" } }, { publication: { trackName: "agent-gauge" } }];
    await goLive();
    await setAttrs({ "lightbulb.speaking_agent": "gauge", [K("gauge", "status")]: "speaking" });
    expect(useTrackVolume).toHaveBeenLastCalledWith(mockTracks[1]);
  });

  it("announces a floor change once", async () => {
    await goLive();
    await setAttrs({ "lightbulb.speaking_agent": "gauge", [K("gauge", "status")]: "speaking" });
    await setAttrs({ "lightbulb.speaking_agent": "gauge", [K("gauge", "status")]: "speaking", [K("gauge", "working")]: "x" });
    expect(announce.mock.calls.filter(([m]) => /Gauge is speaking/.test(m))).toHaveLength(1);
  });

  it("workspace_not_ready says the agent is waking, not a generic failure", async () => {
    await goLive();
    await act(async () => { mockRooms[0].handlers.data(bytes('{"code":"workspace_not_ready"}'), { identity: "agent-ceo" }, 0, "huddle.error"); });
    expect(screen.getByText(/is waking up — this can take a minute/)).toBeTruthy();
  });

  it("voice_limit disables the tiles", async () => {
    await goLive();
    await act(async () => { mockRooms[0].handlers.data(bytes('{"code":"voice_limit","reason":"credits_low"}'), { identity: "agent-ceo" }, 0, "huddle.error"); });
    expect(screen.getByText(liveErrorText({ code: "voice_limit", reason: "credits_low" }))).toBeTruthy();
    await fireEvent.press(screen.getByLabelText(/^CEO, agent/));
    expect(mockRooms[0].rpc).not.toHaveBeenCalled();
  });

  it("agents absent for 45 s: says so, with Retry", async () => {
    jest.useFakeTimers();
    try {
      await render(ui);
      await fireEvent.press(screen.getByText("Go live"));
      await screen.findByLabelText("Leave Live");
      await act(async () => { jest.advanceTimersByTime(46_000); });
      expect(screen.getByText(liveErrorText({ code: "agents_absent" }))).toBeTruthy();
      await fireEvent.press(screen.getByText("Retry"));
      expect(mockPost).toHaveBeenLastCalledWith("/huddles/room-1/join", {});
    } finally { jest.useRealTimers(); }
  });

  it("a people-only room has no agents and no hint", async () => {
    mockPost.mockResolvedValue({ serverUrl: "wss://lk", roomName: "r", token: "tok", lightbulb: false });
    mockParticipants = [{ identity: "user-a", name: "Aden" }];
    await render(ui);
    await fireEvent.press(screen.getByText("Go live"));
    await screen.findByLabelText("Leave Live");
    expect(screen.queryByText("Agents")).toBeNull();
    expect(screen.queryByText(/Say an agent's name|tap Gauge/)).toBeNull();
    expect(screen.queryByText("Agents are joining…")).toBeNull();
  });

  it("idle_ended from the worker ends Live and says why", async () => {
    await render(ui);
    await fireEvent.press(screen.getByText("Go live"));
    await screen.findByLabelText("Leave Live");
    await act(async () => { mockRooms[0].handlers.data(bytes('{"code":"idle_ended"}'), { identity: "agent-ceo" }, 0, "huddle.error"); });
    expect(mockRooms[0].disconnect).toHaveBeenCalled();
    expect(screen.getByText("Live ended — no one was talking.")).toBeTruthy();
  });

  it("shows a huddle.error only from agent-ceo, in plain words", async () => {
    const text = liveErrorText({ code: "voice_limit", reason: "credits_low" });
    await goLive();
    const data = mockRooms[0].handlers.data;
    await act(async () => { data(bytes('{"code":"voice_limit","reason":"credits_low"}'), { identity: "user-a" }, 0, "huddle.error"); });
    expect(screen.queryByText(text)).toBeNull();
    await act(async () => { data(bytes('{"code":"voice_limit","reason":"credits_low"}'), { identity: "agent-ceo" }, 0, "huddle.error"); });
    expect(screen.getByText(text)).toBeTruthy();
  });

  it("mute turns the mic off; leave disconnects and closes", async () => {
    await goLive();
    await fireEvent.press(screen.getByLabelText("Mute"));
    expect(mockRooms[0].mic).toHaveBeenLastCalledWith(false);
    await fireEvent.press(screen.getByLabelText("Leave Live"));
    expect(mockRooms[0].disconnect).toHaveBeenCalled();
    expect(screen.queryByLabelText(/^Gauge, agent/)).toBeNull();
  });

  it("a refused join says why", async () => {
    mockPost.mockRejectedValue({ status: 404, body: { code: "ROOM_ARCHIVED" } });
    await render(ui);
    await fireEvent.press(screen.getByText("Go live"));
    expect(await screen.findByText("This channel is archived.")).toBeTruthy();
  });
  const notice = async (n: object) => { await act(async () => { mockRooms[0].handlers.data(bytes(JSON.stringify(n)), { identity: "agent-ceo" }, 0, "live.notice"); }); };
  const renamed = { kind: "renamed", agentId: "ceo", from: "CEO", to: "Nova", by: "user-a", collides: false };

  it("a rename banner: Undo only for the asker, calls the rename route back, and goes away", async () => {
    await goLive();
    await notice(renamed);
    expect(screen.getByText("CEO is now Nova")).toBeTruthy();
    await fireEvent.press(screen.getByText("Undo"));
    expect(mockPatch).toHaveBeenCalledWith("/teams/agents/ceo", { name: "CEO" });
    expect(screen.queryByText("CEO is now Nova")).toBeNull();
  });

  it("a rename by someone else has no Undo; a colliding name says how to reach the agent; it expires after 10 s", async () => {
    jest.useFakeTimers();
    try {
      await goLive();
      await notice({ ...renamed, by: "user-b", collides: true });
      expect(screen.getByText("CEO is now Nova. Say 'hey Nova' in Live.")).toBeTruthy();
      expect(screen.queryByText("Undo")).toBeNull();
      await act(async () => { jest.advanceTimersByTime(9_000); });
      expect(screen.getByText(/CEO is now Nova/)).toBeTruthy();
      await act(async () => { jest.advanceTimersByTime(1_500); });
      expect(screen.queryByText(/CEO is now Nova/)).toBeNull();
    } finally { jest.useRealTimers(); }
  });

  it("a voice-note banner: Edit opens the Agent tab and hides the sheet", async () => {
    await goLive();
    await notice({ kind: "voice_note_saved", agentId: "gauge", name: "Gauge", text: "be more blunt", by: "user-a" });
    expect(screen.getByText("Gauge will remember: be more blunt")).toBeTruthy();
    await fireEvent.press(screen.getByText("Edit"));
    expect(mockPush).toHaveBeenCalledWith("/(app)/(tabs)/teams");
    expect(screen.queryByText("Gauge will remember: be more blunt")).toBeNull();
  });

  it("a notice from a person is ignored", async () => {
    await goLive();
    await act(async () => { mockRooms[0].handlers.data(bytes(JSON.stringify(renamed)), { identity: "user-b" }, 0, "live.notice"); });
    expect(screen.queryByText("CEO is now Nova")).toBeNull();
  });

  it("ambiguous_name: a 6 s chip naming the agent", async () => {
    jest.useFakeTimers();
    try {
      await goLive();
      await notice({ kind: "ambiguous_name", agentId: "gauge" });
      expect(screen.getByText(/Meant Gauge the agent\? Say "hey Gauge" or tap/)).toBeTruthy();
      await act(async () => { jest.advanceTimersByTime(6_100); });
      expect(screen.queryByText(/Meant Gauge the agent/)).toBeNull();
    } finally { jest.useRealTimers(); }
  });

  it("an agent notice is consumed once: hiding and reopening the sheet does not bring the chip back", async () => {
    await goLive();
    await notice({ kind: "ambiguous_name", agentId: "gauge" });
    expect(screen.getByText(/Meant Gauge the agent/)).toBeTruthy();
    await fireEvent.press(screen.getByText("Hide"));
    await fireEvent.press(screen.getByText("Show Live"));
    await screen.findByLabelText("Leave Live");
    expect(screen.queryByText(/Meant Gauge the agent/)).toBeNull();
  });

  it("a rename's 10 s runs from arrival, not from when a banner mounts", async () => {
    jest.useFakeTimers();
    try {
      await goLive();
      await fireEvent.press(screen.getByText("Hide"));
      await notice({ ...renamed, by: "user-b" });
      await act(async () => { jest.advanceTimersByTime(11_000); });
      await fireEvent.press(screen.getByText("Show Live"));
      await act(async () => { jest.advanceTimersByTime(0); });
      expect(screen.queryByText(/CEO is now Nova/)).toBeNull();
    } finally { jest.useRealTimers(); }
  });

  it("voice_failed marks the tile until its status changes; a tap retries", async () => {
    await goLive();
    await notice({ kind: "voice_failed", agentId: "gauge" });
    expect(screen.getByText("Gauge couldn't join. Try again.")).toBeTruthy();
    await fireEvent.press(screen.getAllByLabelText(/^Gauge, agent.*couldn't join/)[0]);
    expect(mockRooms[0].rpc).toHaveBeenCalledWith({ destinationIdentity: "agent-ceo", method: "talk_start", payload: JSON.stringify({ agentId: "gauge" }) });
    await setAttrs({ [K("gauge", "status")]: "joining", "lightbulb.speaking_agent": "gauge" });
    expect(screen.queryByText("Gauge couldn't join. Try again.")).toBeNull();
  });
});

