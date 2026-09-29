// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { LiveEvent } from "@lightbulb/shared";

const post = vi.fn();
const apiClient = { post };
const handlers = new Set<(event: LiveEvent) => void>();
vi.mock("@/lib/api", () => ({ useApi: () => apiClient }));
vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    onEvent: (handler: (event: LiveEvent) => void) => {
      handlers.add(handler);
      return () => handlers.delete(handler);
    },
  }),
}));

import { BrowserFrameView, FRAMES_HEARTBEAT_MS, INPUT_BATCH_MS } from "../BrowserFrameView";

function frame(runId: string, seq: number, jpegBase64 = `jpeg${seq}`): void {
  const event = {
    schemaVersion: 1, eventId: `${runId}-${seq}`, type: "room.browser.frame", occurredAt: "now", roomId: "r",
    payload: { roomId: "r", agentId: "a", runId, seq, jpegBase64, w: 1000, h: 500 },
  } as LiveEvent;
  act(() => handlers.forEach((handler) => handler(event)));
}

const picture = () => screen.getByRole("img") as HTMLImageElement;

beforeEach(() => {
  vi.useFakeTimers();
  post.mockReset().mockResolvedValue({ ok: true });
  handlers.clear();
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
