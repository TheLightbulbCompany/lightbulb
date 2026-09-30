import { NativeModules } from "react-native";
import type { VoicePeer, VoiceRtc } from "@lightbulb/shared";

const ICE_WAIT_MS = 10_000;

/** False on a binary built before react-native-webrtc was linked (old build + new OTA). */
export function hasNativeVoice(): boolean {
  return !!(NativeModules as Record<string, unknown>).WebRTCModule;
}

export const nativeVoiceRtc: VoiceRtc = {
  async open(): Promise<VoicePeer> {
    // Required lazily: an old binary without the module must never evaluate these imports.
    /* eslint-disable @typescript-eslint/no-require-imports */
    const { RTCPeerConnection, mediaDevices } = require("react-native-webrtc");
    const InCallManager = require("react-native-incall-manager").default;
    /* eslint-enable @typescript-eslint/no-require-imports */
    const stream = await mediaDevices.getUserMedia({ audio: true });
    const created: { pc?: { close(): void }; dc?: { close(): void }; started?: boolean } = {};
    try {
      const pc = new RTCPeerConnection();
      created.pc = pc;
      stream.getTracks().forEach((t: unknown) => pc.addTrack(t, stream));
      const dc = pc.createDataChannel("oai-events"); // before the offer
      created.dc = dc;
      const channel = { send: (d: string) => { if (dc.readyState === "open") dc.send(d); }, onmessage: null as VoicePeer["channel"]["onmessage"] };
      dc.addEventListener("message", (e: { data: unknown }) => channel.onmessage?.({ data: String(e.data) }));
      InCallManager.start({ media: "audio" });
      created.started = true;
      InCallManager.setForceSpeakerphoneOn(true); // voice-chat mode defaults to the earpiece
      await pc.setLocalDescription(await pc.createOffer({}));
      await new Promise<void>((resolve) => {
        if (pc.iceGatheringState === "complete") return resolve();
        const finish = () => { clearTimeout(timer); pc.removeEventListener("icegatheringstatechange", done); resolve(); };
        const done = () => { if (pc.iceGatheringState === "complete") finish(); };
        const timer = setTimeout(finish, ICE_WAIT_MS);
        pc.addEventListener("icegatheringstatechange", done);
      });
      const peer: VoicePeer = {
        offerSdp: pc.localDescription?.sdp ?? "",
        channel,
        accept: (sdp) => pc.setRemoteDescription({ type: "answer", sdp }),
        close: () => { stream.getTracks().forEach((t: { stop(): void }) => t.stop()); dc.close(); pc.close(); InCallManager.stop(); },
        onFailed: null,
      };
      pc.addEventListener("connectionstatechange", () => { if (pc.connectionState === "failed") peer.onFailed?.(); });
      return peer;
    } catch (err) {
      stream.getTracks().forEach((t: { stop(): void }) => t.stop());
      created.dc?.close();
      created.pc?.close();
      if (created.started) InCallManager.stop();
      throw err;
    }
  },
};
