import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ColumnGrip } from "../ColumnGrip";

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
