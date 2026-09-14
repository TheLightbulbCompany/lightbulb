import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
    expect(nativeNotify("Alice", "Hello", { tag: "message:1" })).toBe(true);
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

  it("does not notify when permission is not granted, so the caller can toast", () => {
    FakeNotification.permission = "denied";
    expect(nativeNotify("Alice", "Hello")).toBe(false);
    FakeNotification.permission = "default";
    expect(nativeNotify("Alice", "Hello")).toBe(false);
    expect(instances).toHaveLength(0);
  });

  it("notifies even when the document has focus — the native banner replaces the toast", () => {
    vi.mocked(document.hasFocus).mockReturnValue(true);
    expect(nativeNotify("Alice", "Hello")).toBe(true);
    expect(instances).toHaveLength(1);
  });

  it("reports false when the Notification API is missing", () => {
    vi.unstubAllGlobals();
    expect(nativeNotify("Alice", "Hello")).toBe(false);
    expect(instances).toHaveLength(0);
  });
});

describe("nativeNotify — Tauri desktop shell", () => {
  const sendNotification = vi.fn();

  beforeEach(() => {
    sendNotification.mockReset();
    vi.spyOn(document, "hasFocus").mockReturnValue(false);
    (window as unknown as { __TAURI__?: unknown }).__TAURI__ = { notification: { sendNotification } };
  });

  afterEach(() => {
    delete (window as unknown as { __TAURI__?: unknown }).__TAURI__;
    vi.restoreAllMocks();
  });

  it("sends via the Tauri API, ignoring onClick from opts", () => {
    const onClick = vi.fn();
    expect(nativeNotify("Title", "Body", { onClick, tag: "ignored-by-tauri" })).toBe(true);
    expect(sendNotification).toHaveBeenCalledWith({ title: "Title", body: "Body" });
    expect(onClick).not.toHaveBeenCalled();
  });

  it("always sends natively, even when the window has focus", () => {
    vi.mocked(document.hasFocus).mockReturnValue(true);
    expect(nativeNotify("Title", "Body")).toBe(true);
    expect(sendNotification).toHaveBeenCalledTimes(1);
  });

  it("reports false when the send throws, so the caller can toast", () => {
    sendNotification.mockImplementation(() => { throw new Error("ipc"); });
    expect(nativeNotify("Title", "Body")).toBe(false);
  });
});
