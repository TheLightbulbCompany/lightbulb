import { fireEvent, render, screen } from "@testing-library/react-native";
import { Linking } from "react-native";
import { router } from "expo-router";

import { MessageMarkdown } from "../MessageMarkdown";

jest.mock("expo-router", () => ({ router: { push: jest.fn() } }));

describe("MessageMarkdown selection and links", () => {
  let openURL: jest.SpyInstance;
  beforeEach(() => {
    openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(true as never);
    (router.push as jest.Mock).mockClear();
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

  it("routes an agent-written Paperclip issue link to the in-app issue screen", async () => {
    // The bundled paperclip skill writes links like `/ISOA/issues/ISOA-317` —
    // mobile has an in-app screen for these, so it should not round-trip
    // through the browser and the web's middleware redirect.
    await render(<MessageMarkdown text="See [ISOA-317](/ISOA/issues/ISOA-317)" />);
    fireEvent.press(screen.getByText("ISOA-317"));
    expect(router.push).toHaveBeenCalledWith({
      pathname: "/(app)/teams/issue/[id]",
      params: { id: "ISOA-317" },
    });
    expect(openURL).not.toHaveBeenCalled();
  });

  it("routes a dropped-prefix issue link to the in-app issue screen too", async () => {
    await render(<MessageMarkdown text="See [ISOA-317](/issues/ISOA-317)" />);
    fireEvent.press(screen.getByText("ISOA-317"));
    expect(router.push).toHaveBeenCalledWith({
      pathname: "/(app)/teams/issue/[id]",
      params: { id: "ISOA-317" },
    });
  });

  it("routes an agent-written run link to the in-app run screen", async () => {
    await render(<MessageMarkdown text="See [run](/ISOA/agents/cto/runs/run-9)" />);
    fireEvent.press(screen.getByText("run"));
    expect(router.push).toHaveBeenCalledWith({
      pathname: "/(app)/teams/run/[runId]",
      params: { runId: "run-9" },
    });
  });

  it("resolves an agent link (no mobile detail screen yet) against the web origin", async () => {
    // Linking.openURL can't open a schemeless path on its own — the bundled
    // paperclip skill writes links like `/ISOA/agents/cto`. There is no
    // mobile agent-inspector screen, so this one keeps the web fallback.
    await render(<MessageMarkdown text="See [CTO](/ISOA/agents/cto)" />);
    fireEvent.press(screen.getByText("CTO"));
    expect(openURL).toHaveBeenCalledWith("https://thelightbulb.company/ISOA/agents/cto");
    expect(router.push).not.toHaveBeenCalled();
  });
});
