"use client";

import { useState } from "react";
import useSWRInfinite from "swr/infinite";
import { ArrowDownLeft, ArrowUpRight, Calendar, RefreshCw } from "lucide-react";
import { agentDisplayName, type Agent } from "@lightbulb/shared";

import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";

interface InboxMeeting {
  id: string;
  title: string;
  organizer: string | null;
  status: string | null;
  start_at: number | null;
  end_at: number | null;
  repeats: boolean;
  transcripts: { start_at: number }[];
}

interface InboxMessage {
  key: string;
  direction: "in" | "out";
  from: string;
  to: string;
  subject: string;
  at: string;
  agent_id: string | null;
  text: string;
}

interface InboxPage {
  address: string;
  meetings?: InboxMeeting[];
  messages?: InboxMessage[];
  next?: string | null;
}

const STATE: Record<string, string> = {
  scheduled: "Agents will join",
  bot_created: "Agents will join",
  in_call: "In call",
  ended: "Ended",
  cancelled: "Cancelled",
  bot_failed: "Couldn't join",
};

const WHEN = new Intl.DateTimeFormat(undefined, {
  weekday: "short",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});
const DAY = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const TIME = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

function meetingWhen(m: InboxMeeting): string {
  if (!m.start_at) return "";
  const when = WHEN.format(new Date(m.start_at * 1000));
  return m.repeats ? `Repeats · Next ${when}` : when;
}

function shortTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toDateString() === new Date().toDateString() ? TIME.format(d) : DAY.format(d);
}

type Transcript = { text?: string; error?: boolean };

export function TeamInbox({ agents }: { agents?: Agent[] }) {
  const api = useApi();
  const { data, error, isLoading, isValidating, mutate, setSize } = useSWRInfinite<InboxPage>(
    (index, prev: InboxPage | null) => {
      if (index === 0) return "/agent-mail/inbox";
      return prev?.next ? `/agent-mail/inbox?before=${encodeURIComponent(prev.next)}` : null;
    },
    (url: string) => api.get(url) as Promise<InboxPage>,
    { revalidateOnFocus: true, shouldRetryOnError: false },
  );
  const [openMail, setOpenMail] = useState<string | null>(null);
  const [transcripts, setTranscripts] = useState<Record<string, Transcript>>({});

  const toggleTranscript = async (meetingId: string, startAt: number) => {
    const key = `${meetingId}:${startAt}`;
    if (transcripts[key]) {
      setTranscripts((t) => {
        const rest = { ...t };
        delete rest[key];
        return rest;
      });
      return;
    }
    setTranscripts((t) => ({ ...t, [key]: {} }));
    let result: Transcript;
    try {
      const blob = await api.downloadBlob(`/agent-mail/meetings/${meetingId}/transcript?start_at=${startAt}`);
      result = { text: await blob.text() };
    } catch {
      result = { error: true };
    }
    // Hidden while it loaded: stay hidden.
    setTranscripts((t) => (t[key] ? { ...t, [key]: result } : t));
  };

  if (error && !data) return <p className="text-xs text-err">Couldn&apos;t load the inbox.</p>;
  if (isLoading || !data) return <p className="text-xs text-[var(--midnight-muted)]">Loading inbox…</p>;

  const meetings = data[0]?.meetings ?? [];
  const messages = data.flatMap((p) => p.messages ?? []);
  const more = Boolean(data[data.length - 1]?.next);
  const names = new Map((agents ?? []).map((a) => [a.id, agentDisplayName(a)]));

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold">Inbox</span>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          aria-label="Refresh inbox"
          disabled={isValidating}
          onClick={() => mutate()}
        >
          <RefreshCw size={14} />
        </Button>
      </div>

      {meetings.length === 0 && messages.length === 0 && (
        <p className="text-xs text-[var(--midnight-muted)]">
          Nothing has arrived yet. Add this address to a calendar event to bring the agents into the meeting.
        </p>
      )}

      {meetings.length > 0 && (
        <div>
          <div className="text-xs font-semibold">Meetings</div>
          <ul className="mt-1 divide-y divide-[var(--edge)] rounded-md border border-[var(--edge)]">
            {meetings.map((m) => (
              <li key={m.id} className="p-2 text-xs">
                <div className="flex items-start gap-2">
                  <Calendar size={14} className="mt-0.5 shrink-0 text-[var(--midnight-muted)]" aria-hidden />
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-semibold">{m.title}</div>
                    <div className="text-[var(--midnight-muted)]">
                      {[meetingWhen(m), STATE[m.status ?? ""]].filter(Boolean).join(" · ")}
                    </div>
                    {m.transcripts.length > 0 && (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {m.transcripts.map((t) => {
                          const shown = transcripts[`${m.id}:${t.start_at}`];
                          return (
                            <Button
                              key={t.start_at}
                              type="button"
                              variant="outline"
                              size="sm"
                              aria-expanded={Boolean(shown)}
                              onClick={() => toggleTranscript(m.id, t.start_at)}
                            >
                              Transcript · {DAY.format(new Date(t.start_at * 1000))}
                            </Button>
                          );
                        })}
                      </div>
                    )}
                    {m.transcripts.map((t) => {
                      const shown = transcripts[`${m.id}:${t.start_at}`];
                      if (!shown) return null;
                      if (shown.error) {
                        return <p key={t.start_at} className="mt-1 text-err">Couldn&apos;t load the transcript.</p>;
                      }
                      if (shown.text === undefined) {
                        return <p key={t.start_at} className="mt-1 text-[var(--midnight-muted)]">Loading transcript…</p>;
                      }
                      return (
                        <pre
                          key={t.start_at}
                          className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded-md border border-[var(--edge)] p-2 font-mono text-xs"
                        >
                          {shown.text}
                        </pre>
                      );
                    })}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {messages.length > 0 && (
        <div>
          <div className="text-xs font-semibold">Mail</div>
          <ul className="mt-1 divide-y divide-[var(--edge)] rounded-md border border-[var(--edge)]">
            {messages.map((msg) => {
              const inbound = msg.direction === "in";
              const party = inbound ? msg.from : msg.to;
              const agent = msg.agent_id ? names.get(msg.agent_id) : undefined;
              const open = openMail === msg.key;
              const row = (
                <div className="flex items-center gap-2">
                  {inbound ? (
                    <ArrowDownLeft size={14} className="shrink-0 text-[var(--midnight-muted)]" aria-label="Received" />
                  ) : (
                    <ArrowUpRight size={14} className="shrink-0 text-[var(--midnight-muted)]" aria-label="Sent" />
                  )}
                  <span className="min-w-0 flex-1 truncate">
                    <span className="font-semibold">{party}</span> {msg.subject}
                  </span>
                  {agent && <span className="shrink-0 text-[var(--midnight-muted)]">{agent}</span>}
                  <span className="shrink-0 text-[var(--midnight-muted)]">{shortTime(msg.at)}</span>
                </div>
              );
              return (
                <li key={msg.key} className="text-xs">
                  {inbound ? (
                    <button
                      type="button"
                      className="w-full p-2 text-left"
                      aria-expanded={open}
                      onClick={() => setOpenMail(open ? null : msg.key)}
                    >
                      {row}
                    </button>
                  ) : (
                    <div className="p-2">{row}</div>
                  )}
                  {inbound && open && (
                    <p className="whitespace-pre-wrap px-2 pb-2 text-[var(--midnight-muted)]">{msg.text || "(no text)"}</p>
                  )}
                </li>
              );
            })}
          </ul>
          {more && (
            <Button type="button" variant="ghost" size="sm" className="mt-1" onClick={() => setSize((s) => s + 1)}>
              Load more
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
