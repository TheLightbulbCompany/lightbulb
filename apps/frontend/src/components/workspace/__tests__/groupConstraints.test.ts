// Pins the dock-group floors on BOTH axes. Two distinct behaviours per axis,
// because setConstraints alone provably cannot repair an already-squeezed
// layout: dockview's gridviewPanel applies the new minimum without
// relayouting, so a group restored from localStorage at 63px stays at 63px
// until something else resizes it.
import { describe, it, expect, vi } from "vitest";
import {
  constrainGroup,
  constrainGroupHeight,
  MIN_GROUP_HEIGHT,
  MIN_GROUP_WIDTH,
  type ConstrainableGroup,
} from "../groupConstraints";

function fakeGroup(height: number, width = 800) {
  const setConstraints = vi.fn();
  const setSize = vi.fn();
  const group: ConstrainableGroup = { api: { height, width, setConstraints, setSize } };
  return { group, setConstraints, setSize };
}

describe("constrainGroup", () => {
  it("floors every group on both axes so a sash drag can't crush it", () => {
    const { group, setConstraints } = fakeGroup(400, 800);
    constrainGroup(group);
    expect(setConstraints).toHaveBeenCalledWith({
      minimumHeight: MIN_GROUP_HEIGHT,
      minimumWidth: MIN_GROUP_WIDTH,
    });
  });

  it("leaves a healthy group's size alone", () => {
    const { group, setSize } = fakeGroup(400, 800);
    constrainGroup(group);
    expect(setSize).not.toHaveBeenCalled();
  });

  it("repairs a group that restored below the height floor", () => {
    // The observed bug: a 63px slot holding 2046px of content.
    const { group, setSize } = fakeGroup(63, 800);
    constrainGroup(group);
    expect(setSize).toHaveBeenCalledWith({ height: MIN_GROUP_HEIGHT });
  });

  it("repairs a group that restored below the width floor", () => {
    const { group, setSize } = fakeGroup(400, 120);
    constrainGroup(group);
    expect(setSize).toHaveBeenCalledWith({ width: MIN_GROUP_WIDTH });
  });

  it("repairs both axes in one call when both are short", () => {
    const { group, setSize } = fakeGroup(63, 120);
    constrainGroup(group);
    expect(setSize).toHaveBeenCalledTimes(1);
    expect(setSize).toHaveBeenCalledWith({
      height: MIN_GROUP_HEIGHT,
      width: MIN_GROUP_WIDTH,
    });
  });

  it("never resizes a healthy axis while repairing the other", () => {
    // Sending an already-healthy axis would hand it a size it never asked
    // for, and dockview would redistribute from its siblings to honour it.
    const { group, setSize } = fakeGroup(400, 120);
    constrainGroup(group);
    expect(setSize.mock.calls[0][0]).not.toHaveProperty("height");
  });

  it("leaves room for the 35px tab bar plus usable content", () => {
    expect(MIN_GROUP_HEIGHT).toBeGreaterThan(35 + 40);
  });

  it("keeps the width floor wide enough for panel chrome plus a column", () => {
    expect(MIN_GROUP_WIDTH).toBeGreaterThanOrEqual(280);
  });
});

describe("constrainGroupHeight (back-compat alias)", () => {
  it("still applies both bounds so existing call sites gain the width floor", () => {
    const { group, setConstraints } = fakeGroup(400, 800);
    constrainGroupHeight(group);
    expect(setConstraints).toHaveBeenCalledWith({
      minimumHeight: MIN_GROUP_HEIGHT,
      minimumWidth: MIN_GROUP_WIDTH,
    });
  });
});
