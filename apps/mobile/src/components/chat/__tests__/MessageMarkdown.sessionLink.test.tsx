import { fireEvent, render, screen } from "@testing-library/react-native";
import { router } from "expo-router";

import { chatPalette } from "../../../config/theme";
import { MessageMarkdown } from "../MessageMarkdown";

jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));

it("opens a room reply's session permalink on the native session screen", async () => {
  await render(<MessageMarkdown text="[Open session](https://thelightbulb.company/chat?panel=session&session=s1)"
    palette={chatPalette.light} />);
  fireEvent.press(screen.getByText("Open session"));
  expect(router.push).toHaveBeenCalledWith({ pathname: "/(app)/session/[sessionId]", params: { sessionId: "s1" } });
});

it("opens an agent's sign-in ask on the Activity tab, not in Safari", async () => {
  const focus = "login:0123456789abcdef0123456789abcdef";
  await render(<MessageMarkdown text={`Add it here: https://thelightbulb.company/chat?panel=activity&focus=${focus}`}
    palette={chatPalette.light} />);
  fireEvent.press(screen.getByText(/panel=activity/));
  expect(router.push).toHaveBeenCalledWith({ pathname: "/(app)/(tabs)/activity", params: { focus } });
});
