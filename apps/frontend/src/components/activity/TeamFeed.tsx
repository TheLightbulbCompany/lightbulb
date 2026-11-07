"use client";

import "@/components/messaging/messaging.css";
import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { Info } from "lucide-react";
import type { CanonicalRoomMessage, MessagingClient, RoomDetail } from "@lightbulb/shared";
import { Identity } from "@/components/teams/shared/components/Identity";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { roomUrlTransform } from "@/components/messaging/markdown";
import { roomMarkdown } from "@/components/messaging/roomMarkdown";
import { RoomDocumentCards } from "@/components/messaging/RoomDocumentCards";
import { RoomImageAttachments } from "@/components/messaging/RoomImageAttachments";
import { agentTint } from "@/lib/agentTint";
import { useOptionalLiveEvents } from "@/hooks/useLiveEvents";
import { feedTimeline, splitPost } from "./activityPage";

export type FeedClient = Pick<MessagingClient,
  "getFeed" | "getRoom" | "listMessages" | "markRead" | "updateRoom" | "setReaction" | "downloadAttachment" | "attachmentUrl">;

export interface TeamFeedProps {
  client: FeedClient;
  /** Keys the cache to the signed-in workspace. */
  scope: string;
  currentUserId: string;
  /** Opens the post's thread with its author @mentioned in the composer. */
  onDiscuss: (post: CanonicalRoomMessage) => void;
  isVisible?: () => boolean;
  /** False while the feed column is hidden (the For you tab on a narrow panel): the cursor waits. */
  active?: boolean;
  /** Home: only the newest `limit` posts, no day dividers and no welcome, then a link to the whole feed. */
  limit?: number;
}

/** The feed room's seeded welcome: an intro, not a post. The seed is message kind "welcome"; rooms seeded before that
 *  are matched by its opening words (core/services/messaging.py FEED_WELCOME*). */
export const isFeedWelcome = (post: CanonicalRoomMessage) =>
  post.kind === "welcome" || post.text.startsWith("**This is our feed.**");

const LIKE = "👍";
const ALWAYS = () => true;
/** Enough posts to fill the page; Discuss replies in between never push them out. */
const FEED_POSTS = 30;
// ponytail: a feed buried under more than this many pages of Discuss replies shows fewer posts.
const MAX_PAGES = 10;

/** The latest top-level posts, paging back past thread replies until there are enough. */
export async function loadFeedPosts(client: Pick<FeedClient, "listMessages">, roomId: string, newest: number): Promise<{ messages: CanonicalRoomMessage[] }> {
  const messages: CanonicalRoomMessage[] = [];
  let before: number | null | undefined = newest + 1;
  for (let page = 0; page < MAX_PAGES && before && messages.length < FEED_POSTS; page += 1) {
    const result = await client.listMessages(roomId, 0, 100, { beforeSeq: before });
    messages.push(...result.messages.filter((message) => message.threadRootMessageId === null));
    before = result.nextBeforeSeq;
  }
  return { messages };
}

// ponytail: the feed polls (agents post a few times a day); subscribe to the room's live channel if that is too slow.
const REFRESH_MS = 60_000;

/**
 * The team feed: the brief (editable by anyone), then the agents' posts newest first under
 * "New since you looked" and day dividers. People never post here; they Discuss a post in its
 * thread. Viewing the feed moves its read cursor, which nothing else counts.
 */
