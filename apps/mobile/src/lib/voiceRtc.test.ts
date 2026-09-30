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
