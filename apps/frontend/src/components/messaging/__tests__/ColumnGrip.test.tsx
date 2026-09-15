import { useRef } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ColumnGrip } from "../ColumnGrip";
import { readSidebarPrefs, useSessionColumnWidth } from "../sidebarPrefs";

it("reports pointer drags, keyboard nudges, and commits on release", () => {
  const onDrag = vi.fn(), onNudge = vi.fn(), onCommit = vi.fn();
  render(<ColumnGrip onDrag={onDrag} onNudge={onNudge} onCommit={onCommit} />);
  const grip = screen.getByRole("separator", { name: "Resize session column" });
  grip.setPointerCapture = vi.fn(); grip.releasePointerCapture = vi.fn();
  fireEvent.pointerDown(grip, { pointerId: 1, clientX: 900 });
  fireEvent.pointerMove(grip, { pointerId: 1, clientX: 850, buttons: 1 });
  expect(onDrag).toHaveBeenCalledWith(850);
  fireEvent.pointerUp(grip, { pointerId: 1 });
  expect(onCommit).toHaveBeenCalledTimes(1);
  fireEvent.keyDown(grip, { key: "ArrowLeft" });
  expect(onNudge).toHaveBeenCalledWith(24);
  fireEvent.keyDown(grip, { key: "ArrowRight" });
  expect(onNudge).toHaveBeenCalledWith(-24);
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
    <ColumnGrip onDrag={column.onDrag} onNudge={column.onNudge} onCommit={column.onCommit} value={column.value} min={column.min} max={column.max} />
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
  expect(readSidebarPrefs("grip-scope").sessionColumnWidth).toBe(648);
});
