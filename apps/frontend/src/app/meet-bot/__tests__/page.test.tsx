import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

const lk = vi.hoisted(() => ({ publishData: vi.fn(), attach: vi.fn(), handlers: {} as Record<string, (...a: unknown[]) => void>, remote: [] as { attributes: Record<string, string> }[] }));
vi.mock("livekit-client", () => {
  class Room {
    remoteParticipants = { values: () => lk.remote };
    localParticipant = { publishTrack: vi.fn(), publishData: lk.publishData };
    startAudio = vi.fn(async () => {});
    on(ev: string, fn: (...a: unknown[]) => void) { lk.handlers[ev] = fn; return this; }
    connect = vi.fn(async () => { lk.handlers.trackSubscribed?.({ kind: "audio", attach: lk.attach }); });
    disconnect = vi.fn();
  }
  return {
    Room, Track: { Kind: { Audio: "audio" }, Source: { Microphone: "microphone" } },
    RoomEvent: { TrackUnsubscribed: "trackUnsubscribed", Disconnected: "disconnected", TrackSubscribed: "trackSubscribed", ParticipantAttributesChanged: "attrs", ParticipantConnected: "joined" },
  };
});
import MeetBotPage from "../page";

const reply = (status: number, agents: unknown[] = []) => vi.fn(async () => ({ status, ok: status < 400, json: async () => ({ serverUrl: "wss://x", roomName: "r", token: "t", agents }) }));

beforeEach(() => {
  vi.clearAllMocks(); lk.handlers = {};
  window.location.hash = "#t=abc";
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: vi.fn(async () => ({ getAudioTracks: () => [{}] })) } });
});

it("names the speaking agent and attaches the subscribed audio track", async () => {
  lk.remote = [{ attributes: { "lightbulb.agent.Gauge.status": "speaking", "lightbulb.speaking_agent": "Gauge", "lightbulb.agent.Forge.status": "listening" } }];
  global.fetch = reply(200, [{ id: "Gauge", name: "Gauge Prime" }]) as never;
  render(<MeetBotPage />);
  await waitFor(() => expect(screen.getByText("is speaking")).toBeInTheDocument());
  expect(screen.getAllByText("Gauge Prime").length).toBeGreaterThan(0);
  expect(lk.publishData).not.toHaveBeenCalled();
  expect(screen.getByText(/Listening/)).toHaveTextContent("Forge");
  expect(lk.attach).toHaveBeenCalled();
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
