import type { LocalSession, MemberRef, RoomMember, SessionSend } from "./types";
import { sessionTitle, sessionToolName } from "./localSessionLabels";

export const MENTION_SCHEME = "mention:";

/** Anything addressable by `@`: a room member, or a directory candidate. */
export type MentionTarget = Pick<RoomMember, "kind" | "id" | "displayName" | "mentionHandle">;

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

interface TokenEntry {
  participant: MentionTarget;
  token: string;
  isHandle: boolean;
}

// ONE grammar for both readers: what renders as a chip is exactly what
// resolves to a structured mention on send. Split them and a typed token can
// look mentioned and notify nobody.
// A human answers to their handle AND their display name: rows that predate
// server-side human handles carry mentionHandle: null and are still addressed
// by name.
function tokenEntries(participants: readonly MentionTarget[]): TokenEntry[] {
  return participants
    .flatMap((participant) =>
      [participant.mentionHandle, participant.kind === "human" ? participant.displayName : null]
        .map((token, index) => ({ participant, token, isHandle: index === 0 })))
    .filter((entry): entry is TokenEntry => !!entry.token)
    // Longest token first so @Aden Kim beats @Aden; on a tie the handle wins,
    // because one person's handle can be another's display name and the owner
    // of the handle is who the sender meant.
    .sort((left, right) => right.token.length - left.token.length
      || Number(right.isHandle) - Number(left.isHandle));
}

const tokenPattern = (entries: readonly TokenEntry[]) =>
  new RegExp(`(^|[\\s(])@(${entries.map((entry) => escapeRegExp(entry.token)).join("|")})(?![\\w.-]|\\]\\()`, "gi");

