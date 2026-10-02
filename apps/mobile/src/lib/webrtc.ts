import { NativeModules } from "react-native";

/**
 * The app's ONE WebRTC: LiveKit's fork (`@livekit/react-native-webrtc`) under Live
 * (livekit-client through `@livekit/react-native`). Everything native is required lazily,
 * so a module graph that never calls in (tests, a binary without the module) never evaluates it.
 */

/** False on a binary built before WebRTC was linked. `runtimeVersion: fingerprint` already keeps
 *  this bundle off such a binary; this is the belt to that brace. */
export function hasNativeVoice(): boolean {
  return !!(NativeModules as Record<string, unknown>).WebRTCModule;
}

let ready = false;

/** Installs the WebRTC globals livekit-client needs and the iOS audio session policy. Idempotent. */
export function setupWebRTC(): void {
  if (ready || !hasNativeVoice()) return;
  ready = true;
  /* eslint-disable-next-line @typescript-eslint/no-require-imports */
  const lk = require("@livekit/react-native") as typeof import("@livekit/react-native");
  lk.registerGlobals({ autoConfigureAudioSession: false });
  // Voice chat: play-and-record with Apple's voice processing (echo cancellation), out of the
  // speaker unless a headset or Bluetooth is connected. Applied natively as the engine starts and stops.
  const call = { audioCategory: "playAndRecord", audioCategoryOptions: ["allowBluetooth", "allowBluetoothA2DP", "defaultToSpeaker"], audioMode: "voiceChat" } as const;
  lk.setupIOSAudioManagement(true, {
    recording: { ...call, audioCategoryOptions: [...call.audioCategoryOptions] },
    recordingWithoutVoiceProcessing: { ...call, audioCategoryOptions: [...call.audioCategoryOptions], audioMode: "default" },
    // Listening only (muted in Live): stay in the call category so unmuting does not re-route audio.
    playout: { ...call, audioCategoryOptions: [...call.audioCategoryOptions] },
  });
}
