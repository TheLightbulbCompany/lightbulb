import { render, screen } from "@testing-library/react-native";
import { StyleSheet } from "react-native";

import { chatPalette } from "../../../config/theme";
import { MessageMarkdown } from "../MessageMarkdown";

jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));

const color = (text: string) => StyleSheet.flatten(screen.getByText(text).props.style).color;

it("draws body text at 16pt in the palette ink", async () => {
  await render(<MessageMarkdown text="hello there" palette={chatPalette.dark} />);
  const style = StyleSheet.flatten(screen.getByText("hello there").props.style);
  expect(style.fontSize).toBe(16);
  expect(style.color).toBe(chatPalette.dark.fg);
});

it("inverts for a mine bubble, links and inline code included", async () => {
  await render(<MessageMarkdown text={"see [docs](https://x.dev) and `npm test`"} palette={chatPalette.light} inverse />);
  expect(color("docs")).toBe(chatPalette.light.onFg);
  const code = StyleSheet.flatten(screen.getByText("npm test").props.style);
  expect(code.backgroundColor).toBe(chatPalette.light.fgSoft);
  expect(code.color).toBe(chatPalette.light.onFg);
});

it("labels a fence in the sans face and keeps the code mono", async () => {
  await render(<MessageMarkdown text={"```bash\ngh pr view 3284\n```"} palette={chatPalette.light} />);
  expect(StyleSheet.flatten(screen.getByText("bash").props.style).fontFamily).toBe("SourceSans3_500Medium");
  expect(StyleSheet.flatten(screen.getByText("gh pr view 3284").props.style).fontFamily).toBe("Menlo");
});

// A ScrollView grows by default. In a bubble sized to its content, a code block
// that grew made the message tens of thousands of points tall on iOS: a blank
// session screen (prod, "Demo video with Remotion"). The layout is native, so
// the check is on the style that prevents it.
it("never lets a code block's scroller grow its bubble", async () => {
  await render(<MessageMarkdown text={"- start it:\n  ```bash\n  python3 serve.py\n  ```"} palette={chatPalette.dark} />);
  let node = screen.getByText("python3 serve.py").parent;
  while (node && !node.props.horizontal) node = node.parent;
  expect(StyleSheet.flatten(node?.props.style).flexGrow).toBe(0);
});

it("keeps the legacy look when no palette is passed", async () => {
  await render(<MessageMarkdown text="legacy" />);
  expect(StyleSheet.flatten(screen.getByText("legacy").props.style).fontSize).toBe(15);
});
