import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { waitFor } from "@testing-library/react";
import { nativeNotify } from "../desktopNotify";

describe("nativeNotify — browser fallback (no Tauri)", () => {
  let instances: FakeNotification[];

  class FakeNotification {
    static permission: NotificationPermission = "granted";
    onclick: (() => void) | null = null;
    close = vi.fn();
    constructor(public title: string, public options?: NotificationOptions) {
      instances.push(this);
    }
  }

  beforeEach(() => {
    instances = [];
    FakeNotification.permission = "granted";
    vi.stubGlobal("Notification", FakeNotification);
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    vi.spyOn(window, "focus").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("creates a Notification with title/body/tag when granted", () => {
    nativeNotify("Alice", "Hello", { tag: "message:1" });
    expect(instances).toHaveLength(1);
    expect(instances[0].title).toBe("Alice");
    expect(instances[0].options).toMatchObject({ body: "Hello", tag: "message:1" });
  });

  it("click calls window.focus and onClick, then closes the notification", () => {
    const onClick = vi.fn();
    nativeNotify("Alice", "Hello", { onClick, tag: "message:1" });
    const [created] = instances;
    created.onclick?.();
    expect(window.focus).toHaveBeenCalled();
    expect(onClick).toHaveBeenCalled();
    expect(created.close).toHaveBeenCalled();
  });

  it("does not notify when permission is denied", () => {
    FakeNotification.permission = "denied";
    nativeNotify("Alice", "Hello");
    expect(instances).toHaveLength(0);
  });

  it("does not notify when the document has focus", () => {
    vi.mocked(document.hasFocus).mockReturnValue(true);
    nativeNotify("Alice", "Hello");
    expect(instances).toHaveLength(0);
  });

  it("no-ops when the Notification API is missing", () => {
    vi.unstubAllGlobals();
    expect(() => nativeNotify("Alice", "Hello")).not.toThrow();
    expect(instances).toHaveLength(0);
  });
});

describe("nativeNotify — Tauri path is unchanged", () => {
  const isPermissionGranted = vi.fn();
  const requestPermission = vi.fn();
  const sendNotification = vi.fn();

  beforeEach(() => {
    isPermissionGranted.mockReset();
    requestPermission.mockReset();
    sendNotification.mockReset();
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    (window as unknown as { __TAURI__?: unknown }).__TAURI__ = {
      notification: { isPermissionGranted, requestPermission, sendNotification },
    };
  });

  afterEach(() => {
    delete (window as unknown as { __TAURI__?: unknown }).__TAURI__;
    vi.restoreAllMocks();
  });

  it("sends via the Tauri API, ignoring onClick from opts", async () => {
    isPermissionGranted.mockResolvedValue(true);
    const onClick = vi.fn();
    nativeNotify("Title", "Body", { onClick, tag: "ignored-by-tauri" });
    await waitFor(() => expect(sendNotification).toHaveBeenCalled());
    expect(sendNotification).toHaveBeenCalledWith({ title: "Title", body: "Body" });
    expect(onClick).not.toHaveBeenCalled();
  });

  it("skips when the document has focus", () => {
    vi.mocked(document.hasFocus).mockReturnValue(true);
    nativeNotify("Title", "Body");
    expect(isPermissionGranted).not.toHaveBeenCalled();
  });
});
