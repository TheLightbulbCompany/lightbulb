/**
 * TranscriptSelectionBar — the floating Copy action for text selected
 * inside a transcript.
 *
 * jsdom does no real layout: every element (and every Range) reports
 * `getBoundingClientRect()` as all-zeros, and `offsetWidth`/`offsetHeight`
 * are always 0. So instead of pretending jsdom can verify true on-screen
 * geometry, the positioning tests below stub `Range.prototype.
 * getBoundingClientRect` with a specific, known rect and assert the
 * component's own clamping math against it — that's a real test of the
 * logic this file owns (never covering the selection, never overflowing
 * the viewport), not a claim that jsdom rendered real pixels.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import * as React from "react";
import { TranscriptSelectionBar } from "../TranscriptSelectionBar";

const writeTextMock = vi.fn();

function stubClipboard() {
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText: writeTextMock },
    configurable: true,
    writable: true,
  });
}

/** Fresh stub every test — `restoreAllMocks` in the global afterEach (see
 *  tests/setup.ts) resets any vi.fn() implementation set outside a
 *  beforeEach back to a bare no-op, so this has to be re-armed every time
 *  rather than once at module scope. */
function stubMatchMedia(matches: boolean) {
  Object.defineProperty(window, "matchMedia", {
    value: vi.fn().mockImplementation((query: string) => ({
      matches,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
    configurable: true,
    writable: true,
  });
}

function stubRect(rect: Partial<DOMRect>) {
  const full: DOMRect = {
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    width: 0,
    height: 0,
    x: 0,
    y: 0,
    toJSON() {
      return this;
    },
    ...rect,
  };
  vi.spyOn(Range.prototype, "getBoundingClientRect").mockReturnValue(full);
}

function selectContents(el: Element) {
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel?.removeAllRanges();
  sel?.addRange(range);
}

function deselect() {
  window.getSelection()?.removeAllRanges();
}

/** Selects `el`'s contents, gives it a real (mocked) rect, and fires the
 *  gesture-end event the component listens for to show the bar. */
function selectAndShow(el: Element, rect: Partial<DOMRect> = { top: 100, bottom: 120, left: 500, width: 80, height: 20 }) {
  stubRect(rect);
  selectContents(el);
  act(() => {
    document.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  });
}

function Harness() {
  const containerRef = React.useRef<HTMLDivElement>(null);
  return (
    <div>
      <div ref={containerRef} data-testid="transcript">
        <p data-testid="in-transcript">Selectable transcript text</p>
      </div>
      <p data-testid="outside">Sidebar text outside the transcript</p>
      <TranscriptSelectionBar containerRef={containerRef} />
    </div>
  );
}

beforeEach(() => {
  stubMatchMedia(true); // fine pointer (desktop) by default
  writeTextMock.mockReset().mockResolvedValue(undefined);
  stubClipboard();
  Object.defineProperty(window, "innerWidth", { value: 1024, configurable: true });
  Object.defineProperty(window, "innerHeight", { value: 768, configurable: true });
});

afterEach(() => {
  deselect();
});

describe("TranscriptSelectionBar — appearance scope", () => {
  it("appears for a selection inside the transcript", () => {
    render(<Harness />);
    selectAndShow(screen.getByTestId("in-transcript"));
    expect(screen.getByRole("toolbar", { name: "Selection actions" })).toBeInTheDocument();
  });

  it("does not appear for a selection outside the transcript (sidebar/composer)", () => {
    render(<Harness />);
    selectAndShow(screen.getByTestId("outside"));
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
  });

  it("never appears on a coarse (touch) pointer, even with a valid in-transcript selection", () => {
    stubMatchMedia(false);
    render(<Harness />);
    selectAndShow(screen.getByTestId("in-transcript"));
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
  });
});

describe("TranscriptSelectionBar — dismissal", () => {
  it("dismisses when the selection collapses (deselect)", () => {
    render(<Harness />);
    selectAndShow(screen.getByTestId("in-transcript"));
    expect(screen.getByRole("toolbar")).toBeInTheDocument();

    act(() => {
      deselect();
      document.dispatchEvent(new Event("selectionchange"));
    });
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
  });

  it("dismisses on scroll", () => {
    render(<Harness />);
    selectAndShow(screen.getByTestId("in-transcript"));
    expect(screen.getByRole("toolbar")).toBeInTheDocument();

    act(() => {
      window.dispatchEvent(new Event("scroll"));
    });
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
  });

  it("dismisses on Escape", () => {
    render(<Harness />);
    selectAndShow(screen.getByTestId("in-transcript"));
    expect(screen.getByRole("toolbar")).toBeInTheDocument();

    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
  });

  it("does not immediately re-show after Escape (keyup does not re-measure)", () => {
    render(<Harness />);
    selectAndShow(screen.getByTestId("in-transcript"));

    act(() => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      document.dispatchEvent(new KeyboardEvent("keyup", { key: "Escape", bubbles: true }));
    });
    // Escape never touches the underlying DOM selection, so a keyup handler
    // that blindly re-measured would resurrect the bar right after Escape
    // dismissed it.
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
  });

  it("dismisses on a click outside both the transcript and the bar", () => {
    render(<Harness />);
    selectAndShow(screen.getByTestId("in-transcript"));
    expect(screen.getByRole("toolbar")).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByTestId("outside"));
    expect(screen.queryByRole("toolbar")).not.toBeInTheDocument();
  });

  it("a click on the bar itself does not dismiss it", () => {
    render(<Harness />);
    selectAndShow(screen.getByTestId("in-transcript"));
    const button = screen.getByRole("button", { name: /copy selection/i });

    fireEvent.mouseDown(button);
    expect(screen.getByRole("toolbar")).toBeInTheDocument();
  });
});

describe("TranscriptSelectionBar — copy action", () => {
  it("copies the exact selected text", async () => {
    render(<Harness />);
    selectAndShow(screen.getByTestId("in-transcript"));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /copy selection/i }));
    });
    expect(writeTextMock).toHaveBeenCalledWith("Selectable transcript text");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("prevents default on mousedown so the browser doesn't collapse the selection before the click lands", () => {
    render(<Harness />);
    selectAndShow(screen.getByTestId("in-transcript"));
    const button = screen.getByRole("button", { name: /copy selection/i });

    const event = new MouseEvent("mousedown", { bubbles: true, cancelable: true });
    button.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });
});

