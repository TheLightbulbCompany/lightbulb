import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { describe, expect, it, vi } from "vitest";
import type { RoomDetail } from "@lightbulb/shared";
import { loadFeedPosts, TeamFeed, type FeedClient, type TeamFeedProps } from "../TeamFeed";
import { post } from "./fixtures";

vi.mock("@/components/messaging/RoomDocumentCards", () => ({ RoomDocumentCards: () => null }));
const live = vi.hoisted(() => ({ value: null as null | { frontAgentId?: string } }));
vi.mock("@/hooks/useLiveEvents", () => ({ useOptionalLiveEvents: () => live.value }));

const room = {
  roomId: "feed-1", topic: "Keep us posted on money.", lastMessageSeq: 3, lastReadSeq: 1, feed: true,
} as RoomDetail;

function setup(over: Partial<TeamFeedProps> = {}) {
  const today = new Date().toISOString();
  const client = {
    getFeed: vi.fn().mockResolvedValue({ roomId: "feed-1", topic: "Keep us posted on money." }),
    getRoom: vi.fn().mockResolvedValue(room),
    listMessages: vi.fn().mockResolvedValue({ messages: [
      post(1, { createdAt: today }),
      post(2, { createdAt: today, postReason: "Money in is the first thing the brief asks for." }),
      post(3, { createdAt: today, text: "**Acme paid**\nStripe confirmed it." }),
      post(4, { createdAt: today, threadRootMessageId: "post-3", author: { kind: "human", id: "u1", displayName: "Ann" } }),
    ], nextAfterSeq: null }),
    markRead: vi.fn().mockResolvedValue({ roomId: "feed-1", lastReadSeq: 3 }),
    updateRoom: vi.fn().mockResolvedValue({ ...room, topic: "Only money." }),
    setReaction: vi.fn().mockImplementation(async () => post(3, { createdAt: today, text: "**Acme paid**\nStripe confirmed it.", reactions: { "👍": ["me"] } })),
    downloadAttachment: vi.fn(),
    attachmentUrl: vi.fn(),
  } satisfies FeedClient;
  const onDiscuss = vi.fn();
  const tree = (props: Partial<TeamFeedProps>) => <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
    <TeamFeed client={client} scope="org-1" currentUserId="me" onDiscuss={onDiscuss} {...over} {...props} />
  </SWRConfig>;
  const view = render(tree({}));
  return { client, onDiscuss, rerender: (props: Partial<TeamFeedProps>) => view.rerender(tree(props)) };
}

const posts = () => screen.getAllByRole("article");

