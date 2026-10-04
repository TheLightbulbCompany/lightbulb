"use client";

import useSWR from "swr";

import { ParticipantAvatar } from "@/components/messaging/ParticipantAvatar";
import { useApi } from "@/lib/api";
import { fullDateTime, type MailMessage } from "./mailModel";

export function MailDetail({ message, at, agentName, orgId }: {
  message: MailMessage; at: number; agentName: string | null; orgId: string | undefined;
}) {
  const api = useApi();
  const inbound = message.direction === "in";
  // The list carries the first 2,000 characters; the full text is one read, only when cut.
  const full = useSWR<MailMessage>(
    message.truncated && orgId ? [`/agent-mail/message?key=${encodeURIComponent(message.key)}`, orgId] : null,
    ([url]: [string, string]) => api.get(url) as Promise<MailMessage>,
    { revalidateOnFocus: false, shouldRetryOnError: false },
  );
  const text = full.data?.text || message.text;

  return <article className="mail-detail" aria-labelledby="mail-detail-title">
    <h3 id="mail-detail-title" className="mail-detail__title">{message.subject || "(no subject)"}</h3>
    <p className="mail-detail__meta">
      <span>{inbound ? `From ${message.from} · to ${message.to}` : `To ${message.to}`}</span>
      {at > 0 && <time dateTime={message.at}>{fullDateTime(at)}</time>}
      {message.agent_id && <span className="mail-detail__agent">
        <ParticipantAvatar size="xs" showPresence={false}
          participant={{ kind: "agent", id: message.agent_id, displayName: agentName ?? "Agent", mentionHandle: null }} />
        {inbound ? "Handled by" : "Sent by"} {agentName ?? "an agent"}
      </span>}
    </p>
    {!inbound && !message.text
      ? <p className="mail-detail__note">The text of this mail was not kept.</p>
      : <div className="mail-detail__body">{text}</div>}
    {message.truncated && full.error && <p className="mail-detail__note">Showing the first 2,000 characters.</p>}
  </article>;
}
