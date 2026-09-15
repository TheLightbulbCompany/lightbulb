import { fireEvent, render, screen } from "@testing-library/react-native";

// Lazy reference: babel lowers `const mockPush` to `var`, and this factory runs
// while the screen's own import is still resolving.
const mockPush = jest.fn();
jest.mock("expo-router", () => ({
  router: { push: (...args: unknown[]) => mockPush(...args) },
}));

import More from "../more";

// These route trees lost their tab when the bar went Slack-shaped
// (Home · DMs · Activity · More), so this screen is the only way in.
it("pushes the route trees the tab bar no longer shows", async () => {
  await render(<More />);
  for (const [label, route] of [
    ["Teams", "/(app)/teams"],
    ["Shared sessions", "/(app)/sessions"],
    ["Control", "/(app)/control"],
    ["Settings", "/(app)/settings"],
  ] as const) {
    mockPush.mockClear();
    await fireEvent.press(screen.getByLabelText(label));
    expect(mockPush).toHaveBeenCalledWith(route);
  }
});