export function TeamFeed({ client, scope, currentUserId, onDiscuss, isVisible = ALWAYS, active = true, limit }: TeamFeedProps) {
  const feed = useSWR(["activity-feed", scope], () => client.getFeed());
  // Super-agent: the front agent's older posts were authored with its seat name ("CEO"); it reads "Lightbulb".
  const live = useOptionalLiveEvents();
  const frontAgentId = live?.frontAgentId;
  const roomId = feed.data?.roomId ?? null;
  const room = useSWR(roomId ? ["activity-feed-room", roomId] : null, () => client.getRoom(roomId!), { refreshInterval: REFRESH_MS });
  const newest = room.data?.lastMessageSeq;
  const posts = useSWR(roomId && newest !== undefined ? ["activity-feed-posts", roomId, newest] : null,
    () => loadFeedPosts(client, roomId!, newest!));

  // "New since you looked" compares against the cursor as it was when the feed opened.
  const [readAtOpen, setReadAtOpen] = useState<number | null>(null);
  if (room.data && readAtOpen === null) setReadAtOpen(room.data.lastReadSeq);
  const marked = useRef(0);
  useEffect(() => {
    const current = room.data;
    if (!active || !current || current.lastMessageSeq <= Math.max(current.lastReadSeq, marked.current) || !isVisible()) return;
    marked.current = current.lastMessageSeq;
    void client.markRead(current.roomId, { lastReadSeq: current.lastMessageSeq }).catch(() => { marked.current = 0; });
  }, [room.data, client, isVisible, active]);

  const setRoom = (detail: RoomDetail) => void room.mutate(detail, { revalidate: false });
  const replacePost = (next: CanonicalRoomMessage) => void posts.mutate((page) => page && {
    ...page, messages: page.messages.map((message) => message.messageId === next.messageId ? next : message),
  }, { revalidate: false });

  const all = posts.data ? feedTimeline(posts.data.messages, readAtOpen ?? 0) : [];
  const compact = limit !== undefined;
  const postsOnly = all.filter((entry) => entry.type === "post" && !isFeedWelcome(entry.post));
  const timeline = compact ? postsOnly.slice(0, limit) : all;
  const failed = feed.error || room.error || posts.error;

  return <section className={compact ? "team-feed team-feed--compact" : "team-feed"} aria-labelledby="team-feed-title">
    {compact ? <h2 id="team-feed-title" className="text-ui font-semibold">Feed</h2> : <h2 id="team-feed-title" className="sr-only">Team feed</h2>}
    {roomId && room.data && <Brief roomId={roomId} topic={room.data.topic ?? feed.data?.topic ?? null} client={client} onSaved={setRoom} />}
    {failed && timeline.length === 0
      ? <p role="alert" className="team-feed__empty">Couldn&apos;t load the feed. Try again in a moment.</p>
      : !posts.data
        ? <p role="status" className="team-feed__empty">Loading the feed…</p>
        : timeline.length === 0
          ? <p className="team-feed__empty">No posts yet. Your agents post here when something matters to the team.</p>
          : <ol className="team-feed__list">
            {timeline.map((entry) => entry.type === "divider"
              ? <li key={entry.key} className="team-feed__divider" data-new={entry.key === "new" || undefined}><span>{entry.label}</span></li>
              : <li key={entry.post.messageId}><FeedPost post={entry.post} client={client} currentUserId={currentUserId} frontAgentId={frontAgentId} compact={compact}
                onDiscuss={onDiscuss} onChanged={replacePost} /></li>)}
          </ol>}
    {compact && roomId && postsOnly.length > timeline.length && <button type="button" className="justify-self-start text-ui text-muted-foreground underline hover:text-foreground"
      onClick={() => window.dispatchEvent(new CustomEvent("openRoom", { detail: { roomId } }))}>See the whole feed</button>}
  </section>;
}

