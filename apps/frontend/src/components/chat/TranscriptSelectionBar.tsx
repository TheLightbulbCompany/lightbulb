"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { Copy, Check } from "lucide-react";
import { cn } from "@/lib/utils";

/** Clearance kept between the bar and both the selection rect and the
 *  viewport edges. */
const GUTTER = 8;
/** Used only before the bar has EVER mounted in this session. Every show
 *  after the first reads a real size — either from the currently-mounted
 *  bar, or (while it's unmounted between selections) from `lastSizeRef`,
 *  cached by a layout effect the last time it *was* mounted. The bar
 *  unmounts on every `hide()` (see the component doc comment for why: this
 *  keeps its one-shot entrance animation replaying on every reappearance),
 *  which resets `barRef.current` to null each time — without the cache,
 *  every show after the first would silently keep using this estimate. */
const ESTIMATED_WIDTH = 96;
const ESTIMATED_HEIGHT = 32;

interface BarCoords {
  top: number;
  left: number;
}

/**
 * Floating "Copy" action for text selected inside a transcript.
 *
 * Desktop (fine-pointer) only. On touch, the OS already renders its own
 * selection callout (Copy / Look Up / Share…) for the exact same long-press
 * + drag-handle gesture a custom bar would have to share — layering a second
 * floating control on top of that fights the platform instead of using it,
 * so this never mounts anything under a coarse/no-hover-primary pointer.
 *
 * Rendered through `createPortal(…, document.body)` rather than in place:
 * MessageList's bubble groups mount inside `.chat-block-in` (a translate
 * reveal) and the assistant column is `position: relative` — an ancestor
 * `transform` redefines the containing block for any descendant
 * `position: fixed`, trapping it inside that ancestor's box instead of the
 * viewport. Escaping straight to `document.body` sidesteps that regardless
 * of what any current or future ancestor does.
 */