describe("TranscriptSelectionBar — positioning stays clear of the selection and inside the viewport", () => {
  it("sits above the selection when there's room", () => {
    render(<Harness />);
    selectAndShow(screen.getByTestId("in-transcript"), { top: 100, bottom: 120, left: 500, width: 80, height: 20 });

    const bar = screen.getByRole("toolbar");
    // jsdom reports 0 for offsetWidth/offsetHeight, so the component's own
    // documented fallback estimate (96x32) is what the clamp math uses —
    // asserting against that constant, not a jsdom-measured box.
    expect(bar.style.top).toBe(`${100 - 32 - 8}px`);
  });

  it("flips below the selection when there's no room above", () => {
    render(<Harness />);
    selectAndShow(screen.getByTestId("in-transcript"), { top: 10, bottom: 30, left: 500, width: 80, height: 20 });

    const bar = screen.getByRole("toolbar");
    expect(bar.style.top).toBe(`${30 + 8}px`);
  });

  it("clamps left so the bar never overflows the right edge of the viewport", () => {
    render(<Harness />);
    // Selection near the right edge of a 1024px-wide viewport.
    selectAndShow(screen.getByTestId("in-transcript"), { top: 100, bottom: 120, left: 1000, width: 20, height: 20 });

    const bar = screen.getByRole("toolbar");
    const maxLeft = 1024 - 96 - 8; // innerWidth - estimated bar width - gutter
    expect(Number(bar.style.left.replace("px", ""))).toBeLessThanOrEqual(maxLeft);
  });

  it("clamps left so the bar never overflows the left edge of the viewport", () => {
    render(<Harness />);
    // Selection hugging the left edge.
    selectAndShow(screen.getByTestId("in-transcript"), { top: 100, bottom: 120, left: 0, width: 10, height: 20 });

    const bar = screen.getByRole("toolbar");
    expect(Number(bar.style.left.replace("px", ""))).toBeGreaterThanOrEqual(8);
  });
});
