// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { useEffect } from "react";

const viewer = vi.hoisted(() => ({ mounts: 0, unmounts: 0 }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: vi.fn(), post: vi.fn() }) }));
vi.mock("../dcvWebKitShim", () => ({ ensureDcvMediaShim: () => true }));
vi.mock("../DirectDcvLiveView", async () => {
  const { useEffect: effect } = await import("react");
  return {
    DirectDcvLiveView: ({ signedUrl, resizeRemote }: { signedUrl: string; resizeRemote?: unknown }) => {
      effect(() => {
        viewer.mounts += 1;
        return () => void (viewer.unmounts += 1);
      }, []);
      return <canvas data-testid="dcv" data-url={signedUrl} data-input={resizeRemote ? "panel" : "parked"} />;
    },
  };
});

import { SharedLiveViewHost, _resetSharedLiveViewForTests, useLiveViewHold, useLiveViewWatch } from "../sharedLiveView";

const source = { url: "https://signed", sessionId: "s1" };
function Preview(): null {
  useLiveViewWatch(source);
  return null;
}
function Panel(): React.ReactElement {
  const slot = document.getElementById("panel-slot")!;
  useLiveViewWatch(source);
  useLiveViewHold({ slot, menuHost: null, busy: false });
  return <></>;
}

async function flush(ms = 0): Promise<void> {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

beforeEach(() => {
  vi.useFakeTimers();
  viewer.mounts = 0;
  viewer.unmounts = 0;
  _resetSharedLiveViewForTests();
  document.body.innerHTML = '<div id="panel-slot"></div>';
});
afterEach(() => vi.useRealTimers());

describe("shared live view", () => {
  it("opens ONE client for any number of watchers, and moves it between panel and parking without reconnecting", async () => {
    const view = render(<><SharedLiveViewHost /><Preview /><Preview /><Preview /></>);
    await flush();
    expect(screen.getAllByTestId("dcv")).toHaveLength(1);
    expect(viewer.mounts).toBe(1);
    // No panel on screen: parked off screen, no input, still drawing for the previews.
    expect(document.getElementById("panel-slot")!.childElementCount).toBe(0);
    expect(screen.getByTestId("dcv")).toHaveAttribute("data-input", "parked");

    view.rerender(<><SharedLiveViewHost /><Preview /><Preview /><Preview /><Panel /></>);
    await flush();
    expect(document.getElementById("panel-slot")!.querySelector("[data-testid=dcv]")).not.toBeNull();
    expect(screen.getByTestId("dcv")).toHaveAttribute("data-input", "panel");

    view.rerender(<><SharedLiveViewHost /><Preview /></>); // the panel goes behind Messages
    await flush();
    expect(document.getElementById("panel-slot")!.childElementCount).toBe(0);
    expect(screen.getAllByTestId("dcv")).toHaveLength(1);
    expect(viewer.mounts).toBe(1);
    expect(viewer.unmounts).toBe(0);
  });

  it("lets go of the slot as soon as nothing watches", async () => {
    const view = render(<><SharedLiveViewHost /><Preview /></>);
    await flush();
    view.rerender(<SharedLiveViewHost />);
    await flush();
    expect(viewer.unmounts).toBe(1);
    expect(screen.queryByTestId("dcv")).toBeNull();
  });

  it("opens nothing with no watcher", async () => {
    function Idle(): null {
      useEffect(() => undefined, []);
      return null;
    }
    render(<><SharedLiveViewHost /><Idle /></>);
    await flush();
    expect(viewer.mounts).toBe(0);
  });
});
