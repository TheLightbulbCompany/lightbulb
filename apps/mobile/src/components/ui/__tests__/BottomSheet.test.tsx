import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { Text } from "react-native";
import { fireGestureHandler, getByGestureTestId } from "react-native-gesture-handler/jest-utils";
import * as Reanimated from "react-native-reanimated";

import { BottomSheet } from "../BottomSheet";

describe("BottomSheet", () => {
  it("renders its children when visible", async () => {
    await render(
      <BottomSheet visible onClose={() => {}} closeLabel="Close sheet">
        <Text>Sheet body</Text>
      </BottomSheet>,
    );
    expect(screen.getByText("Sheet body")).toBeTruthy();
  });

  it("does not render children when not visible", async () => {
    await render(
      <BottomSheet visible={false} onClose={() => {}} closeLabel="Close sheet">
        <Text>Sheet body</Text>
      </BottomSheet>,
    );
    expect(screen.queryByText("Sheet body")).toBeNull();
  });

  it("calls onClose when the scrim is pressed", async () => {
    const onClose = jest.fn();
    await render(
      <BottomSheet visible onClose={onClose} closeLabel="Close sheet">
        <Text>Sheet body</Text>
      </BottomSheet>,
    );
    fireEvent.press(screen.getByLabelText("Close sheet"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("plays the exit animation before calling onClose", async () => {
    // The default reanimated jest mock resolves withSpring's callback
    // synchronously (no real animation), which can't show ordering. Override
    // it for this one test so the spring's completion is a manual step —
    // proving BottomSheet defers `onClose` to that step, not to the `visible`
    // prop flip itself.
    const pendingFinishers: Array<() => void> = [];
    const withSpringSpy = jest
      .spyOn(Reanimated, "withSpring")
      .mockImplementation((...args: Parameters<typeof Reanimated.withSpring>) => {
        const [toValue, , callback] = args;
        if (callback) pendingFinishers.push(() => callback(true));
        return toValue;
      });
    try {
      const onClose = jest.fn();
      const { rerender } = await render(
        <BottomSheet visible onClose={onClose} closeLabel="Close sheet">
          <Text>Sheet body</Text>
        </BottomSheet>,
      );
      await rerender(
        <BottomSheet visible={false} onClose={onClose} closeLabel="Close sheet">
          <Text>Sheet body</Text>
        </BottomSheet>,
      );
      // The close spring was started but hasn't reported `finished` yet.
      expect(onClose).not.toHaveBeenCalled();
      await act(async () => {
        pendingFinishers.forEach((finish) => finish());
      });
      expect(onClose).toHaveBeenCalledTimes(1);
    } finally {
      withSpringSpy.mockRestore();
    }
  });

  it("a fast downward fling dismisses even from a small drag", async () => {
    const onClose = jest.fn();
    await render(
      <BottomSheet visible onClose={onClose} closeLabel="Close sheet">
        <Text>Sheet body</Text>
      </BottomSheet>,
    );
    await act(async () => {
      fireGestureHandler(getByGestureTestId("bottom-sheet-pan"), [
        { translationY: 10, velocityY: 0 },
        { translationY: 40, velocityY: 1200 },
        { translationY: 40, velocityY: 1400 },
      ]);
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("a slow drag below the distance threshold snaps back", async () => {
    const onClose = jest.fn();
    await render(
      <BottomSheet visible onClose={onClose} closeLabel="Close sheet">
        <Text>Sheet body</Text>
      </BottomSheet>,
    );
    await act(async () => {
      fireGestureHandler(getByGestureTestId("bottom-sheet-pan"), [
        { translationY: 10, velocityY: 0 },
        { translationY: 30, velocityY: 40 },
        { translationY: 40, velocityY: 50 },
      ]);
    });
    expect(onClose).not.toHaveBeenCalled();
    // Still open — its content stays queryable.
    expect(screen.getByText("Sheet body")).toBeTruthy();
  });
});
