import type { VoicePeer, VoiceRtc } from "@lightbulb/shared";

const ICE_WAIT_MS = 10_000;

export const webVoiceRtc: VoiceRtc = {
  async open(): Promise<VoicePeer> {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    const created: { pc?: RTCPeerConnection; dc?: RTCDataChannel } = {};
    try {
      const pc = new RTCPeerConnection();
      created.pc = pc;
      stream.getTracks().forEach((track) => pc.addTrack(track, stream));
      const audio = new Audio();
      audio.autoplay = true;
      pc.ontrack = (e) => { audio.srcObject = e.streams[0]; void audio.play().catch(() => {}); };
      const dc = pc.createDataChannel("oai-events"); // before the offer
      created.dc = dc;
      const channel = { send: (d: string) => { if (dc.readyState === "open") dc.send(d); }, onmessage: null as VoicePeer["channel"]["onmessage"] };
      dc.onmessage = (e) => channel.onmessage?.({ data: String(e.data) });
      await pc.setLocalDescription(await pc.createOffer());
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
        close: () => { stream.getTracks().forEach((t) => t.stop()); dc.close(); pc.close(); audio.srcObject = null; },
        onFailed: null,
      };
      pc.addEventListener("connectionstatechange", () => { if (pc.connectionState === "failed") peer.onFailed?.(); });
      return peer;
    } catch (err) {
      stream.getTracks().forEach((t) => t.stop());
      created.dc?.close();
      created.pc?.close();
      throw err;
    }
  },
};
