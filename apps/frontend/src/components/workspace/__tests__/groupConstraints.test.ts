// Pins the dock-group height floor. Two distinct behaviours, because
// setConstraints alone provably cannot repair an already-squeezed layout:
// dockview's gridviewPanel applies the new _minimumHeight without relayouting,
// so a group restored from localStorage at 63px stays at 63px until something
// else resizes it.
import { describe, it, expect, vi } from "vitest";
import {
  constrainGroupHeight,
  MIN_GROUP_HEIGHT,
  type ConstrainableGroup,
} from "../groupConstraints";

function fakeGroup(height: number) {
  const setConstraints = vi.fn();
  const setSize = vi.fn();
  const group: ConstrainableGroup = { api: { height, setConstraints, setSize } };
  return { group, setConstraints, setSize };
}

describe("constrainGroupHeight", () => {
  it("floors every group so a sash drag can't crush it to its tab bar", () => {
    const { group, setConstraints } = fakeGroup(400);
    constrainGroupHeight(group);
    expect(setConstraints).toHaveBeenCalledWith({
      minimumHeight: MIN_GROUP_HEIGHT,
    });
  });

  it("leaves a healthy group's size alone", () => {
    const { group, setSize } = fakeGroup(400);
    constrainGroupHeight(group);
    expect(setSize).not.toHaveBeenCalled();
  });

  it("repairs a group that restored below the floor", () => {
    // The observed bug: a 63px slot holding 2046px of content.
    const { group, setSize } = fakeGroup(63);
    constrainGroupHeight(group);
    expect(setSize).toHaveBeenCalledWith({ height: MIN_GROUP_HEIGHT });
  });

  it("leaves room for the 35px tab bar plus usable content", () => {
    expect(MIN_GROUP_HEIGHT).toBeGreaterThan(35 + 40);
  });
});
