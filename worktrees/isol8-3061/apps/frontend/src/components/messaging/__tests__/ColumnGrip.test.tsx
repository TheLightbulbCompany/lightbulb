import { useRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ColumnGrip } from "../ColumnGrip";
import { readSessionColumnWidth, useSessionColumnWidth, writeSessionColumnWidth } from "../sidebarPrefs";

it("reports pointer drags, keyboard nudges, and commits exactly once per release gesture", () => {
  const onDrag = vi.fn(), onNudge = vi.fn(), onCommit = vi.fn();
  render(<ColumnGrip onDrag={onDrag} onNudge={onNudge} onCommit={onCommit} value={600} min={360} max={800} />);
  const grip = screen.getByRole("separator", { name: "Resize session column" });
  grip.setPointerCapture = vi.fn(); grip.releasePointerCapture = vi.fn(); grip.hasPointerCapture = vi.fn(() => true);
  fireEvent.pointerDown(grip, { pointerId: 1, clientX: 900 });
  fireEvent.pointerMove(grip, { pointerId: 1, clientX: 850, buttons: 1 });
  expect(onDrag).toHaveBeenCalledWith(850);
  fireEvent.pointerUp(grip, { pointerId: 1 });
  expect(grip.releasePointerCapture).toHaveBeenCalledWith(1);
  // Releasing alone must not commit -- a real browser fires lostpointercapture
  // right after, and that is the single place the commit happens.
  expect(onCommit).not.toHaveBeenCalled();
  fireEvent.lostPointerCapture(grip, { pointerId: 1 });
  expect(onCommit).toHaveBeenCalledTimes(1);
  fireEvent.keyDown(grip, { key: "ArrowLeft" });
  expect(onNudge).toHaveBeenCalledWith(24);
  fireEvent.keyDown(grip, { key: "ArrowRight" });
  expect(onNudge).toHaveBeenCalledWith(-24);
});

it("commits exactly once when a captured pointer is cancelled", () => {
  // Per the Pointer Events spec, cancelling a captured pointer implicitly
  // releases capture right after pointercancel fires, which fires
  // lostpointercapture in turn -- jsdom does not cascade that second event,
  // so the test fires both explicitly to simulate what a real browser does.
  const onCommit = vi.fn();
  render(<ColumnGrip onDrag={vi.fn()} onNudge={vi.fn()} onCommit={onCommit} value={600} min={360} max={800} />);
  const grip = screen.getByRole("separator", { name: "Resize session column" });
  grip.setPointerCapture = vi.fn();
  fireEvent.pointerDown(grip, { pointerId: 1, clientX: 900 });
  fireEvent.pointerCancel(grip, { pointerId: 1 });
  fireEvent.lostPointerCapture(grip, { pointerId: 1 });
  expect(onCommit).toHaveBeenCalledTimes(1);
});

it("never calls releasePointerCapture once capture is already lost", () => {
  const onCommit = vi.fn();
  render(<ColumnGrip onDrag={vi.fn()} onNudge={vi.fn()} onCommit={onCommit} value={600} min={360} max={800} />);
  const grip = screen.getByRole("separator", { name: "Resize session column" });
  grip.setPointerCapture = vi.fn();
  grip.releasePointerCapture = vi.fn();
  grip.hasPointerCapture = vi.fn(() => false);
  fireEvent.pointerDown(grip, { pointerId: 1, clientX: 900 });
  // Capture was already lost (cancellation, or the OS took it) by the time
  // pointerup fires -- calling releasePointerCapture here throws in a real
  // browser, so it must be guarded.
  fireEvent.pointerUp(grip, { pointerId: 1 });
  expect(grip.releasePointerCapture).not.toHaveBeenCalled();
});

it("announces its current width and allowed range for assistive tech", () => {
  render(<ColumnGrip onDrag={vi.fn()} onNudge={vi.fn()} onCommit={vi.fn()} value={624.4} min={360} max={959.6} />);
  const grip = screen.getByRole("separator", { name: "Resize session column" });
  expect(grip).toHaveAttribute("aria-valuenow", "624");
  expect(grip).toHaveAttribute("aria-valuemin", "360");
  expect(grip).toHaveAttribute("aria-valuemax", "960");
});

function Harness({ scope }: { scope: string }) {
  const panelRef = useRef<HTMLElement>(null);
  const column = useSessionColumnWidth(scope, panelRef);
  return <section ref={panelRef}>
    <ColumnGrip {...column.grip} />
  </section>;
}

it("commits the width from the most recent keyboard nudge, not a stale pre-nudge render", () => {
  window.localStorage.clear();
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
    { width: 1200, right: 1200, left: 0, top: 0, bottom: 0, height: 0, x: 0, y: 0, toJSON() {} } as DOMRect,
  );
  render(<Harness scope="grip-scope" />);
  const grip = screen.getByRole("separator", { name: "Resize session column" });
  fireEvent.keyDown(grip, { key: "ArrowLeft" });
  fireEvent.keyDown(grip, { key: "ArrowLeft" });
  // panelWidth 1200 -> unset-width default of 600, then two +24 nudges: 648.
  // A stale-closure regression would persist 624 (the pre-second-nudge value).
  expect(readSessionColumnWidth("grip-scope")).toBe(648);
});

// This proves useSessionColumnWidth's own useState initializer reads the
// right scope's width on a fresh mount -- it supplies the `key` itself, so it
// does NOT cover RoomPanel's RoomBoundary key or SessionFocus's key={scope}
// actually remounting on a scope change; that is a property of those callers,
// not of this hook.
it("reads the width stored under whatever scope a fresh mount receives, and commits back under that same scope", () => {
  window.localStorage.clear();
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
    { width: 1200, right: 1200, left: 0, top: 0, bottom: 0, height: 0, x: 0, y: 0, toJSON() {} } as DOMRect,
  );
  writeSessionColumnWidth("scope-a", 500);
  writeSessionColumnWidth("scope-b", 700);
  const view = render(<Harness key="scope-a" scope="scope-a" />);
  expect(screen.getByRole("separator", { name: "Resize session column" })).toHaveAttribute("aria-valuenow", "500");

  view.rerender(<Harness key="scope-b" scope="scope-b" />);
  const grip = screen.getByRole("separator", { name: "Resize session column" });
  expect(grip).toHaveAttribute("aria-valuenow", "700");

  // Committing now must land under the NEW scope, not the carried-over width
  // clobbering scope-a's stored value.
  fireEvent.keyDown(grip, { key: "ArrowLeft" });
  expect(readSessionColumnWidth("scope-b")).toBe(724);
  expect(readSessionColumnWidth("scope-a")).toBe(500);
});
