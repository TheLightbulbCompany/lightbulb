import { render, screen } from "@testing-library/react-native";
import { Text } from "react-native";

import { BottomSheet } from "../BottomSheet";

// The sheet itself is SwiftUI's (mocked in jest-setup.ts as an inline View that
// renders while presented). What is ours, and tested here, is the contract the
// six sheets rely on.

const sheetProps = () => screen.getByTestId("native-sheet").props;

describe("BottomSheet", () => {
  it("renders its children when visible", async () => {
    await render(
      <BottomSheet visible onClose={() => {}}>
        <Text>Sheet body</Text>
      </BottomSheet>,
    );
    expect(screen.getByText("Sheet body")).toBeTruthy();
  });

  // Several screens render a sheet per list row; a closed one mounts nothing,
  // not even the native host.
  it("mounts nothing until it is first opened", async () => {
    const screenApi = await render(
      <BottomSheet visible={false} onClose={() => {}}>
        <Text>Sheet body</Text>
      </BottomSheet>,
    );
    expect(screenApi.toJSON()).toBeNull();
  });

  it("does not render children when not visible", async () => {
    await render(
      <BottomSheet visible={false} onClose={() => {}}>
        <Text>Sheet body</Text>
      </BottomSheet>,
    );
    expect(screen.queryByText("Sheet body")).toBeNull();
  });

  it("calls onClose when the reader dismisses it", async () => {
    const onClose = jest.fn();
    await render(
      <BottomSheet visible onClose={onClose}>
        <Text>Sheet body</Text>
      </BottomSheet>,
    );
    sheetProps().onIsPresentedChange(false);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  // Closing from code is `visible={false}`; it must not echo back as a
  // reader dismissal (callers already cleared their own state).
  it("does not call onClose when closed from code", async () => {
    const onClose = jest.fn();
    const screenApi = await render(
      <BottomSheet visible onClose={onClose}>
        <Text>Sheet body</Text>
      </BottomSheet>,
    );
    await screenApi.rerender(
      <BottomSheet visible={false} onClose={onClose}>
        <Text>Sheet body</Text>
      </BottomSheet>,
    );
    expect(onClose).not.toHaveBeenCalled();
  });

  it("sizes to its content unless it is a large sheet", async () => {
    const screenApi = await render(
      <BottomSheet visible onClose={() => {}}>
        <Text>Sheet body</Text>
      </BottomSheet>,
    );
    expect(sheetProps().fitToContents).toBe(true);

    await screenApi.rerender(
      <BottomSheet visible large onClose={() => {}}>
        <Text>Sheet body</Text>
      </BottomSheet>,
    );
    expect(sheetProps().fitToContents).toBe(false);
  });
});
