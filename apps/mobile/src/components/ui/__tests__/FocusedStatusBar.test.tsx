import { render, screen } from "@testing-library/react-native";

let mockFocused = true;

jest.mock("expo-router", () => ({
  useIsFocused: () => mockFocused,
}));

jest.mock("expo-status-bar", () => {
  const { View } = require("react-native");
  return {
    StatusBar: ({ style }: { style: string }) => <View testID="status-bar" statusBarStyle={style} />,
  };
});

import { FocusedStatusBar } from "../FocusedStatusBar";

describe("FocusedStatusBar", () => {
  afterEach(() => {
    mockFocused = true;
  });

  it("renders the auto status bar while focused", async () => {
    mockFocused = true;
    await render(<FocusedStatusBar />);
    expect(screen.getByTestId("status-bar").props.statusBarStyle).toBe("auto");
  });

  it("renders nothing while not focused", async () => {
    mockFocused = false;
    await render(<FocusedStatusBar />);
    expect(screen.queryByTestId("status-bar")).toBeNull();
  });
});
