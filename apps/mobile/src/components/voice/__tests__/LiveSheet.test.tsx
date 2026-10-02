import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { LiveEvent } from "@lightbulb/shared";
import { Pressable, Text } from "react-native";

const mockPost = jest.fn();
const mockGet = jest.fn();
const mockRooms: { handlers: Record<string, (...a: unknown[]) => void>; rpc: jest.Mock; mic: jest.Mock; disconnect: jest.Mock }[] = [];
let mockParticipants: { identity: string; name?: string }[] = [];
let mockEmit: (e: LiveEvent) => void = () => {};

jest.mock("@clerk/clerk-expo", () => ({ useAuth: () => ({ getToken: async () => "t" }) }));
const mockLiveEvents = { onEvent: (h: (e: LiveEvent) => void) => { mockEmit = h; return () => {}; }, connectionRevision: 0 };
jest.mock("../../../providers/live-events", () => ({ useLiveEvents: () => mockLiveEvents }));
jest.mock("../../../lib/webrtc", () => ({ setupWebRTC: jest.fn() }));
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ post: (...a: unknown[]) => mockPost(...a), get: (...a: unknown[]) => mockGet(...a) }),
}));
jest.mock("livekit-client", () => ({
  Track: { Source: { Microphone: "microphone" } },
  RoomEvent: { DataReceived: "data", Disconnected: "disconnected" },
  Room: class {
    handlers: Record<string, (...a: unknown[]) => void> = {};
    rpc = jest.fn(async () => "");
    mic = jest.fn(async () => {});
    disconnect = jest.fn(async () => {});
    localParticipant = { setMicrophoneEnabled: this.mic, performRpc: this.rpc };
    constructor() { mockRooms.push(this); }
    on(ev: string, h: (...a: unknown[]) => void) { this.handlers[ev] = h; return this; }
    connect = async () => {};
  },
}));
jest.mock("@livekit/components-react", () => ({
  RoomContext: require("react").createContext(undefined),
  useIsSpeaking: (p?: { identity: string }) => p?.identity === "user-b",
  useParticipantAttributes: () => ({ attributes: { "lightbulb.status": "listening", "lightbulb.speaker": "Aden" } }),
  useParticipants: () => mockParticipants,
  useTracks: () => [],
}));

import { LiveProvider, useLive, useRoomLive } from "../../../providers/live";
import { LiveSheet } from "../LiveSheet";

function GoLive() {
  const live = useLive();
  return <Pressable accessibilityRole="button" onPress={() => void live.join("room-1", "#launch")}><Text>Go live</Text></Pressable>;
}
const ui = <LiveProvider><GoLive /><LiveSheet /></LiveProvider>;
const bytes = (s: string) => new Uint8Array([...s].map((c) => c.charCodeAt(0)));

describe("Live", () => {
  beforeEach(() => {
    mockRooms.length = 0;
    mockPost.mockReset().mockResolvedValue({ serverUrl: "wss://lk", roomName: "r", token: "tok" });
    mockParticipants = [{ identity: "agent-ceo", name: "Lightbulb" }, { identity: "user-a", name: "Aden" }, { identity: "user-b", name: "Leo" }];
  });

  it("joins with the mic on and shows Lightbulb, people with speaking rings, and the worker's status", async () => {
    await render(ui);
    await fireEvent.press(screen.getByText("Go live"));
    await waitFor(() => expect(screen.getByText("Listening to Aden")).toBeTruthy());
    expect(mockPost).toHaveBeenCalledWith("/huddles/room-1/join", {});
    expect(mockRooms[0].mic).toHaveBeenCalledWith(true);
    expect(screen.getByText("🔴 Live · #launch")).toBeTruthy();
    expect(screen.getByLabelText("Talk to Lightbulb")).toBeTruthy();
    expect(screen.getByLabelText("Leo, speaking")).toBeTruthy();
    expect(screen.getByLabelText("Aden")).toBeTruthy();
  });

  it("tapping Lightbulb sends one talk_start to the worker; there is no hold and no talk_end", async () => {
    await render(ui);
    await fireEvent.press(screen.getByText("Go live"));
    await fireEvent.press(await screen.findByLabelText("Talk to Lightbulb"));
    expect(mockRooms[0].rpc).toHaveBeenCalledTimes(1);
    expect(mockRooms[0].rpc).toHaveBeenCalledWith({ destinationIdentity: "agent-ceo", method: "talk_start", payload: "" });
    expect(screen.queryByLabelText(/Hold to talk/)).toBeNull();
  });

  it("a people-only room has no Lightbulb tile", async () => {
    mockPost.mockResolvedValue({ serverUrl: "wss://lk", roomName: "r", token: "tok", lightbulb: false });
    mockParticipants = [{ identity: "user-a", name: "Aden" }];
    await render(ui);
    await fireEvent.press(screen.getByText("Go live"));
    await screen.findByLabelText("Leave Live");
    expect(screen.queryByLabelText("Talk to Lightbulb")).toBeNull();
    expect(screen.queryByText("Lightbulb is joining…")).toBeNull();
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
    await render(ui);
    await fireEvent.press(screen.getByText("Go live"));
    await screen.findByLabelText("Talk to Lightbulb");
    const data = mockRooms[0].handlers.data;
    await act(async () => { data(bytes('{"code":"voice_limit","reason":"credits_low"}'), { identity: "user-a" }, 0, "huddle.error"); });
    expect(screen.queryByText("Add credits to keep talking to agents.")).toBeNull();
    await act(async () => { data(bytes('{"code":"voice_limit","reason":"credits_low"}'), { identity: "agent-ceo" }, 0, "huddle.error"); });
    expect(screen.getByText("Add credits to keep talking to agents.")).toBeTruthy();
  });

  it("mute turns the mic off; leave disconnects and closes", async () => {
    await render(ui);
    await fireEvent.press(screen.getByText("Go live"));
    await fireEvent.press(await screen.findByLabelText("Mute"));
    expect(mockRooms[0].mic).toHaveBeenLastCalledWith(false);
    await fireEvent.press(screen.getByLabelText("Leave Live"));
    expect(mockRooms[0].disconnect).toHaveBeenCalled();
    expect(screen.queryByLabelText("Talk to Lightbulb")).toBeNull();
  });

  it("a refused join says why", async () => {
    mockPost.mockRejectedValue({ status: 404, body: { code: "ROOM_ARCHIVED" } });
    await render(ui);
    await fireEvent.press(screen.getByText("Go live"));
    expect(await screen.findByText("This channel is archived.")).toBeTruthy();
  });
});

describe("useRoomLive", () => {
  function Probe() {
    const { userIds, available } = useRoomLive("room-1");
    return <Text>{`${available ? "on" : "off"}:${userIds.join(",")}`}</Text>;
  }

  it("seeds from the server, then follows room.huddle.changed", async () => {
    mockGet.mockResolvedValue({ userIds: ["u1"], available: true });
    await render(<Probe />);
    expect(await screen.findByText("on:u1")).toBeTruthy();
    expect(mockGet).toHaveBeenCalledWith("/huddles/room-1");
    await act(async () => { mockEmit({ type: "room.huddle.changed", payload: { roomId: "room-1", userIds: ["u1", "u2"] } } as unknown as LiveEvent); });
    expect(screen.getByText("on:u1,u2")).toBeTruthy();
    await act(async () => { mockEmit({ type: "room.huddle.changed", payload: { roomId: "other", userIds: [] } } as unknown as LiveEvent); });
    expect(screen.getByText("on:u1,u2")).toBeTruthy();
  });
});
