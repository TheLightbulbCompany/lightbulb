"use client";

import { useMemo, useState } from "react";
import { ArrowDownLeft, ArrowUpRight, Calendar, Repeat } from "lucide-react";

import { ParticipantAvatar } from "@/components/messaging/ParticipantAvatar";
import { clockTime, meetingWhen, meetingEntryStatus, type MailEntry, type MailSection } from "./mailModel";

function typingInField(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && (target.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName));
}

export interface MailListProps {
  sections: MailSection[];
  selectedKey: string | null;
  newKeys: ReadonlySet<string>;
  agentName: (agentId: string) => string | null;
  onOpen: (key: string) => void;
  now: number;
}

/**
 * The list is ONE tab stop (roving tabindex). ↑/↓ and j/k move focus across sections,
 * Home/End jump, Enter/Space open (the rows are buttons). Moving focus never opens.
 */
export function MailList({ sections, selectedKey, newKeys, agentName, onOpen, now }: MailListProps) {
  const order = useMemo(() => sections.flatMap((s) => s.entries.map((e) => e.key)), [sections]);
  const [active, setActive] = useState<string | null>(null);
  const tabStop = active && order.includes(active) ? active : selectedKey && order.includes(selectedKey) ? selectedKey : order[0];

  const focusRow = (list: HTMLElement, key: string) => {
    list.querySelector<HTMLElement>(`[data-mail-key="${CSS.escape(key)}"]`)?.focus();
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (typingInField(event.target) || event.metaKey || event.ctrlKey || event.altKey || order.length === 0) return;
    const at = tabStop ? order.indexOf(tabStop) : -1;
    let next: string | undefined;
    if (event.key === "ArrowDown" || event.key === "j") next = order[Math.min(order.length - 1, at + 1)];
    else if (event.key === "ArrowUp" || event.key === "k") next = order[Math.max(0, at - 1)];
    else if (event.key === "Home") next = order[0];
    else if (event.key === "End") next = order[order.length - 1];
    if (!next) return;
    event.preventDefault();
    setActive(next);
    focusRow(event.currentTarget, next);
  };

  return (
    <div className="mail-list__rows" onKeyDown={onKeyDown}>
      {sections.map((section) => (
        <section key={section.key} aria-labelledby={`mail-${section.key}`} className="mail-section">
          <h3 id={`mail-${section.key}`} className="mail-section__title">{section.title}</h3>
          <ul>
            {section.entries.map((entry) => (
              <li key={entry.key}>
                <button
                  type="button"
                  className="mail-row"
                  data-mail-key={entry.key}
                  data-new={newKeys.has(entry.key) || undefined}
                  aria-current={entry.key === selectedKey ? "true" : undefined}
                  tabIndex={entry.key === tabStop ? 0 : -1}
                  onFocus={() => setActive(entry.key)}
                  onClick={() => onOpen(entry.key)}
                >
                  {entry.kind === "mail"
                    ? <MailRow entry={entry} isNew={newKeys.has(entry.key)} agentName={agentName} />
                    : <MeetingRow entry={entry} now={now} />}
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function MailRow({ entry, isNew, agentName }: {
  entry: MailEntry & { kind: "mail" }; isNew: boolean; agentName: (agentId: string) => string | null;
}) {
  const m = entry.message;
  const inbound = m.direction === "in";
  const name = m.agent_id ? agentName(m.agent_id) : null;
  return <>
    <span className="mail-row__avatar">
      {m.agent_id && <ParticipantAvatar size="xs" showPresence={false}
        participant={{ kind: "agent", id: m.agent_id, displayName: name ?? "Agent", mentionHandle: null }} />}
    </span>
    <span className="mail-row__body">
      <span className="mail-row__line">
        {isNew && <span className="mail-row__dot" aria-hidden="true" />}
        {isNew && <span className="sr-only">New.</span>}
        {inbound
          ? <ArrowDownLeft size={14} className="mail-row__glyph" aria-label="Received" role="img" />
          : <ArrowUpRight size={14} className="mail-row__glyph" aria-label="Sent" role="img" />}
        <span className="mail-row__party">{inbound ? m.from : m.to}</span>
        {m.channel === "text" && <span className="mail-row__chip">Text</span>}
        <time className="mail-row__time" dateTime={m.at}>{entry.at ? clockTime(entry.at) : ""}</time>
      </span>
      <span className="mail-row__second">
        <span className="mail-row__subject">{m.subject}</span>
        {m.text && <span className="mail-row__preview">{m.text.replace(/\s+/g, " ").trim()}</span>}
      </span>
    </span>
  </>;
}

export function MeetingStatus({ entry, now }: { entry: MailEntry & { kind: "meeting" }; now: number }) {
  const presentation = meetingEntryStatus(entry, now);
  return <span className="mail-status" data-status={entry.status}>
    {entry.status === "in_call" && <span className="mail-status__live" aria-hidden="true" />}
    {presentation.label}{entry.status === "ended" && entry.hasTranscript ? " · Transcript" : ""}
  </span>;
}

function MeetingRow({ entry, now }: { entry: MailEntry & { kind: "meeting" }; now: number }) {
  const when = meetingWhen(entry.startAt, entry.endAt, now);
  return <>
    <span className="mail-row__avatar mail-row__avatar--icon"><Calendar size={16} aria-hidden="true" /></span>
    <span className="mail-row__body">
      <span className="mail-row__line">
        <span className="mail-row__party">{entry.meeting.title}</span>
        {entry.meeting.repeats && <Repeat size={13} className="mail-row__glyph" aria-label="Repeats" role="img" />}
      </span>
      <span className="mail-row__second">
        {when && <span className="mail-row__when">{when}</span>}
        <MeetingStatus entry={entry} now={now} />
      </span>
    </span>
  </>;
}
