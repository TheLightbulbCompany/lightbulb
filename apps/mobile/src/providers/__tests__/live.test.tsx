import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { LiveEvent } from "@lightbulb/shared";
import { Pressable, Text } from "react-native";

const mockPost = jest.fn();
const mockGet = jest.fn();
const mockRooms: { handlers: Record<string, (...a: unknown[]) => void>; rpc: jest.Mock; disconnect: jest.Mock }[] = [];
let mockEmit: (e: LiveEvent) => void = () => {};

jest.mock("@clerk/expo", () => ({ useAuth: () => ({ getToken: async () => "t" }) }));
jest.mock("../live-events", () => ({
  useLiveEvents: () => ({ onEvent: (h: (e: LiveEvent) => void) => { mockEmit = h; return () => {}; }, connectionRevision: 0 }),
}));
jest.mock("../../lib/webrtc", () => ({ setupWebRTC: jest.fn() }));
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ post: (...a: unknown[]) => mockPost(...a), get: (...a: unknown[]) => mockGet(...a) }),
}));
jest.mock("livekit-client", () => ({
  RoomEvent: { DataReceived: "data", Disconnected: "disconnected", ParticipantConnected: "pconn", ParticipantDisconnected: "pdisc", ParticipantAttributesChanged: "pattr" },
  Room: class {
    handlers: Record<string, (...a: unknown[]) => void> = {};
    rpc = jest.fn(async () => "");
    disconnect = jest.fn(async () => {});
    localParticipant = { identity: "user-me", setMicrophoneEnabled: async () => {}, performRpc: this.rpc };
    constructor() { mockRooms.push(this); }
    on(ev: string, h: (...a: unknown[]) => void) { this.handlers[ev] = h; return this; }
    connect = async () => {};
  },
}));

// eslint-disable-next-line import/first
import { LiveProvider, useLive } from "../live";

let live: ReturnType<typeof useLive>;
function Probe() {
  live = useLive();
  return <Pressable accessibilityRole="button" onPress={() => void live.join("room-1", "launch")}><Text>Go live</Text></Pressable>;
}
const bytes = (s: string) => new TextEncoder().encode(s);
const members = [{ kind: "agent", id: "gauge", displayName: "Gauge", listenMode: "active" }];
const rooms = () => mockRooms[0];
const roomGets = () => mockGet.mock.calls.filter(([p]) => String(p).includes("/messaging/rooms/room-1")).length;

async function joined() {
  await render(<LiveProvider><Probe /></LiveProvider>);
  await fireEvent.press(screen.getByText("Go live"));
  await waitFor(() => expect(live.room).not.toBeNull());
}

describe("LiveProvider per-agent state", () => {
  beforeEach(() => {
    mockRooms.length = 0;
    mockPost.mockReset().mockResolvedValue({ serverUrl: "wss://lk", token: "tok" });
    mockGet.mockReset().mockResolvedValue({ members });
  });

  it("talkStart sends the C6 payloads", async () => {
    await joined();
    live.talkStart();
    live.talkStart("gauge");
    expect(rooms().rpc).toHaveBeenNthCalledWith(1, { destinationIdentity: "agent-ceo", method: "talk_start", payload: "" });
    expect(rooms().rpc).toHaveBeenNthCalledWith(2, { destinationIdentity: "agent-ceo", method: "talk_start", payload: '{"agentId":"gauge"}' });
  });

  it("reads only the worker's attributes; lastSpeaker survives an empty floor", async () => {
    await joined();
    expect(live.localIdentity).toBe("user-me");
    const attr = rooms().handlers.pattr;
    await act(async () => { attr({}, { identity: "user-a", attributes: { "lightbulb.speaking_agent": "evil" } }); });
    expect(live.workerAttrs).toEqual({});
    await act(async () => { attr({}, { identity: "agent-ceo", attributes: { "lightbulb.speaking_agent": "gauge" } }); });
    expect(live.workerAttrs["lightbulb.speaking_agent"]).toBe("gauge");
    await act(async () => { attr({}, { identity: "agent-ceo", attributes: { "lightbulb.speaking_agent": "" } }); });
    expect(live.lastSpeaker).toBe("gauge");
    await act(async () => { rooms().handlers.pconn({ identity: "agent-ceo", attributes: {} }); });
    expect(live.workerPresent).toBe(true);
  });

  it("notices: ignored from a user, renamed from the worker sets notice and refetches, malformed is null, UTF-8 decodes", async () => {
    await joined();
    await waitFor(() => expect(live.roster).toHaveLength(1));
    const data = rooms().handlers.data;
    const renamed = '{"kind":"renamed","agentId":"gauge","from":"CEO","to":"Zoë","by":"u","collides":false}';
    await act(async () => { data(bytes(renamed), { identity: "user-a" }, 0, "live.notice"); });
    expect(live.notice).toBeNull();
    const before = roomGets();
    await act(async () => { data(bytes("not json"), { identity: "agent-ceo" }, 0, "live.notice"); });
    expect(live.notice).toBeNull();
    await act(async () => { data(bytes(renamed), { identity: "agent-ceo" }, 0, "live.notice"); });
    expect(live.notice).toMatchObject({ kind: "renamed", to: "Zoë" });
    expect(roomGets()).toBe(before + 1);
    await act(async () => { live.dismissNotice(); });
    expect(live.notice).toBeNull();
  });

  it("an agent notice takes its own slot and never displaces a rename banner", async () => {
    await joined();
    const data = rooms().handlers.data;
    await act(async () => { data(bytes('{"kind":"renamed","agentId":"gauge","from":"CEO","to":"Zoe","by":"u","collides":false}'), { identity: "agent-ceo" }, 0, "live.notice"); });
    await act(async () => { data(bytes('{"kind":"ambiguous_name","agentId":"gauge"}'), { identity: "agent-ceo" }, 0, "live.notice"); });
    expect(live.notice).toMatchObject({ kind: "renamed" });
    expect(live.agentNotice).toMatchObject({ kind: "ambiguous_name", agentId: "gauge" });
    await act(async () => { live.dismissAgentNotice(); });
    expect(live.agentNotice).toBeNull();
  });

  it("room.member.updated refetches for this room only", async () => {
    await joined();
    await waitFor(() => expect(live.roster).toHaveLength(1));
    const n = roomGets();
    const ev = (roomId: string) => ({ type: "room.member.updated", payload: { roomId } }) as unknown as LiveEvent;
    await act(async () => { mockEmit(ev("other")); });
    expect(roomGets()).toBe(n);
    await act(async () => { mockEmit(ev("room-1")); });
    expect(roomGets()).toBe(n + 1);
  });

  it("leave clears state", async () => {
    await joined();
    await act(async () => { rooms().handlers.pattr({}, { identity: "agent-ceo", attributes: { "lightbulb.speaking_agent": "gauge" } }); });
    await waitFor(() => expect(live.roster).toHaveLength(1));
    await act(async () => { live.leave(); });
    expect(live).toMatchObject({ workerAttrs: {}, workerPresent: false, roster: [], lastSpeaker: null, notice: null, agentNotice: null, localIdentity: null });
  });
});