function Brief({ roomId, topic, client, onSaved }: {
  roomId: string; topic: string | null; client: FeedClient; onSaved: (detail: RoomDetail) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (draft === null) return;
    setBusy(true);
    setError(false);
    try {
      onSaved(await client.updateRoom(roomId, { topic: draft.trim() || null }));
      setDraft(null);
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  };
  if (draft !== null) return <form className="team-feed__brief" onSubmit={save}>
    <label htmlFor="team-feed-brief" className="team-feed__brief-label">Brief</label>
    <textarea id="team-feed-brief" className="team-feed__brief-input" value={draft} rows={2} autoFocus disabled={busy}
      onChange={(event) => setDraft(event.target.value)} />
    {error && <p role="alert" className="team-feed__error">Couldn&apos;t save the brief. Try again.</p>}
    <div className="team-feed__brief-buttons">
      <button type="submit" className="editorial-btn editorial-btn--sm editorial-btn--primary" disabled={busy}>Save</button>
      <button type="button" className="editorial-btn editorial-btn--sm editorial-btn--secondary" disabled={busy} onClick={() => setDraft(null)}>Cancel</button>
    </div>
  </form>;
  return <div className="team-feed__brief">
    <span className="team-feed__brief-label">Brief</span>
    <p className="team-feed__brief-text">{topic || "No brief yet. Tell your agents what the team wants to hear about."}</p>
    <button type="button" className="editorial-btn editorial-btn--sm editorial-btn--ghost" aria-label="Edit the brief"
      onClick={() => setDraft(topic ?? "")}>Edit</button>
  </div>;
}

function FeedPost({ post, client, currentUserId, frontAgentId, onDiscuss, onChanged, compact = false }: {
  post: CanonicalRoomMessage; client: FeedClient; currentUserId: string; frontAgentId?: string; compact?: boolean;
  onDiscuss: (post: CanonicalRoomMessage) => void; onChanged: (post: CanonicalRoomMessage) => void;
}) {
  const [why, setWhy] = useState(false);
  const [pending, setPending] = useState(false);
  const { title, body } = splitPost(post.text);
  const name = post.author.kind === "agent" && post.author.id === frontAgentId ? "Lightbulb" : post.author.displayName || "Unknown author";
  const likes = post.reactions?.[LIKE] ?? [];
  const liked = likes.includes(currentUserId);
  const toggleLike = async () => {
    setPending(true);
    try {
      onChanged(await client.setReaction(post.roomId, post.messageId, LIKE, !liked));
    } catch {
      /* the button stays as it was; a retry is one click */
    } finally {
      setPending(false);
    }
  };
  return <article className="room-message feed-post" aria-label={`Post from ${name}`}>
    <Identity name={name} avatarOnly size="default" tint={post.author.kind === "agent" ? agentTint(post.author.id) : null} />
    <div className="room-message-content">
      <div className="room-message-byline">
        <span className="font-semibold truncate" title={name}>{name}</span>
        {post.author.kind === "agent" && <span className="room-agent-label">Agent</span>}
      </div>
      {/* The title line is markdown too: the welcome's "**This is our feed.** When …" is bold text, not asterisks. */}
      <div className="feed-post__title"><MarkdownView skipHtml components={roomMarkdown} urlTransform={roomUrlTransform}>{title}</MarkdownView></div>
      {body && <div className={compact ? "feed-post__body--clamp" : undefined}><MarkdownView skipHtml components={roomMarkdown} urlTransform={roomUrlTransform}>{body}</MarkdownView></div>}
      <RoomDocumentCards text={post.text} />
      {!!post.attachments?.length && <RoomImageAttachments roomId={post.roomId} attachments={post.attachments}
        download={client.downloadAttachment} attachmentUrl={client.attachmentUrl} />}
      <div className="feed-post__footer">
        <button type="button" className="editorial-btn editorial-btn--sm editorial-btn--ghost" aria-pressed={liked} disabled={pending}
          aria-label={`${liked ? "Unlike" : "Like"}${likes.length ? `, ${likes.length}` : ""}`} onClick={() => void toggleLike()}>
          {LIKE}{likes.length > 0 && <span>{likes.length}</span>}
        </button>
        <button type="button" className="editorial-btn editorial-btn--sm editorial-btn--ghost" onClick={() => onDiscuss(post)}>Discuss</button>
        <time className="feed-post__age" dateTime={post.createdAt} title={new Date(post.createdAt).toLocaleString()}>{timeAgo(post.createdAt)}</time>
        {post.postReason && <button type="button" className="feed-post__why-toggle" aria-label="Why I created this post" aria-expanded={why}
          onClick={() => setWhy(!why)}><Info size={14} aria-hidden="true" /></button>}
      </div>
      {why && post.postReason && <p className="feed-post__why"><span>Why I created this post</span>{post.postReason}</p>}
    </div>
  </article>;
}
