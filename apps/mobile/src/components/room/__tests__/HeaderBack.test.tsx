import { fireEvent, render, screen } from "@testing-library/react-native";

// `mock`-prefixed and referenced lazily, the way every suite on this surface
// does it — babel-plugin-jest-hoist rejects any other out-of-scope name.
const mockBack = jest.fn();
const mockReplace = jest.fn();
const mockCanGoBack = jest.fn(() => true);
jest.mock("expo-router", () => ({
  router: {
    back: () => mockBack(),
    replace: (href: string) => mockReplace(href),
    canGoBack: () => mockCanGoBack(),
  },
}));

import { HeaderBack } from "../HeaderBack";

describe("HeaderBack", () => {
  beforeEach(() => {
    mockBack.mockClear();
    mockReplace.mockClear();
    mockCanGoBack.mockReturnValue(true);
  });

  it("pops history when there is any", async () => {
    await render(<HeaderBack fallback="/(app)/home" />);
    await fireEvent.press(screen.getByLabelText("Back"));
    expect(mockBack).toHaveBeenCalled();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it("falls back when this screen cold-started the app", async () => {
    mockCanGoBack.mockReturnValue(false);
    await render(<HeaderBack fallback="/(app)/room/room-1" />);
    await fireEvent.press(screen.getByLabelText("Back"));
    expect(mockReplace).toHaveBeenCalledWith("/(app)/room/room-1");
    expect(mockBack).not.toHaveBeenCalled();
  });
});
