/* eslint-disable @typescript-eslint/no-explicit-any -- browser media API stubs */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { speechBands, webVoiceRtc } from "../voiceRtc";

describe("webVoiceRtc", () => {
  let mockStream: MediaStream;
  let mockPeerConnection: Partial<RTCPeerConnection>;
  let mockAudioContext: Partial<AudioContext>;
  let mockAnalyser: Partial<AnalyserNode>;
  let mockMediaStreamSource: Partial<AudioNode>;
  let mockTrack: any;

  beforeEach(() => {
    // Create a single mock track object that persists across calls
    mockTrack = { enabled: true, stop: vi.fn() };

    // Mock MediaStream
    mockStream = {
      getTracks: vi.fn(() => [mockTrack]),
      getAudioTracks: vi.fn(() => [mockTrack]),
    } as any;

    // Mock AnalyserNode
    mockAnalyser = {
      fftSize: 512,
      frequencyBinCount: 256,
      getByteFrequencyData: vi.fn((buf: Uint8Array) => {
        buf.fill(0);
        buf.fill(255, 1, 43); // 100 Hz..4 kHz at 48 kHz / 256 bins
      }),
      getByteTimeDomainData: vi.fn((buf: Uint8Array) => {
        buf.fill(160); // (160-128)/128 = 0.25 RMS, x2 gain = 0.5
      }),
    };

    // Mock AudioNode (source)
    mockMediaStreamSource = {
      connect: vi.fn(),
    };

    // Mock AudioContext
    mockAudioContext = {
      createMediaStreamSource: vi.fn(() => mockMediaStreamSource) as any,
      createAnalyser: vi.fn(() => mockAnalyser) as any,
      close: vi.fn(),
      resume: vi.fn(() => Promise.resolve()),
      sampleRate: 48_000,
    } as any;

    // Mock RTCPeerConnection
    mockPeerConnection = {
      addTrack: vi.fn(),
      createDataChannel: vi.fn(() => ({
        readyState: "open",
        send: vi.fn(),
        close: vi.fn(),
        onmessage: null,
      })),
      setLocalDescription: vi.fn(),
      createOffer: vi.fn(async () => ({ type: "offer", sdp: "test-sdp" })),
      localDescription: { sdp: "test-sdp" },
      iceGatheringState: "complete",
      connectionState: "connected",
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      close: vi.fn(),
      setRemoteDescription: vi.fn(),
    } as any;

    // Global mocks
    (global as any).navigator = {
      mediaDevices: {
        getUserMedia: vi.fn(async () => mockStream),
      },
    };

    global.RTCPeerConnection = vi.fn(() => mockPeerConnection) as any;
    global.AudioContext = vi.fn(() => mockAudioContext) as any;
    global.Audio = vi.fn(() => ({
      autoplay: true,
      srcObject: null,
      play: vi.fn(async () => {}),
    })) as any;
  });

  it("should provide levels() that returns RMS values", async () => {
    const peer = await webVoiceRtc.open();

    // Call levels() and expect mic level ≈ 0.5
    const levels = peer.levels?.();
    expect(levels).toBeDefined();
    expect(levels?.mic).toBeCloseTo(0.5, 2);
    expect(levels?.out).toBeDefined();
  });

  it("bands(n) returns n speech-range bands per side in 0..1", async () => {
    const peer = await webVoiceRtc.open();
    const bands = peer.bands?.(6);
    expect(bands?.mic).toHaveLength(6);
    expect(bands?.out).toHaveLength(6);
    bands?.mic.forEach((v) => expect(v).toBeCloseTo(1, 5));
  });

  it("speechBands drops bins outside 100 Hz..4 kHz and normalizes to 0..1", () => {
    const data = new Uint8Array(256); // 93.75 Hz per bin at 48 kHz
    data[0] = 255; // below 100 Hz: ignored
    data.fill(255, 50); // above 4 kHz: ignored
    data.fill(51, 1, 43);
    const bands = speechBands(data, 48_000, 4);
    expect(bands).toHaveLength(4);
    bands.forEach((v) => expect(v).toBeCloseTo(0.2, 5));
    expect(speechBands(new Uint8Array(256), 48_000, 64).every((v) => v === 0)).toBe(true);
  });

  it("should disable audio tracks when setMuted(true)", async () => {
    const peer = await webVoiceRtc.open();
    const mockTrack = mockStream.getAudioTracks()[0];

    peer.setMuted?.(true);

    expect(mockTrack.enabled).toBe(false);
  });

  it("should enable audio tracks when setMuted(false)", async () => {
    const peer = await webVoiceRtc.open();
    const mockTrack = mockStream.getAudioTracks()[0];

    peer.setMuted?.(true);
    expect(mockTrack.enabled).toBe(false);

    peer.setMuted?.(false);
    expect(mockTrack.enabled).toBe(true);
  });

  it("should close AudioContext in close()", async () => {
    const peer = await webVoiceRtc.open();

    peer.close();

    expect(mockAudioContext.close).toHaveBeenCalled();
  });

  it("should create AnalyserNodes on setup", async () => {
    const peer = await webVoiceRtc.open();

    expect(mockAudioContext.createAnalyser).toHaveBeenCalled();
    expect(mockMediaStreamSource.connect).toHaveBeenCalledWith(mockAnalyser);
  });
});
