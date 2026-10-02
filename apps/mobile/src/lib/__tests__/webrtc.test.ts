import { NativeModules } from "react-native";

import { hasNativeVoice } from "../webrtc";

describe("hasNativeVoice", () => {
  afterEach(() => { delete (NativeModules as Record<string, unknown>).WebRTCModule; });

  it("is false on a binary without the WebRTC module", () => {
    expect(hasNativeVoice()).toBe(false);
  });
  it("is true when the module is linked", () => {
    (NativeModules as Record<string, unknown>).WebRTCModule = {};
    expect(hasNativeVoice()).toBe(true);
  });
});
