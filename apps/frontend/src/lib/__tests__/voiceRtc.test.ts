import { describe, it, expect, beforeEach, vi } from "vitest";
import { webVoiceRtc } from "../voiceRtc";

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
      getByteTimeDomainData: vi.fn((buf: Uint8Array) => {
        buf.fill(192);
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
    expect(levels?.mic).toBeCloseTo(0.5, 1); // (192-128)/128 ≈ 0.5
    expect(levels?.out).toBeDefined();
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
