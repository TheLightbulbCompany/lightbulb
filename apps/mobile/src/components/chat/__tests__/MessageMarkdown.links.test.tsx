import { fireEvent, render, screen } from "@testing-library/react-native";
import { Linking } from "react-native";

import { MessageMarkdown } from "../MessageMarkdown";

describe("MessageMarkdown selection and links", () => {
  let openURL: jest.SpyInstance;
  beforeEach(() => {
    openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(true as never);
  });
  afterEach(() => {
    openURL.mockRestore();
  });

  it("renders prose as one native-selectable text tree across inline markdown", async () => {
    await render(<MessageMarkdown text="Select **only these words** from this response." />);

    expect(screen.getByText("Select only these words from this response.").props.selectable).toBe(true);
  });

  it("keeps fenced code content native-selectable", async () => {
    await render(<MessageMarkdown text={'```ts\nconst answer = 42;\n```'} />);

    expect(screen.getByText("const answer = 42;").props.selectable).toBe(true);
  });

  it("opens an explicit markdown link on press", async () => {
    await render(<MessageMarkdown text="Read the [docs](https://example.com/docs)." />);
    fireEvent.press(screen.getByText("docs"));
    expect(openURL).toHaveBeenCalledWith("https://example.com/docs");
  });

  it("renders a bare URL as a tappable link (linkify)", async () => {
    // Agents post links as bare URLs constantly; with linkify off they render
    // as dead text — the "pressing links does nothing" bug.
    await render(<MessageMarkdown text="Deployed: https://example.com/run/42 done" />);
    fireEvent.press(screen.getByText("https://example.com/run/42"));
    expect(openURL).toHaveBeenCalledWith("https://example.com/run/42");
  });
});
