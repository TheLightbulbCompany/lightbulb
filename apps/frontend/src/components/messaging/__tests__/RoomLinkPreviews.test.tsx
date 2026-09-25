import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { SWRConfig } from "swr";
import { beforeEach, describe, expect, it, vi } from "vitest";

const m = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: m.get }) }));

import { RoomLinkPreviews } from "../RoomLinkPreviews";

const article = {
  url: "https://news.example.com/a",
  kind: "link",
  title: "Big News",
  description: "A short summary.",
  siteName: "The Paper",
  imageUrl: "https://news.example.com/hero.jpg",
  author: null, handle: null, avatarUrl: null, publishedAt: null, tweet: null,
};

const tweet = {
  url: "https://x.com/jack/status/20",
  kind: "tweet",
  title: "jack",
  description: "just setting up my twttr",
  siteName: "X",
  imageUrl: null,
  author: "jack",
  handle: "jack",
  avatarUrl: "https://pbs.twimg.com/a_bigger.jpg",
  publishedAt: "2006-03-21T20:50:14.000Z",
  tweet: null,
};

function renderText(text: string) {
  return render(<SWRConfig value={{ provider: () => new Map() }}><RoomLinkPreviews text={text} /></SWRConfig>);
}

describe("RoomLinkPreviews", () => {
  beforeEach(() => m.get.mockReset());

  it("renders a link card with image, title, description and site", async () => {
    m.get.mockResolvedValue(article);
    renderText("read this https://news.example.com/a.");
    const link = await screen.findByRole("link", { name: /Big News/ });
    expect(link).toHaveAttribute("href", "https://news.example.com/a");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText("A short summary.")).toBeInTheDocument();
    expect(screen.getByText("The Paper")).toBeInTheDocument();
    expect(m.get).toHaveBeenCalledWith("/messaging/link-preview?url=https%3A%2F%2Fnews.example.com%2Fa");
    const image = link.querySelector("img")!;
    expect(image).toHaveAttribute("loading", "lazy");
    fireEvent.error(image);
    expect(link.querySelector("img")).toBeNull();
  });

  it("renders a tweet card from the normalized fields when no raw tweet came back", async () => {
    m.get.mockResolvedValue(tweet);
    renderText("https://x.com/jack/status/20");
    expect(await screen.findByText("just setting up my twttr")).toBeInTheDocument();
    expect(screen.getByText("@jack")).toBeInTheDocument();
    expect(screen.getByText(/X · Mar 2\d, 2006/)).toBeInTheDocument();
  });

  it("renders react-tweet's card from raw syndication data, with no script injected", async () => {
    m.get.mockResolvedValue({
      ...tweet,
      tweet: {
        __typename: "Tweet", id_str: "20", lang: "en", created_at: "2006-03-21T20:50:14.000Z",
        text: "just setting up my twttr", display_text_range: [0, 24], entities: {},
        favorite_count: 1, conversation_count: 0, news_action_type: "conversation", isEdited: false, isStaleEdit: false,
        edit_control: { edit_tweet_ids: ["20"], editable_until_msecs: "0", is_edit_eligible: false, edits_remaining: "0" },
        user: { id_str: "12", name: "jack", screen_name: "jack", profile_image_url_https: "https://pbs.twimg.com/a_normal.jpg",
          profile_image_shape: "Circle", verified: false, is_blue_verified: false },
      },
    });
    const { container } = renderText("https://x.com/jack/status/20");
    expect(await screen.findByText("just setting up my twttr")).toBeInTheDocument();
    expect(container.querySelector(".react-tweet-theme")).not.toBeNull();
    expect(container.querySelector("script, iframe")).toBeNull();
  });

  it("renders nothing when the preview fetch fails, and never fetches without a URL", async () => {
    m.get.mockRejectedValue(new Error("404"));
    const { container } = renderText("https://nothing.example/");
    await waitFor(() => expect(m.get).toHaveBeenCalledTimes(1));
    expect(container.querySelector(".room-link-card")).toBeNull();
    m.get.mockClear();
    const plain = renderText("no links here");
    expect(plain.container).toBeEmptyDOMElement();
    expect(m.get).not.toHaveBeenCalled();
  });

  it("caps previews at two URLs", async () => {
    m.get.mockResolvedValue(article);
    renderText("https://a.example/ https://b.example/ https://c.example/");
    await waitFor(() => expect(m.get).toHaveBeenCalledTimes(2));
  });
});
