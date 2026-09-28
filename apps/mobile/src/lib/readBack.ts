import { useCallback, useEffect, useRef } from "react";
import type { LayoutChangeEvent, NativeScrollEvent, NativeSyntheticEvent } from "react-native";

/** Screens of timeline kept loaded behind the newest message. */
const FILL_SCREENS = 2;
/** How close to the top, in screens, the reader gets before older history is read. */
const TOP_SLACK_SCREENS = 0.5;

/**
 * Older history for an inverted timeline, read without a button.
 *
 * Not `onEndReached`: a list only raises that when its content grows, and a
 * page that draws nothing new (thread replies, a subagent's events, more of a
 * tool group already on screen) grows nothing -- the timeline then stops a few
 * rows in and never asks again. So there are two reasons to read back, checked
 * whenever a page lands and whenever the reader scrolls: the screen and a
 * little scrollback behind it are not full yet, or the reader is near the top.
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
  const viewport = useRef(0);
  const content = useRef(0);
  const nearTop = useRef(false);
  const asked = useRef({ scope, from: 0 });
  const readBack = useCallback(() => {
    if (!canLoad || oldest <= 1) return;
    // Until the list has been measured there is nothing to judge fullness by.
    const full = viewport.current === 0 || content.current >= viewport.current * FILL_SCREENS;
    if (full && !nearTop.current) return;
    // A page that came back with nothing older is not asked for twice.
    if (asked.current.scope === scope && asked.current.from === oldest) return;
    asked.current = { scope, from: oldest };
    load();
  }, [scope, oldest, canLoad, load]);
  useEffect(readBack, [readBack]);
  // The list's own callbacks outlive a render; they call whatever is current.
  const current = useRef(readBack);
  current.current = readBack;
  const onScroll = useCallback((event: NativeSyntheticEvent<NativeScrollEvent>) => {
    const { contentOffset, contentSize, layoutMeasurement } = event.nativeEvent;
    // Inverted: the far end of the scroll is the top of the timeline.
    nearTop.current = contentSize.height - layoutMeasurement.height - contentOffset.y < layoutMeasurement.height * TOP_SLACK_SCREENS;
    if (nearTop.current) current.current();
  }, []);
  const onLayout = useCallback((event: LayoutChangeEvent) => {
    viewport.current = event.nativeEvent.layout.height;
    current.current();
  }, []);
  const onContentSizeChange = useCallback((_: number, height: number) => {
    content.current = height;
    current.current();
  }, []);
  return { onScroll, onLayout, onContentSizeChange };
}
