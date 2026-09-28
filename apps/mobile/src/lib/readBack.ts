import { useCallback, useEffect, useRef } from "react";
import type { LayoutChangeEvent, NativeScrollEvent, NativeSyntheticEvent } from "react-native";

/** Screens of older timeline kept loaded past what is on screen. */
const AHEAD_SCREENS = 1;
/** How long a page that landed may go without changing the list's height before it is taken to have drawn nothing. */
const SETTLE_MS = 250;

/**
 * Older history for an inverted timeline, read without a button.
 *
 * Not `onEndReached`: a list only raises that when its content grows, and a
 * page that draws nothing new (thread replies, a subagent's events, more of a
 * tool group already on screen) grows nothing -- the timeline then stops a few
 * rows in and never asks again. One rule instead, checked whenever a page
 * lands, the list is measured or the reader scrolls: keep a screen of older
 * timeline loaded past what is showing. On open that fills the screen and a
 * screen of scrollback behind it; while reading back it stays a screen ahead.
 *
 * The distance is worked out from what the list last reported, never latched:
 * a page landing moves the top away from the reader, and that has to stop the
 * reading or one visit to the top drains the whole history. And only from a
 * height measured since the page landed: judged on the height before it, a
 * timeline that had just filled several screens asked for one page more. A
 * page that draws nothing never changes the height, so after `SETTLE_MS` the
 * height there is counts as measured.
 *
 * `oldest` is the oldest sequence loaded (1 is the beginning); `scope` is the
 * conversation, so a reused screen starts over.
 */
export function useReadBack({ scope, oldest, canLoad, load }: {
  scope: string;
  oldest: number;
  canLoad: boolean;
  load: () => void;
}) {
  // Inverted: `offset` counts up from the newest message.
  const metrics = useRef({ viewport: 0, content: 0, offset: 0 });
  const asked = useRef({ scope, from: 0 });
  const measured = useRef(false);
  const readBack = useCallback(() => {
    if (!canLoad || oldest <= 1 || !measured.current) return;
    const { viewport, content, offset } = metrics.current;
    // Until the list has been measured there is nothing to judge by.
    if (viewport === 0 || content - viewport - offset >= viewport * AHEAD_SCREENS) return;
    // A page that came back with nothing older is not asked for twice.
    if (asked.current.scope === scope && asked.current.from === oldest) return;
    asked.current = { scope, from: oldest };
    load();
  }, [scope, oldest, canLoad, load]);
  // The list's own callbacks outlive a render; they call whatever is current.
  const current = useRef(readBack);
  current.current = readBack;
  // A page has landed (or the conversation changed): the height held is from
  // before it. Declared ahead of the effect below, which would otherwise judge
  // the new page on that height first.
  useEffect(() => {
    measured.current = false;
    const settled = setTimeout(() => {
      measured.current = true;
      current.current();
    }, SETTLE_MS);
    return () => clearTimeout(settled);
  }, [scope, oldest]);
  useEffect(readBack, [readBack]);
  const onScroll = useCallback((event: NativeSyntheticEvent<NativeScrollEvent>) => {
    const { contentOffset, contentSize, layoutMeasurement } = event.nativeEvent;
    metrics.current = { viewport: layoutMeasurement.height, content: contentSize.height, offset: contentOffset.y };
    measured.current = true;
    current.current();
  }, []);
  const onLayout = useCallback((event: LayoutChangeEvent) => {
    metrics.current.viewport = event.nativeEvent.layout.height;
    current.current();
  }, []);
  const onContentSizeChange = useCallback((_: number, height: number) => {
    metrics.current.content = height;
    measured.current = true;
    current.current();
  }, []);
  return { onScroll, onLayout, onContentSizeChange };
}
