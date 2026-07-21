import { fireEvent, render, screen } from "@testing-library/react-native";
import { Text } from "react-native";

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
});
