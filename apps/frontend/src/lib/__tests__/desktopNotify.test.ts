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

  it("creates a Notification with title/body/tag when granted", async () => {
    expect(await nativeNotify("Alice", "Hello", { tag: "message:1" })).toBe(true);
    expect(instances).toHaveLength(1);
    expect(instances[0].title).toBe("Alice");
    expect(instances[0].options).toMatchObject({ body: "Hello", tag: "message:1" });
  });

  it("click calls window.focus and onClick, then closes the notification", async () => {
    const onClick = vi.fn();
    await nativeNotify("Alice", "Hello", { onClick, tag: "message:1" });
    const [created] = instances;
    created.onclick?.();
    expect(window.focus).toHaveBeenCalled();
    expect(onClick).toHaveBeenCalled();
    expect(created.close).toHaveBeenCalled();
  });

  it("does not notify when permission is not granted, so the caller can toast", async () => {
    FakeNotification.permission = "denied";
    expect(await nativeNotify("Alice", "Hello")).toBe(false);
    FakeNotification.permission = "default";
    expect(await nativeNotify("Alice", "Hello")).toBe(false);
    expect(instances).toHaveLength(0);
  });

  it("notifies even when the document has focus — the native banner replaces the toast", async () => {
    vi.mocked(document.hasFocus).mockReturnValue(true);
    expect(await nativeNotify("Alice", "Hello")).toBe(true);
    expect(instances).toHaveLength(1);
  });

  it("reports false when the Notification API is missing", async () => {
    vi.unstubAllGlobals();
    expect(await nativeNotify("Alice", "Hello")).toBe(false);
    expect(instances).toHaveLength(0);
  });
});

describe("nativeNotify — Tauri desktop shell", () => {
  const invoke = vi.fn();
  const listen = vi.fn();
  let onClicked: ((event: { payload: unknown }) => void) | null = null;

  beforeEach(() => {
    invoke.mockReset().mockResolvedValue(true);
    listen.mockReset().mockImplementation(async (_name: string, handler: (event: { payload: unknown }) => void) => {
      onClicked = handler;
      return () => {};
    });
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    (window as unknown as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke }, event: { listen } };
  });

  afterEach(() => {
    delete (window as unknown as { __TAURI__?: unknown }).__TAURI__;
    vi.restoreAllMocks();
  });

  it("invokes the native_notify command, even with focus, and reports its answer", async () => {
    expect(await nativeNotify("Title", "Body", { tag: "message:1" })).toBe(true);
    expect(invoke).toHaveBeenCalledWith("native_notify", { title: "Title", body: "Body", tag: "message:1" });
    // A denied System Settings permission answers false rather than throwing.
    invoke.mockResolvedValue(false);
    expect(await nativeNotify("Title", "Body")).toBe(false);
  });

  it("routes a native click back to the handler registered under its tag, once", async () => {
    const onClick = vi.fn();
    await nativeNotify("Title", "Body", { onClick, tag: "message:1" });
    expect(listen).toHaveBeenCalledWith("notification:clicked", expect.any(Function));
    onClicked?.({ payload: "message:other" });
    expect(onClick).not.toHaveBeenCalled();
    onClicked?.({ payload: "message:1" });
    onClicked?.({ payload: "message:1" });
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("reports false when the command fails (unbundled dev binary), so the caller can toast", async () => {
    invoke.mockRejectedValue(new Error("ipc"));
    expect(await nativeNotify("Title", "Body")).toBe(false);
  });
});