// Even segments are prose; odd segments are code (inline `…` or fenced ```…```).
const proseSegments = (text: string) => text.split(/(```[\s\S]*?```|`[^`\n]*`)/);

export function parseMentionHref(href: string): { kind: "human" | "agent" | "session"; id: string } | null {
  if (!href.startsWith(MENTION_SCHEME)) return null;
  const [kind, ...rest] = href.slice(MENTION_SCHEME.length).split(":");
  const id = rest.join(":");
  return (kind === "human" || kind === "agent" || kind === "session") && id ? { kind, id } : null;
}

/**
 * Token list and pattern per participant list.
 *
 * Both derive only from `participants`, but `linkifyMentions` is called once
 * per message per render of the timeline — so without this every render sorted
 * the token list and compiled a fresh RegExp for every message on screen.
 * Keyed on the array's identity (both apps hand over a memoised array) and
 * weak, so a stale roster cannot pin memory.
 */
const grammarCache = new WeakMap<object, { entries: TokenEntry[]; pattern: RegExp | null }>();

function grammarFor(participants: readonly MentionTarget[]): { entries: TokenEntry[]; pattern: RegExp | null } {
  const cached = grammarCache.get(participants);
  if (cached) return cached;
  const entries = tokenEntries(participants);
  const grammar = { entries, pattern: entries.length === 0 ? null : tokenPattern(entries) };
  grammarCache.set(participants, grammar);
  return grammar;
}

export function linkifyMentions(
  text: string,
  // Unused: the composer resolves typed @tokens to structured mentions on
  // send. This rewrite links any member token found in the text regardless of
  // whether it is also present here as a structured mention.
  mentions: readonly MemberRef[],
  participants: readonly MentionTarget[],
): string {
  const { entries, pattern } = grammarFor(participants);
  if (pattern === null) return text;
  const rewrite = (segment: string) =>
    segment.replace(pattern, (match, lead: string, token: string) => {
      const entry = entries.find((candidate) => candidate.token.toLowerCase() === token.toLowerCase());
      if (!entry) return match;
      return `${lead}[@${token}](${MENTION_SCHEME}${entry.participant.kind}:${entry.participant.id})`;
    });
  return proseSegments(text)
    .map((segment, index) => (index % 2 === 0 ? rewrite(segment) : segment))
    .join("");
}

/** Every room member named by a plain `@token` in the prose of `text`. */
export function resolveTypedMentions(text: string, participants: readonly MentionTarget[]): MemberRef[] {
  const entries = tokenEntries(participants);
  if (entries.length === 0) return [];
  const pattern = tokenPattern(entries);
  const found: MemberRef[] = [];
  proseSegments(text).forEach((segment, index) => {
    if (index % 2 !== 0) return;
    for (const match of segment.matchAll(pattern)) {
      const entry = entries.find((candidate) => candidate.token.toLowerCase() === match[2].toLowerCase());
      if (!entry) continue;
      const ref = { kind: entry.participant.kind, id: entry.participant.id };
      if (!found.some((existing) => existing.kind === ref.kind && existing.id === ref.id)) found.push(ref);
    }
  });
  return found;
}

export interface SessionMentionCandidate {
  session: LocalSession;
  handle: string;
}

export function sessionMentionHandle(session: Pick<LocalSession, "provider" | "cwdName" | "title" | "displayTitle">): string {
  const name = (session.displayTitle || session.title || session.cwdName || "session").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48) || "session";
  return `${session.provider}-${name}`;
}

export function sessionMentionCandidates(sessions: readonly LocalSession[]): SessionMentionCandidate[] {
  const counts = new Map<string, number>();
  for (const session of sessions) counts.set(sessionMentionHandle(session), (counts.get(sessionMentionHandle(session)) ?? 0) + 1);
  return sessions.map((session) => {
    const base = sessionMentionHandle(session);
    // Rare identical titles need a stable suffix so two targets fit one message.
    return { session, handle: counts.get(base)! > 1 ? `${base}-${session.sessionId.slice(0, 8)}` : base };
  });
}

export function resolveTypedSessionMentions(
  text: string, targets: readonly LocalSession[] | readonly SessionMentionCandidate[], selected: Readonly<Record<string, string>> = {},
): string[] {
  const candidates = targets[0] && "handle" in targets[0]
    ? targets as readonly SessionMentionCandidate[]
    : sessionMentionCandidates(targets as readonly LocalSession[]);
  if (!candidates.length) return [];
  const handles = [...candidates].sort((a, b) => b.handle.length - a.handle.length);
  const pattern = new RegExp(`(^|[\\s(])@(${handles.map((c) => escapeRegExp(c.handle)).join("|")})(?![\\w.-]|\\]\\()`, "gi");
  const found: string[] = [];
  proseSegments(text).forEach((segment, index) => {
    if (index % 2 !== 0) return;
    for (const match of segment.matchAll(pattern)) {
      const matches = handles.filter((entry) => entry.handle.toLowerCase() === match[2].toLowerCase());
      const candidate = matches.find((entry) => entry.session.sessionId === selected[match[2].toLowerCase()]) ?? (matches.length === 1 ? matches[0] : null);
      if (candidate && !found.includes(candidate.session.sessionId)) found.push(candidate.session.sessionId);
    }
  });
  return found;
}

function rewriteSessionMentions(text: string, sends: readonly SessionSend[] | undefined, sessions: readonly LocalSession[], markdown: boolean): string {
  const pattern = new RegExp(`(^|[\\s(])@((?:claude|codex)-[a-z0-9-]+)(?![\\w.-]|\\]\\()`, "gi");
  return proseSegments(text).map((segment, index) => index % 2 === 0
    ? segment.replace(pattern, (match, lead: string, token: string) => {
      const legacyId = /([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i.exec(token)?.[1];
      const matching = (sends ?? []).filter((send) => {
        const session = sessions.find((item) => item.sessionId === send.sessionId);
        const [tool, title] = send.displayLabel?.split(" · ") ?? [];
        const snapshotHandle = tool && title ? sessionMentionHandle({ provider: tool === "Claude Code" ? "claude" : "codex", title, displayTitle: null, cwdName: null }) : null;
        return token.toLowerCase().endsWith(send.sessionId.toLowerCase())
          || (session && (token.toLowerCase() === sessionMentionHandle(session) || token.toLowerCase() === `${sessionMentionHandle(session)}-${send.sessionId.slice(0, 8).toLowerCase()}`))
          || (snapshotHandle && (token.toLowerCase() === snapshotHandle || token.toLowerCase() === `${snapshotHandle}-${send.sessionId.slice(0, 8).toLowerCase()}`));
      });
      const send = matching.length === 1 ? matching[0] : sends?.length === 1 ? sends[0] : null;
      if (!send && !legacyId) return match;
      const id = send?.sessionId ?? legacyId!;
      const session = sessions.find((item) => item.sessionId === id);
      const label = `@${send?.displayLabel || (session ? `${sessionToolName(session.provider)} · ${sessionTitle(session)}` : `${token.startsWith("claude-") ? "Claude Code" : "Codex"} session`)}`;
      return markdown ? `${lead}[${label.replace(/[\\[\]]/g, "")}](${MENTION_SCHEME}session:${id})` : `${lead}${label}`;
    })
    : segment).join("");
}

/** Turn a session tag into a readable mention chip, including old UUID tags. */
export function linkifySessionMentions(text: string, sends: readonly SessionSend[] | undefined, sessions: readonly LocalSession[] = []): string {
  return rewriteSessionMentions(text, sends, sessions, true);
}

/** Readable text for previews and accessibility labels that cannot render Markdown. */
export function displaySessionMentions(text: string, sends: readonly SessionSend[] | undefined, sessions: readonly LocalSession[] = []): string {
  return rewriteSessionMentions(text, sends, sessions, false);
}

export type TriggerKind = "mention" | "emoji";

export interface CompletionTrigger {
  /** "mention" for `@`, "emoji" for `:`. */
  kind: TriggerKind;
  /** Index of the sigil itself. */
  start: number;
  /** The caret. */
  end: number;
  /** Everything typed after the sigil. */
  query: string;
}

// A mention query may carry up to two internal spaces so `@Aden Kim` is
// reachable (Open decision 1: the human token in text is `@Display Name`).
// Nothing here rejects prose — the caller closes the list when no member
// matches, which is the only rule that generalises to every display name.
const MENTION_RE = /(?:^|\s)@([^\s@:]*(?: [^\s@:]*){0,2})$/;
// Two characters minimum, per the spec's "`:` emoji typeahead after two characters".
const EMOJI_RE = /(?:^|\s):([a-z0-9_+-]{2,})$/i;

export function activeTrigger(value: string, caret: number | null): CompletionTrigger | null {
  if (caret === null) return null;
  const head = value.slice(0, caret);
  const emoji = EMOJI_RE.exec(head);
  if (emoji) return { kind: "emoji", start: caret - emoji[1].length - 1, end: caret, query: emoji[1] };
  const mention = MENTION_RE.exec(head);
  if (mention) return { kind: "mention", start: caret - mention[1].length - 1, end: caret, query: mention[1] };
  return null;
}

export function applyCompletion(value: string, trigger: CompletionTrigger, token: string): { text: string; caret: number } {
  const tail = value.slice(trigger.end);
  const separator = tail.startsWith(" ") ? "" : " ";
  return {
    text: `${value.slice(0, trigger.start)}${token}${separator}${tail}`,
    caret: trigger.start + token.length + separator.length,
  };
}
