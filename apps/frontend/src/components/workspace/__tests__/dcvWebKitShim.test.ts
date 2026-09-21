import { describe, expect, it } from "vitest";

import { ensureDcvMediaShim } from "../panels/dcvWebKitShim";

describe("ensureDcvMediaShim", () => {
  it("supplies a writable mediaDevices object when WebKit exposes no media API", async () => {
    const original = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: undefined,
      writable: true,
    });

    try {
      ensureDcvMediaShim();
      expect(navigator.mediaDevices).toMatchObject({ getUserMedia: expect.any(Function) });
      await expect(navigator.mediaDevices.getUserMedia({ video: true })).resolves.toMatchObject({
        getAudioTracks: expect.any(Function),
        getTracks: expect.any(Function),
        getVideoTracks: expect.any(Function),
      });
    } finally {
      if (original) Object.defineProperty(navigator, "mediaDevices", original);
      else delete (navigator as Navigator & { mediaDevices?: MediaDevices }).mediaDevices;
    }
  });

  it("leaves a browser's native mediaDevices object intact", () => {
    const native = {};
    const original = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: native,
      writable: true,
    });

    try {
      ensureDcvMediaShim();
      expect(navigator.mediaDevices).toBe(native);
    } finally {
      if (original) Object.defineProperty(navigator, "mediaDevices", original);
      else delete (navigator as Navigator & { mediaDevices?: MediaDevices }).mediaDevices;
    }
  });
});
