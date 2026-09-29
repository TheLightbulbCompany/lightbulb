import { act, renderHook } from "@testing-library/react-native";

import { useReadBack } from "../readBack";

const scrolled = (offset: number, content: number, viewport = 800) => ({
  nativeEvent: { contentOffset: { x: 0, y: offset }, contentSize: { width: 390, height: content }, layoutMeasurement: { width: 390, height: viewport } },
}) as never;
const laidOut = (height: number) => ({ nativeEvent: { layout: { x: 0, y: 0, width: 390, height } } }) as never;

describe("useReadBack", () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());
  const settle = () => act(async () => { jest.advanceTimersByTime(300); });

  it("fills a screen that opened short, then stops", async () => {
    const load = jest.fn();
    const { result, rerender } = await renderHook(
      ({ oldest }: { oldest: number }) => useReadBack({ scope: "s", oldest, canLoad: true, load }),
      { initialProps: { oldest: 100 } },
    );
    expect(load).not.toHaveBeenCalled(); // nothing measured yet
    await act(async () => { result.current.onLayout(laidOut(800)); result.current.onContentSizeChange(390, 300); });
    expect(load).toHaveBeenCalledTimes(1);
    // A page lands and draws nothing: the oldest moved, the height never does. Ask again.
    await rerender({ oldest: 50 });
    expect(load).toHaveBeenCalledTimes(1);
    await settle();
    expect(load).toHaveBeenCalledTimes(2);
    // The next one fills the screen and a screen behind it.
    await rerender({ oldest: 20 });
    await act(async () => { result.current.onContentSizeChange(390, 1700); });
    await settle();
    expect(load).toHaveBeenCalledTimes(2);
  });

  // Judged on the height before the page, a timeline that had just filled
  // several screens asked for one page more.
  it("waits for the height of a page that landed before judging it short", async () => {
    const load = jest.fn();
    const { result, rerender } = await renderHook(
      ({ oldest }: { oldest: number }) => useReadBack({ scope: "s", oldest, canLoad: true, load }),
      { initialProps: { oldest: 0 } },
    );
    // Laid out around a loading skeleton, 120 tall.
    await act(async () => { result.current.onLayout(laidOut(800)); result.current.onContentSizeChange(390, 120); });
    // The newest page lands; its rows are not measured yet.
    await rerender({ oldest: 100 });
    expect(load).not.toHaveBeenCalled();
    await act(async () => { result.current.onContentSizeChange(390, 3000); });
    await settle();
    expect(load).not.toHaveBeenCalled();
  });

  it("reads one page at the top, not the whole history", async () => {
    const load = jest.fn();
    const { result, rerender } = await renderHook(
      ({ oldest }: { oldest: number }) => useReadBack({ scope: "s", oldest, canLoad: true, load }),
      { initialProps: { oldest: 300 } },
    );
    await act(async () => { result.current.onScroll(scrolled(1200, 2000)); });
    expect(load).toHaveBeenCalledTimes(1);
    // The page lands above the reader: the list grows, the reader has not moved.
    await rerender({ oldest: 200 });
    await act(async () => { result.current.onContentSizeChange(390, 5000); });
    await settle();
    expect(load).toHaveBeenCalledTimes(1);
    // Scrolling on towards the new top asks for the next one.
    await act(async () => { result.current.onScroll(scrolled(3900, 5000)); });
    expect(load).toHaveBeenCalledTimes(2);
  });

  it("does not ask twice for a page that came back with nothing older", async () => {
    const load = jest.fn();
    const { result } = await renderHook(() => useReadBack({ scope: "s", oldest: 40, canLoad: true, load }));
    await act(async () => { result.current.onScroll(scrolled(1200, 2000)); result.current.onScroll(scrolled(1210, 2000)); });
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("has nothing to read at the beginning, or while it may not load", async () => {
    const load = jest.fn();
    const first = await renderHook(() => useReadBack({ scope: "s", oldest: 1, canLoad: true, load }));
    await act(async () => { first.result.current.onScroll(scrolled(1200, 2000)); });
    const busy = await renderHook(() => useReadBack({ scope: "s", oldest: 90, canLoad: false, load }));
    await act(async () => { busy.result.current.onScroll(scrolled(1200, 2000)); });
    expect(load).not.toHaveBeenCalled();
  });
});
