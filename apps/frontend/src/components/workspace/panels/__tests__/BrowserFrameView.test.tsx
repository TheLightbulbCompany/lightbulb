// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { LiveEvent } from "@lightbulb/shared";

const post = vi.fn();
const apiClient = { post };
const handlers = new Set<(event: LiveEvent) => void>();
const release = vi.fn();
const claimFrames = vi.fn(() => release);
vi.mock("@/lib/api", () => ({ useApi: () => apiClient }));
vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    onEvent: (handler: (event: LiveEvent) => void) => {
      handlers.add(handler);
      return () => handlers.delete(handler);
    },
    claimFrames,
  }),
}));

import { BrowserFrameView, FRAMES_HEARTBEAT_MS, INPUT_BATCH_MS } from "../BrowserFrameView";

function frame(runId: string, seq: number, jpegBase64 = `jpeg${seq}`, tier?: "view" | "thumb"): void {
  const event = {
    schemaVersion: 1, eventId: `${runId}-${seq}`, type: "room.browser.frame", occurredAt: "now", roomId: "r",
    payload: { roomId: "r", agentId: "a", runId, seq, jpegBase64, w: 1000, h: 500, tier },
  } as LiveEvent;
  act(() => handlers.forEach((handler) => handler(event)));
}

const picture = () => screen.getByRole("img") as HTMLImageElement;

beforeEach(() => {
  vi.useFakeTimers();
  post.mockReset().mockResolvedValue({ ok: true });
  handlers.clear();
  claimFrames.mockClear();
  release.mockClear();
});
afterEach(() => vi.useRealTimers());

