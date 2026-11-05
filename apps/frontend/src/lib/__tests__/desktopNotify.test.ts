import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isDesktopApp, nativeNotify, notificationPermission, onDesktopToast, onTaskNoticeClick } from "../desktopNotify";

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

  it("a task tag the Mac posted opens the thread pane", async () => {
    const openTask = vi.fn();
    // The tag the Mac posts for a task question; routing starts here if nothing registered before.
    // Click routing is already up from the test above (one listener per module), so only the tag matters here.
    const stop = onTaskNoticeClick(openTask);
    onClicked?.({ payload: "task:t1" });
    expect(openTask).toHaveBeenCalledWith("t1");
    stop();
    onClicked?.({ payload: "task:t2" });
    expect(openTask).toHaveBeenCalledTimes(1);
  });

});

describe("notificationPermission", () => {
  class FakeNotification {
    static permission: NotificationPermission = "default";
    static requestPermission = vi.fn();
  }

  beforeEach(() => {
    FakeNotification.permission = "default";
    FakeNotification.requestPermission.mockReset().mockResolvedValue("granted");
    vi.stubGlobal("Notification", FakeNotification);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete (window as unknown as { __TAURI__?: unknown }).__TAURI__;
  });

  it("reads the browser permission without prompting", async () => {
    expect(await notificationPermission()).toBe("default");
    FakeNotification.permission = "denied";
    expect(await notificationPermission(true)).toBe("denied");
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled();
  });

  it("prompts in the browser only while undecided, and returns the answer", async () => {
    expect(await notificationPermission(true)).toBe("granted");
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
  });

  it("is null without a Notification API", async () => {
    vi.unstubAllGlobals();
    expect(await notificationPermission()).toBeNull();
    expect(isDesktopApp()).toBe(false);
  });

  it("asks the desktop app, never the webview's own Notification API", async () => {
    const invoke = vi.fn().mockResolvedValue("denied");
    (window as unknown as { __TAURI__?: unknown }).__TAURI__ = { core: { invoke } };
    expect(isDesktopApp()).toBe(true);
    expect(await notificationPermission(true)).toBe("denied");
    expect(invoke).toHaveBeenCalledWith("notification_permission", { request: true });
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled();
    // No bundle, or a desktop build that predates the command: unknown, not denied.
    invoke.mockResolvedValue("unsupported");
    expect(await notificationPermission()).toBeNull();
    invoke.mockRejectedValue(new Error("command notification_permission not found"));
    expect(await notificationPermission()).toBeNull();
  });
});

describe("onDesktopToast — desktop notices when macOS notifications are off", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows desktop:toast text, and the open-the-window ask raises the agent Chrome", async () => {
    let handler: ((event: { payload: unknown }) => void) | null = null;
    const invoke = vi.fn(async () => undefined);
    vi.stubGlobal("__TAURI__", {
      core: { invoke },
      event: { listen: vi.fn(async (_name: string, h: typeof handler) => { handler = h; return () => {}; }) },
    });
    const show = vi.fn();
    const stop = onDesktopToast(show);
    await Promise.resolve();
    handler!({ payload: { title: "Lightbulb", body: "Growth is using your Mac's browser", tag: null } });
    expect(show).toHaveBeenLastCalledWith("Lightbulb", "Growth is using your Mac's browser", undefined);
    handler!({ payload: { title: "Lightbulb", body: "Open the Lightbulb agent window", tag: "lightbulb-agent-chrome-show" } });
    const open = show.mock.calls[1][2] as () => void;
    open();
    expect(invoke).toHaveBeenCalledWith("agent_chrome_raise");
    stop();
  });

  it("is a no-op outside the desktop app", () => {
    expect(onDesktopToast(vi.fn())).toBeTypeOf("function");
  });
});
