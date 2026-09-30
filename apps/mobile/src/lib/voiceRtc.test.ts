import { NativeModules } from "react-native";
import { hasNativeVoice } from "./voiceRtc";

describe("hasNativeVoice", () => {
  it("is false on a binary without the WebRTC module (old build + new OTA)", () => {
    delete (NativeModules as Record<string, unknown>).WebRTCModule;
    expect(hasNativeVoice()).toBe(false);
  });
  it("is true when the module is linked", () => {
    (NativeModules as Record<string, unknown>).WebRTCModule = {};
    expect(hasNativeVoice()).toBe(true);
  });
});

describe("nativeVoiceRtc.close", () => {
  it("does not throw and still stops InCallManager when pc.close throws", async () => {
    const stop = jest.fn();
    const start = jest.fn();
    jest.resetModules();
    jest.doMock("react-native-webrtc", () => ({
      mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop: jest.fn() }] }) },
      RTCPeerConnection: class {
        iceGatheringState = "complete";
        localDescription = { sdp: "x" };
        addTrack() {}
        addEventListener() {}
        createDataChannel() { return { readyState: "open", addEventListener() {}, close() {} }; }
        createOffer = async () => ({});
        setLocalDescription = async () => {};
        close() { throw new Error("boom"); }
      },
    }));
    jest.doMock("react-native-incall-manager", () => ({ default: { start, stop } }));
    const { nativeVoiceRtc } = require("./voiceRtc");
    const peer = await nativeVoiceRtc.open();
    expect(() => peer.close()).not.toThrow();
    expect(start).toHaveBeenCalledWith({ media: "video" });
    expect(stop).toHaveBeenCalledTimes(1);
  });
});