describe("BrowserFrameView", () => {
  it("asks for frames while mounted and shows the newest frame of its run only", () => {
    const view = render(<BrowserFrameView runId="run-1" />);
    expect(post).toHaveBeenCalledWith("/browser/viewing", { frames: true });
    act(() => vi.advanceTimersByTime(FRAMES_HEARTBEAT_MS));
    expect(post).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("status")).toHaveTextContent(/waiting/i);
    frame("run-2", 9);
    expect(screen.queryByRole("img")).toBeNull();
    frame("run-1", 5);
    frame("run-1", 4); // AppSync reordered: never step back
    expect(picture().src).toContain("jpeg5");
    view.unmount();
    act(() => vi.advanceTimersByTime(FRAMES_HEARTBEAT_MS));
    expect(post).toHaveBeenCalledTimes(2);
  });

  it("a picture only unless interactive", () => {
    render(<BrowserFrameView runId="run-1" />);
    frame("run-1", 1);
    fireEvent.click(picture());
    expect(post).not.toHaveBeenCalledWith("/browser/input", expect.anything());
  });

  it("sends clicks in the tab's own pixels, batches typing and scrolling, and presses keys", () => {
    render(<BrowserFrameView runId="run-1" interactive />);
    frame("run-1", 1);
    const img = picture();
    img.getBoundingClientRect = () => ({ left: 10, top: 20, width: 500, height: 250, right: 510, bottom: 270, x: 10, y: 20, toJSON: () => ({}) });
    post.mockClear();
    fireEvent.click(img, { clientX: 260, clientY: 145 });
    expect(post).toHaveBeenLastCalledWith("/browser/input", { runId: "run-1", type: "click", x: 500, y: 250, button: "left" });
    // Letterboxed: a 1000x500 tab in a 500x500 box sits 125 px down; the bars map to the edge.
    img.getBoundingClientRect = () => ({ left: 0, top: 0, width: 500, height: 500, right: 500, bottom: 500, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.click(img, { clientX: 250, clientY: 250 });
    expect(post).toHaveBeenLastCalledWith("/browser/input", { runId: "run-1", type: "click", x: 500, y: 250, button: "left" });
    fireEvent.click(img, { clientX: 10, clientY: 20 });
    expect(post).toHaveBeenLastCalledWith("/browser/input", { runId: "run-1", type: "click", x: 20, y: 0, button: "left" });
    post.mockClear();
    fireEvent.keyDown(img, { key: "h" });
    fireEvent.keyDown(img, { key: "i" });
    fireEvent.keyDown(img, { key: "c", metaKey: true }); // the viewer's own copy, not the page's
    fireEvent.wheel(img, { clientX: 10, clientY: 20, deltaY: 100 });
    fireEvent.wheel(img, { clientX: 10, clientY: 20, deltaY: 60 });
    act(() => vi.advanceTimersByTime(INPUT_BATCH_MS));
    expect(post).toHaveBeenCalledWith("/browser/input", { runId: "run-1", type: "text", text: "hi" });
    expect(post).toHaveBeenCalledWith("/browser/input", { runId: "run-1", type: "wheel", x: 20, y: 0, deltaY: 160 });
    fireEvent.keyDown(img, { key: "Enter" });
    expect(post).toHaveBeenLastCalledWith("/browser/input", { runId: "run-1", type: "key", key: "Enter" });
    expect(post).toHaveBeenCalledTimes(3);
  });

  it("shows why input did not land, and offers the live view", async () => {
    const take = vi.fn();
    post.mockImplementation((path: string) => (path === "/browser/input"
      ? Promise.reject(new Error("That page isn't live here any more."))
      : Promise.resolve({})));
    render(<BrowserFrameView interactive onTakeControl={take} />);
    frame("run-7", 1);
    fireEvent.click(picture());
    expect(post).toHaveBeenLastCalledWith("/browser/input", expect.objectContaining({ runId: "run-7", type: "click" }));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(screen.getByRole("alert")).toHaveTextContent("isn't live here");
    fireEvent.click(screen.getByRole("button", { name: "Take live control" }));
    expect(take).toHaveBeenCalled();
  });
});

const box = (img: HTMLImageElement) => {
  img.getBoundingClientRect = () => ({ left: 10, top: 20, width: 500, height: 250, right: 510, bottom: 270, x: 10, y: 20, toJSON: () => ({}) });
};
const macCalls = () => post.mock.calls.filter(([path]) => path === "/browser/mac/input").map(([, body]) => body);
const settle = () => act(async () => { for (let i = 0; i < 5; i += 1) await Promise.resolve(); });

describe("BrowserFrameView on a Mac tab", () => {
  it("beats the view tier, claims the frames channel while mounted, and prefers the view frame", () => {
    const view = render(<BrowserFrameView runId="run-1" mac={{ deviceId: "mac-1" }} prefer="view" />);
    expect(post).toHaveBeenCalledWith("/browser/frames", { tier: "view" });
    expect(claimFrames).toHaveBeenCalledTimes(1);
    frame("run-1", 5, "thumb5");
    expect(picture().src).toContain("thumb5"); // no view frame yet: the thumb stands in
    frame("run-1", 3, "view3", "view");
    frame("run-1", 9, "thumb9"); // a newer thumb never displaces the sharp view
    expect(picture().src).toContain("view3");
    frame("run-1", 2, "view2", "view"); // never step back within a tier
    expect(picture().src).toContain("view3");
    view.unmount();
    expect(release).toHaveBeenCalledTimes(1);
  });

  it("pointer downs and ups post one batch in the tab's own pixels, counting a double click itself", async () => {
    render(<BrowserFrameView runId="run-1" mac={{ deviceId: "mac-1" }} />);
    frame("run-1", 1);
    const img = picture();
    box(img);
    // Pointer events carry detail=0: the second quick press in place is the double click.
    fireEvent.pointerDown(img, { clientX: 260, clientY: 145, button: 0 });
    fireEvent.pointerUp(img, { clientX: 260, clientY: 145, button: 0 });
    fireEvent.pointerDown(img, { clientX: 262, clientY: 146, button: 0 });
    fireEvent.pointerUp(img, { clientX: 262, clientY: 146, button: 0 });
    await settle();
    expect(macCalls()).toEqual([{ runId: "run-1", deviceId: "mac-1", events: [
      { type: "down", x: 500, y: 250, button: "left", clicks: 1 },
      { type: "up", x: 500, y: 250, button: "left", clicks: 1 },
      { type: "down", x: 504, y: 252, button: "left", clicks: 2 },
      { type: "up", x: 504, y: 252, button: "left", clicks: 2 },
    ] }]);
    // A press long after, or far away, starts over at one.
    act(() => vi.advanceTimersByTime(600));
    fireEvent.pointerDown(img, { clientX: 262, clientY: 146, button: 0 });
    fireEvent.pointerUp(img, { clientX: 262, clientY: 146, button: 0 });
    await settle();
    expect((macCalls()[1] as { events: Array<{ clicks: number }> }).events.map((e) => e.clicks)).toEqual([1, 1]);
    expect(post).not.toHaveBeenCalledWith("/browser/input", expect.anything());
  });

  it("keeps one request in flight: later events wait for it, moves coalesce, order holds", async () => {
    let resolve: (value: unknown) => void = () => undefined;
    post.mockImplementation((path: string) => (path === "/browser/mac/input"
      ? new Promise((done) => { resolve = done; })
      : Promise.resolve({})));
    render(<BrowserFrameView runId="run-1" mac={{ deviceId: "mac-1" }} />);
    frame("run-1", 1);
    const img = picture();
    box(img);
    fireEvent.pointerDown(img, { clientX: 10, clientY: 20, button: 0 });
    await settle();
    expect(macCalls()).toHaveLength(1);
    fireEvent.pointerMove(img, { clientX: 20, clientY: 20, buttons: 1 });
    fireEvent.pointerMove(img, { clientX: 30, clientY: 20, buttons: 1 });
    fireEvent.pointerMove(img, { clientX: 40, clientY: 20, buttons: 0 }); // no button held: no hover
    fireEvent.pointerUp(img, { clientX: 40, clientY: 20, button: 0 });
    await settle();
    expect(macCalls()).toHaveLength(1); // still waiting on the first
    resolve({ ok: true });
    await settle();
    expect(macCalls()[1]).toEqual({ runId: "run-1", deviceId: "mac-1", events: [
      { type: "move", x: 40, y: 0, button: "left", clicks: 1 },
      { type: "up", x: 60, y: 0, button: "left", clicks: 1 },
    ] });
  });

  it("sends keys with CDP modifier bits, Meta+a as a key, typing as text and a paste as paste", async () => {
    render(<BrowserFrameView runId="run-1" mac={{ deviceId: "mac-1" }} />);
    frame("run-1", 1);
    const img = picture();
    fireEvent.keyDown(img, { key: "a", metaKey: true });
    fireEvent.keyDown(img, { key: "c", metaKey: true }); // the viewer's own copy
    fireEvent.keyDown(img, { key: "ArrowLeft", shiftKey: true });
    await settle();
    fireEvent.keyDown(img, { key: "h" });
    fireEvent.keyDown(img, { key: "i" });
    act(() => vi.advanceTimersByTime(INPUT_BATCH_MS));
    await settle();
    fireEvent.paste(img, { clipboardData: { getData: () => "pasted" } });
    await settle();
    expect(macCalls().flatMap((body) => (body as { events: unknown[] }).events)).toEqual([
      { type: "key", key: "a", modifiers: 4 },
      { type: "key", key: "ArrowLeft", modifiers: 8 },
      { type: "text", text: "hi" },
      { type: "paste", text: "pasted" },
    ]);
  });

  it("a touch that becomes a pan (cancel) releases the Mac's button once", async () => {
    render(<BrowserFrameView runId="run-1" mac={{ deviceId: "mac-1" }} />);
    frame("run-1", 1);
    const img = picture();
    box(img);
    expect(img.className).toContain("touch-none");
    fireEvent.pointerDown(img, { clientX: 10, clientY: 20, button: 0 });
    fireEvent.pointerCancel(img, { clientX: 10, clientY: 20 });
    fireEvent.lostPointerCapture(img, { clientX: 10, clientY: 20 });
    fireEvent.pointerDown(img, { clientX: 400, clientY: 20, button: 0 });
    fireEvent.pointerUp(img, { clientX: 400, clientY: 20, button: 0 });
    fireEvent.lostPointerCapture(img, { clientX: 400, clientY: 20 }); // follows every up: not a second up
    await settle();
    expect((macCalls()[0] as { events: Array<{ type: string }> }).events.map((e) => e.type)).toEqual(["down", "up", "down", "up"]);
  });

  it("drops what queued behind a refused batch; later input still goes", async () => {
    let reject: (reason: unknown) => void = () => undefined;
    post.mockImplementation((path: string) => (path === "/browser/mac/input" && macCalls().length === 1
      ? new Promise((_, fail) => { reject = fail; })
      : Promise.resolve({})));
    render(<BrowserFrameView runId="run-1" mac={{ deviceId: "mac-1" }} />);
    frame("run-1", 1);
    const img = picture();
    fireEvent.keyDown(img, { key: "Enter" });
    await settle();
    fireEvent.keyDown(img, { key: "Tab" }); // queued behind the doomed request
    await settle();
    reject(new Error("That page isn't live on the Mac any more."));
    await settle();
    expect(macCalls()).toHaveLength(1);
    fireEvent.keyDown(img, { key: "Escape" });
    await settle();
    expect(macCalls()[1]).toEqual({ runId: "run-1", deviceId: "mac-1", events: [{ type: "key", key: "Escape", modifiers: 0 }] });
  });

  it("a view frame frozen >2 s behind the thumbs gives way to them", () => {
    render(<BrowserFrameView runId="run-1" mac={{ deviceId: "mac-1" }} prefer="view" />);
    frame("run-1", 10_000, "view", "view");
    frame("run-1", 11_000, "thumbA");
    expect(picture().src).toContain("view");
    frame("run-1", 12_500, "thumbB");
    expect(picture().src).toContain("thumbB");
  });

  it("shows the server's refusal in the alert line and keeps the view", async () => {
    post.mockImplementation((path: string) => (path === "/browser/mac/input"
      ? Promise.reject(new Error("That page isn't live on the Mac any more."))
      : Promise.resolve({})));
    render(<BrowserFrameView runId="run-1" mac={{ deviceId: "mac-1" }} />);
    frame("run-1", 1);
    fireEvent.keyDown(picture(), { key: "Enter" });
    await settle();
    expect(screen.getByRole("alert")).toHaveTextContent("isn't live on the Mac");
    expect(picture()).toBeInTheDocument();
  });

  it("without mac, claims nothing and keeps the cloud heartbeat", () => {
    render(<BrowserFrameView runId="run-1" interactive />);
    expect(claimFrames).not.toHaveBeenCalled();
    expect(post).toHaveBeenCalledWith("/browser/viewing", { frames: true });
  });
});
