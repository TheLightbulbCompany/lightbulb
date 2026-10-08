import { describe, expect, it, vi } from "vitest";

import {
  beginNativeAppReturn,
  createNativeAppSignInUrl,
  isIosDevice,
} from "../nativeAppReturn";

describe("isIosDevice", () => {
  it.each(["iPhone", "iPad", "iPod"])("recognises %s browsers", (device) => {
    expect(isIosDevice(`Mozilla/5.0 (${device}; CPU OS 18_0 like Mac OS X)`, "", 0)).toBe(true);
  });

  it("recognises iPadOS desktop-mode Safari", () => {
    expect(isIosDevice("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15)", "MacIntel", 5)).toBe(true);
  });

  it("does not treat desktop or Android browsers as iOS", () => {
    expect(isIosDevice("Mozilla/5.0 (Macintosh; Intel Mac OS X)", "MacIntel", 0)).toBe(false);
    expect(isIosDevice("Mozilla/5.0 (Linux; Android 15)", "Linux armv8l", 5)).toBe(false);
  });
});

describe("beginNativeAppReturn", () => {
  it("opens the native app on iOS, then keeps a timed web fallback", () => {
    const openApp = vi.fn();
    const continueWeb = vi.fn();
    const scheduleFallback = vi.fn(() => 17);

    expect(beginNativeAppReturn({
      userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)",
      platform: "iPhone",
      maxTouchPoints: 5,
      openApp,
      continueWeb,
      scheduleFallback,
    })).toBe(17);
    expect(openApp).toHaveBeenCalledWith("flickmeon://");
    expect(scheduleFallback).toHaveBeenCalledWith(continueWeb, 1500);
    expect(continueWeb).not.toHaveBeenCalled();
  });

  it("continues directly to web on non-iOS devices", () => {
    const openApp = vi.fn();
    const continueWeb = vi.fn();
    const scheduleFallback = vi.fn();

    expect(beginNativeAppReturn({
      userAgent: "Mozilla/5.0 (X11; Linux x86_64)",
      platform: "Linux x86_64",
      maxTouchPoints: 0,
      openApp,
      continueWeb,
      scheduleFallback,
    })).toBeNull();
    expect(openApp).not.toHaveBeenCalled();
    expect(scheduleFallback).not.toHaveBeenCalled();
    expect(continueWeb).toHaveBeenCalledOnce();
  });
});


describe("createNativeAppSignInUrl", () => {
  it("exchanges the browser session for a one-time Clerk ticket", async () => {
    const getToken = vi.fn().mockResolvedValue("browser-session-token");
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ token: "one-time ticket+/=" }),
    });

    await expect(
      createNativeAppSignInUrl("/chat?room=42", getToken, fetchImpl),
    ).resolves.toBe(
      "flickmeon://auth?ticket=one-time%20ticket%2B%2F%3D",
    );
    expect(getToken).toHaveBeenCalledOnce();
    expect(fetchImpl).toHaveBeenCalledWith(
      expect.stringMatching(/\/auth\/desktop\/sign-in-token$/),
      expect.objectContaining({
        method: "POST",
        headers: { Authorization: "Bearer browser-session-token" },
      }),
    );
  });

  it("does not open a signed-out app when the ticket exchange fails", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: false, status: 503 });

    await expect(
      createNativeAppSignInUrl("/chat", async () => "browser-session-token", fetchImpl),
    ).rejects.toThrow("Could not prepare the native sign-in handoff");
  });
});
