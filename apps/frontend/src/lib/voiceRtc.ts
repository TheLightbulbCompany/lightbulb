import type { VoicePeer, VoiceRtc } from "@lightbulb/shared";

const ICE_WAIT_MS = 10_000;

function rms(analyser: AnalyserNode, buf: Uint8Array): number {
  analyser.getByteTimeDomainData(buf);
  let sum = 0;
  for (const v of buf) {
    const x = (v - 128) / 128;
    sum += x * x;
  }
  // ponytail: x2 gain so normal speech (RMS ~0.1-0.3) visibly moves the orb; tune by ear.
  return Math.min(1, Math.sqrt(sum / buf.length) * 2);
}

export const webVoiceRtc: VoiceRtc = {
  async open(): Promise<VoicePeer> {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    const created: { pc?: RTCPeerConnection; dc?: RTCDataChannel; ctx?: AudioContext } = {};
    try {
      const ctx = new AudioContext();
      created.ctx = ctx;
      const micAn = ctx.createAnalyser();
      micAn.fftSize = 512;
      const outAn = ctx.createAnalyser();
      outAn.fftSize = 512;
      ctx.createMediaStreamSource(stream).connect(micAn);
      const micBuf = new Uint8Array(micAn.fftSize);
      const outBuf = new Uint8Array(outAn.fftSize);

      const pc = new RTCPeerConnection();
      created.pc = pc;
      stream.getTracks().forEach((track) => pc.addTrack(track, stream));
      const audio = new Audio();
      audio.autoplay = true;
      pc.ontrack = (e) => {
        audio.srcObject = e.streams[0];
        ctx.createMediaStreamSource(e.streams[0]).connect(outAn);
        void audio.play().catch(() => {});
      };
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
        levels: () => ({ mic: rms(micAn, micBuf), out: rms(outAn, outBuf) }),
        setMuted: (m) => { stream.getAudioTracks().forEach((t) => { t.enabled = !m; }); },
        close: () => {
          stream.getTracks().forEach((t) => t.stop());
          dc.close();
          pc.close();
          audio.srcObject = null;
          void ctx.close();
        },
        onFailed: null,
      };
      pc.addEventListener("connectionstatechange", () => { if (pc.connectionState === "failed") peer.onFailed?.(); });
      return peer;
    } catch (err) {
      stream.getTracks().forEach((t) => t.stop());
      created.dc?.close();
      created.pc?.close();
      void created.ctx?.close();
      throw err;
    }
  },
};