export function TranscriptSelectionBar({
  containerRef,
  isAutoScrollingRef,
}: {
  /** The transcript scroll container. A selection only surfaces the bar
   *  when it lives entirely inside this element — the composer, the
   *  sidebar, and anything else on the page must not trigger it. */
  containerRef: React.RefObject<HTMLElement | null>;
  /** True for the brief window around a PROGRAMMATIC scrollTop write (the
   *  transcript's autoscroll). The capture-phase scroll listener below fires
   *  for those writes too — without this it destroys the bar on every
   *  streamed chunk, before a mid-answer selection can ever be clicked. */
  isAutoScrollingRef?: React.RefObject<boolean>;
}) {
  const [finePointer, setFinePointer] = React.useState(false);
  const [coords, setCoords] = React.useState<BarCoords | null>(null);
  const [selectedText, setSelectedText] = React.useState("");
  const [copied, setCopied] = React.useState(false);
  const barRef = React.useRef<HTMLDivElement>(null);
  /** Last real `offsetWidth`/`offsetHeight` measured while the bar was
   *  mounted — see the ESTIMATED_WIDTH doc comment above. */
  const lastSizeRef = React.useRef<{ width: number; height: number } | null>(null);
  const copyTimerRef = React.useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  React.useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(pointer: fine)");
    setFinePointer(mq.matches);
    const onChange = (e: MediaQueryListEvent) => setFinePointer(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  const hide = React.useCallback(() => {
    setCoords(null);
    setSelectedText("");
  }, []);

  /** Reads the live selection, confirms it sits inside the transcript, and
   *  (re)computes the bar's position. Called on gesture-END events
   *  (mouseup / keyup) rather than on every `selectionchange` tick, so the
   *  bar appears once a drag settles instead of chasing the cursor pixel by
   *  pixel while the user is still selecting. */
  const measure = React.useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return hide();
    const text = sel.toString();
    if (!text.trim()) return hide();

    // anchorNode/focusNode are frequently text nodes — climb to an element
    // to use `.contains`.
    const anchorEl =
      sel.anchorNode?.nodeType === Node.ELEMENT_NODE
        ? (sel.anchorNode as Element)
        : sel.anchorNode?.parentElement;
    const focusEl =
      sel.focusNode?.nodeType === Node.ELEMENT_NODE
        ? (sel.focusNode as Element)
        : sel.focusNode?.parentElement;
    if (!anchorEl || !focusEl || !container.contains(anchorEl) || !container.contains(focusEl)) {
      return hide();
    }

    const rect = sel.getRangeAt(0).getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return hide();

    const barWidth = barRef.current?.offsetWidth || lastSizeRef.current?.width || ESTIMATED_WIDTH;
    const barHeight = barRef.current?.offsetHeight || lastSizeRef.current?.height || ESTIMATED_HEIGHT;

    // Prefer sitting clear above the selection; flip below it when there
    // isn't room, so it never covers the text just selected.
    const fitsAbove = rect.top - barHeight - GUTTER >= 0;
    const top = fitsAbove ? rect.top - barHeight - GUTTER : rect.bottom + GUTTER;
    const rawLeft = rect.left + rect.width / 2 - barWidth / 2;

    setSelectedText(text);
    setCoords({
      top: Math.min(Math.max(top, GUTTER), window.innerHeight - barHeight - GUTTER),
      left: Math.min(Math.max(rawLeft, GUTTER), window.innerWidth - barWidth - GUTTER),
    });
  }, [containerRef, hide]);

  React.useEffect(() => {
    if (!finePointer) {
      hide();
      return;
    }

    // Cheap collapse detector — hides on ANY way the selection can empty
    // out (deselect via click, Ctrl/Cmd+click, programmatic clear) without
    // recomputing position on every drag tick.
    const onSelectionChange = () => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) hide();
    };
    const onMouseUp = () => measure();
    const onKeyUp = (e: KeyboardEvent) => {
      // Escape's keydown handler (below) already hid the bar; measuring on
      // its keyup would immediately re-show it, since Escape never touches
      // the underlying DOM selection — only our own visibility state.
      if (e.key === "Escape") return;
      measure();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") hide();
    };
    // Scroll events don't bubble, so any scrollable ancestor (the transcript
    // pane itself, an outer dock panel) is only reachable via the capture
    // phase on window. The bar's coordinates are a snapshot of the
    // selection's viewport rect at measure time — once anything scrolls,
    // that snapshot is stale, so it dismisses rather than drifting away
    // from the text it was anchored to.
    //
    // MessageList's own autoscroll writes `scrollTop` directly on this same
    // container while a run streams (~10/s), and that write fires this exact
    // scroll event too — without the guard below, selecting text mid-answer
    // gets its bar destroyed before a click can ever land on it.
    // `isAutoScrollingRef` is set for the brief window around that write (see
    // useScrollToBottom's markAutoScroll), so a REAL user scroll — which
    // never touches the flag — still dismisses as before.
    const onScroll = () => {
      if (isAutoScrollingRef?.current) return;
      hide();
    };
    const onMouseDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (barRef.current?.contains(target)) return; // let the button's own click land
      if (!containerRef.current?.contains(target)) hide();
    };

    document.addEventListener("selectionchange", onSelectionChange);
    document.addEventListener("mouseup", onMouseUp);
    document.addEventListener("keyup", onKeyUp);
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onMouseDown);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("selectionchange", onSelectionChange);
      document.removeEventListener("mouseup", onMouseUp);
      document.removeEventListener("keyup", onKeyUp);
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onMouseDown);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [finePointer, measure, hide, containerRef, isAutoScrollingRef]);

  React.useEffect(() => () => clearTimeout(copyTimerRef.current), []);

  // Runs after the bar commits with real coords, i.e. exactly when it's
  // mounted — capture its true size for the NEXT measure(), which will run
  // after this instance has unmounted and zeroed barRef.current.
  React.useLayoutEffect(() => {
    const bar = barRef.current;
    if (!bar) return;
    lastSizeRef.current = { width: bar.offsetWidth, height: bar.offsetHeight };
  }, [coords]);

  if (!finePointer || !coords) return null;

  const handleCopy = async () => {
    if (!navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(selectedText);
      setCopied(true);
      clearTimeout(copyTimerRef.current);
      copyTimerRef.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard write failed — no success state.
    }
  };

  return createPortal(
    <div
      ref={barRef}
      role="toolbar"
      aria-label="Selection actions"
      style={{ position: "fixed", top: coords.top, left: coords.left }}
      className="z-(--z-chrome) flex origin-bottom items-center rounded-lg border border-[var(--edge)] bg-[var(--cream)] p-1 transition-[opacity,scale] duration-(--dur-fast) ease-out starting:scale-[0.97] starting:opacity-0 motion-reduce:transition-[opacity]"
    >
      <button
        type="button"
        // Prevents the browser's default mousedown behavior of collapsing
        // the current selection before the click (and therefore the copy)
        // ever fires.
        onMouseDown={(e) => e.preventDefault()}
        onClick={handleCopy}
        aria-label={copied ? "Copied" : "Copy selection"}
        className={cn(
          "flex items-center gap-1.5 rounded-md px-2 py-1 text-xs transition-colors",
          copied ? "text-[var(--midnight)]" : "text-[var(--midnight-muted)] hover:text-[var(--midnight)] hover:bg-[var(--cream-deep)]",
        )}
      >
        {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
        Copy
      </button>
    </div>,
    document.body,
  );
}
