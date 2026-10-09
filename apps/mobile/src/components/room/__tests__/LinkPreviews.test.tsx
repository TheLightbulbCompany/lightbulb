import { fireEvent, render, screen } from "@testing-library/react-native";
import { Linking } from "react-native";

import { LinkPreviews } from "../LinkPreviews";

jest.mock("@clerk/expo", () => ({ useAuth: () => ({ getToken: async () => "t" }) }));
// SWR never fetches under jest-expo (no `window`, so it thinks it is a server render);
// the hook's fetch is covered in packages/shared. Here: which URLs reach it, and the card.
const mockGet = jest.fn();
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({}),
  useLinkPreview: (_api: unknown, url: string) => mockGet(url),
}));

const article = {
  url: "https://news.example.com/a",
  kind: "link",
  title: "Big News",
  description: "A short summary.",
  siteName: "The Paper",
  imageUrl: "https://news.example.com/hero.jpg",
  author: null,
  handle: null,
  avatarUrl: null,
  publishedAt: null,
  tweet: null,
};

function renderText(text: string) {
  return render(<LinkPreviews text={text} />);
}

describe("LinkPreviews", () => {
  beforeEach(() => mockGet.mockReset());

  it("renders a link card and opens the URL on tap", async () => {
    mockGet.mockReturnValue(article);
    const open = jest.spyOn(Linking, "openURL").mockResolvedValue(true);
    await renderText("see https://news.example.com/a");
    expect(await screen.findByText("Big News")).toBeTruthy();
    expect(screen.getByText("A short summary.")).toBeTruthy();
    expect(screen.getByText("The Paper")).toBeTruthy();
    expect(mockGet).toHaveBeenCalledWith("https://news.example.com/a");
    fireEvent.press(screen.getByTestId("link-preview"));
    expect(open).toHaveBeenCalledWith("https://news.example.com/a");
  });

  it("renders a tweet card with author, handle, text and date", async () => {
    mockGet.mockReturnValue({
      ...article,
      url: "https://x.com/jack/status/20",
      kind: "tweet",
      title: "jack",
      author: "jack",
      handle: "jack",
      description: "just setting up my twttr",
      siteName: "X",
      imageUrl: null,
      avatarUrl: "https://pbs.twimg.com/a_bigger.jpg",
      publishedAt: "2006-03-21T20:50:14.000Z",
    });
    await renderText("https://x.com/jack/status/20");
    expect(await screen.findByText("just setting up my twttr")).toBeTruthy();
    expect(screen.getByText("@jack")).toBeTruthy();
    expect(screen.getByText(/^X · /)).toBeTruthy();
  });

  it("renders nothing when the fetch fails (e.g. an older backend) or there is no URL", async () => {
    mockGet.mockReturnValue(undefined);
    await renderText("https://nothing.example/");
    expect(screen.queryByTestId("link-preview")).toBeNull();
    mockGet.mockClear();
    await renderText("plain words");
    expect(mockGet).not.toHaveBeenCalled();
  });
});