describe("TeamFeed", () => {
  it("shows top-level posts newest first, the new ones under New since you looked, with a bold title", async () => {
    const { client } = setup();
    await waitFor(() => expect(posts()).toHaveLength(3));
    expect(client.listMessages).toHaveBeenCalledWith("feed-1", 0, 100, { beforeSeq: 4 });
    expect(posts().map((article) => article.querySelector(".feed-post__title")?.textContent)).toEqual(["Acme paid", "Post 2 title", "Post 1 title"]);
    const labels = [...document.querySelectorAll(".team-feed__divider")].map((divider) => divider.textContent);
    expect(labels).toEqual(["New since you looked", "Today"]);
    // The divider sits above the two posts after the reader's cursor (seq 1).
    const list = document.querySelector(".team-feed__list")!;
    expect([...list.children].map((li) => li.textContent?.includes("New since") ? "new" : li.querySelector("article") ? "post" : "day"))
      .toEqual(["new", "post", "post", "day", "post"]);
    // Viewing the feed moves its read cursor; nothing else counts feed posts.
    await waitFor(() => expect(client.markRead).toHaveBeenCalledWith("feed-1", { lastReadSeq: 3 }));
  });

  it("super-agent on: the front agent's older posts (seat name 'Finance' here) read Lightbulb", async () => {
    live.value = { frontAgentId: "agent-fin" };
    try {
      setup();
      await waitFor(() => expect(posts().length).toBeGreaterThan(0));
      expect(screen.getAllByText("Lightbulb").length).toBeGreaterThan(0);
      expect(screen.queryByText("Finance")).toBeNull();
    } finally { live.value = null; }
  });

  it("renders a title line with bold markdown as bold text, never asterisks", async () => {
    const { client } = setup();
    client.listMessages.mockResolvedValue({ messages: [
      post(1, { createdAt: new Date().toISOString(), text: "**This is our feed.** When something here matters, we post it.\nMore." }),
    ], nextAfterSeq: null });
    client.getRoom.mockResolvedValue({ ...room, lastMessageSeq: 1 });
    setup({ client, scope: "org-2" });
    const title = await waitFor(() => {
      const found = [...document.querySelectorAll(".feed-post__title")].find((el) => el.textContent?.includes("This is our feed."));
      expect(found).toBeTruthy();
      return found!;
    });
    expect(title.textContent).not.toContain("**");
    expect(title.querySelector("strong")).toHaveTextContent("This is our feed.");
  });

  it("keeps Why I created this post closed until the info button opens it", async () => {
    setup();
    await waitFor(() => expect(posts()).toHaveLength(3));
    const second = posts()[1];
    expect(within(second).queryByText("Money in is the first thing the brief asks for.")).not.toBeInTheDocument();
    expect(within(posts()[0]).queryByRole("button", { name: "Why I created this post" })).not.toBeInTheDocument();
    await userEvent.click(within(second).getByRole("button", { name: "Why I created this post" }));
    expect(within(second).getByText("Money in is the first thing the brief asks for.")).toBeInTheDocument();
  });

  it("opens Discuss on the post", async () => {
    const { onDiscuss } = setup();
    await waitFor(() => expect(posts()).toHaveLength(3));
    await userEvent.click(within(posts()[0]).getByRole("button", { name: "Discuss" }));
    expect(onDiscuss).toHaveBeenCalledWith(expect.objectContaining({ messageId: "post-3", roomId: "feed-1" }));
  });

  it("likes a post with the room reaction", async () => {
    const { client } = setup();
    await waitFor(() => expect(posts()).toHaveLength(3));
    await userEvent.click(within(posts()[0]).getByRole("button", { name: "Like" }));
    expect(client.setReaction).toHaveBeenCalledWith("feed-1", "post-3", "👍", true);
    expect(await within(posts()[0]).findByRole("button", { name: "Unlike, 1" })).toHaveAttribute("aria-pressed", "true");
  });

  it("edits the brief through the room topic", async () => {
    const { client } = setup();
    expect(await screen.findByText("Keep us posted on money.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Edit the brief" }));
    const field = screen.getByRole("textbox", { name: "Brief" });
    await userEvent.clear(field);
    await userEvent.type(field, "Only money.");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(client.updateRoom).toHaveBeenCalledWith("feed-1", { topic: "Only money." });
    expect(await screen.findByText("Only money.")).toBeInTheDocument();
  });

  it("leaves the read cursor alone while the feed column is hidden (the For you tab on a narrow panel)", async () => {
    const { client, rerender } = setup({ active: false });
    await waitFor(() => expect(posts()).toHaveLength(3));
    expect(client.markRead).not.toHaveBeenCalled();
    rerender({ active: true });
    await waitFor(() => expect(client.markRead).toHaveBeenCalledWith("feed-1", { lastReadSeq: 3 }));
  });

  it("pages back past Discuss replies until it has enough top-level posts", async () => {
    const reply = (seq: number) => post(seq, { threadRootMessageId: "post-1" });
    const listMessages = vi.fn()
      .mockResolvedValueOnce({ messages: [post(200), ...Array.from({ length: 99 }, (_, i) => reply(201 + i))], nextAfterSeq: null, nextBeforeSeq: 200 })
      .mockResolvedValueOnce({ messages: Array.from({ length: 40 }, (_, i) => post(150 + i)), nextAfterSeq: null, nextBeforeSeq: 150 })
      .mockResolvedValueOnce({ messages: [post(1)], nextAfterSeq: null, nextBeforeSeq: null });
    const page = await loadFeedPosts({ listMessages }, "feed-1", 299);
    expect(listMessages.mock.calls.map(([, , , options]) => options.beforeSeq)).toEqual([300, 200]);
    expect(page.messages).toHaveLength(41);
    expect(page.messages.every((message) => message.threadRootMessageId === null)).toBe(true);
  });

  it("offers no composer: people reply only in Discuss", async () => {
    setup();
    await waitFor(() => expect(posts()).toHaveLength(3));
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("Home's compact feed: the newest posts only, no dividers, no welcome, then a way to the whole feed", async () => {
    const today = new Date().toISOString();
    const { client } = setup({ limit: 1 });
    client.listMessages.mockResolvedValue({ messages: [
      post(1, { createdAt: today, text: "**This is our feed.** When something here matters to you, one of us posts it." }),
      // The new seed is known by its kind, whatever its words.
      post(4, { createdAt: today, text: "**Welcome.** Home shows your company today.", kind: "welcome" }),
      post(2, { createdAt: today }),
      post(3, { createdAt: today, text: "**Acme paid**\nStripe confirmed it." }),
    ], nextAfterSeq: null });
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0].querySelector(".feed-post__title")?.textContent).toBe("Acme paid");
    expect(document.querySelector(".team-feed__divider")).toBeNull();
    // The Brief stays editable here, as on the full feed.
    expect(screen.getByRole("button", { name: "Edit the brief" })).toBeInTheDocument();
    const opened = vi.fn();
    window.addEventListener("openRoom", (event) => opened((event as CustomEvent).detail), { once: true });
    await userEvent.click(screen.getByRole("button", { name: "See the whole feed" }));
    expect(opened).toHaveBeenCalledWith({ roomId: "feed-1" });
  });
});
