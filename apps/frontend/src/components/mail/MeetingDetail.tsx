"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { Repeat } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { MeetingStatus } from "./MailList";
import {
  clockTime,
  countMatches,
  highlight,
  meetingItemKey,
  meetingWhen,
  parseTranscript,
  shortDay,
  type MailEntry,
} from "./mailModel";

const UPCOMING = new Set(["scheduled", "bot_created"]);

export function MeetingDetail({ entry, orgId, now, onOpen, onOpenNotes }: {
  entry: MailEntry & { kind: "meeting" };
  orgId: string | undefined;
  now: number;
  onOpen: (key: string) => void;
  onOpenNotes: (roomId: string) => void;
}) {
  const m = entry.meeting;
  const when = meetingWhen(entry.startAt, entry.endAt, now);
  const notes = m.notes_room_id;
  const occurrences = [...m.transcripts].sort((a, b) => b.start_at - a.start_at).slice(0, 10);

  return <article className="mail-detail" aria-labelledby="mail-detail-title">
    <h3 id="mail-detail-title" className="mail-detail__title">{m.title}</h3>
    <p className="mail-detail__meta">
      {when && <span>{when}</span>}
      <MeetingStatus status={entry.status} hasTranscript={entry.hasTranscript} />
      {m.repeats && <span className="mail-detail__repeats"><Repeat size={13} aria-hidden="true" /> Repeats</span>}
    </p>
    {(m.organizer || m.invited_by) && <p className="mail-detail__meta">
      {m.organizer && <span>Organized by {m.organizer}</span>}
      {m.invited_by && m.invited_by !== m.organizer && <span>Added by {m.invited_by}</span>}
    </p>}
    {entry.status === "bot_failed" && m.reason === "not_admitted" && (
      <p className="mail-detail__note">Nobody let the agents in from the lobby.</p>
    )}
    {occurrences.length > 1 && <div className="mail-occurrences" role="group" aria-label="Occurrences">
      {occurrences.map((t) => <button key={t.start_at} type="button" className="mail-chip"
        aria-pressed={t.start_at === entry.startAt} onClick={() => onOpen(meetingItemKey(m.id, t.start_at))}>
        {shortDay(t.start_at)}
      </button>)}
    </div>}
    {notes && <p>
      <button type="button" className="mail-link" onClick={() => onOpenNotes(notes)}>Open notes in chat</button>
    </p>}
    {entry.hasTranscript && entry.startAt !== null
      ? <Transcript meetingId={m.id} startAt={entry.startAt} orgId={orgId} />
      : <p className="mail-detail__note">
        {UPCOMING.has(entry.status ?? "")
          ? "The agents join when it starts. Let them in from the lobby."
          : "No transcript for this meeting."}
      </p>}
  </article>;
}

function Transcript({ meetingId, startAt, orgId }: { meetingId: string; startAt: number; orgId: string | undefined }) {
  const api = useApi();
  const [query, setQuery] = useState("");
  // Fetched only while this occurrence is open; cached per workspace + meeting + occurrence.
  const { data, error, mutate, isValidating } = useSWR<string>(
    orgId ? ["mail-transcript", orgId, meetingId, startAt] : null,
    async () => (await api.downloadBlob(`/agent-mail/meetings/${meetingId}/transcript?start_at=${startAt}`)).text(),
    { revalidateOnFocus: false, shouldRetryOnError: false },
  );
  const parts = useMemo(() => (data === undefined ? [] : parseTranscript(data, startAt)), [data, startAt]);
  const q = query.trim();
  const shown = q
    ? parts.filter((p) => countMatches(p.text, q) + (p.kind === "turn" ? countMatches(p.speaker, q) : 0) > 0)
    : parts;
  const matches = q
    ? shown.reduce((n, p) => n + countMatches(p.text, q) + (p.kind === "turn" ? countMatches(p.speaker, q) : 0), 0)
    : 0;

  if (error && data === undefined) {
    return <div className="mail-transcript" role="alert">
      <p className="mail-detail__note">Couldn&apos;t load the transcript.</p>
      <Button type="button" variant="outline" size="sm" disabled={isValidating} onClick={() => void mutate()}>Retry</Button>
    </div>;
  }
  if (data === undefined) {
    return <div className="room-skeleton" aria-hidden="true" data-testid="transcript-skeleton"><span /><span /><span /></div>;
  }
  return <section className="mail-transcript" aria-label="Transcript">
    <div className="mail-transcript__find">
      <input type="search" aria-label="Find in transcript" placeholder="Find in transcript" value={query}
        onChange={(e) => setQuery(e.target.value)} />
      {q && <span role="status">{matches} {matches === 1 ? "match" : "matches"}</span>}
    </div>
    <ol className="mail-transcript__turns">
      {shown.map((part, i) => part.kind === "turn"
        ? <li key={i} className="mail-turn">
          <p className="mail-turn__head">
            <span className="mail-turn__speaker"><Marked text={part.speaker} query={q} /></span>
            <time className="mail-turn__time" dateTime={new Date(part.at).toISOString()}>{clockTime(part.at)}</time>
          </p>
          <p className="mail-turn__text"><Marked text={part.text} query={q} /></p>
        </li>
        : <li key={i} className="mail-turn"><p className="mail-turn__text"><Marked text={part.text} query={q} /></p></li>)}
    </ol>
  </section>;
}

function Marked({ text, query }: { text: string; query: string }) {
  return <>{highlight(text, query).map((part, i) => (part.match ? <mark key={i}>{part.text}</mark> : part.text))}</>;
}
