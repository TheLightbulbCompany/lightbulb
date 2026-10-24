"use client";

import { useEffect, useState } from "react";

import { capture } from "@/lib/analytics";

const INSTALL = "curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh | sh";

// The ticket lives in the URL fragment: read here in the browser only, never sent or logged.
export function JoinCommand() {
  const [ticket, setTicket] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    const t = window.location.hash.slice(1);
    // Tickets are base64url. Anything else is refused, never decoded or repaired into a shell line.
    const valid = /^[A-Za-z0-9_-]{1,4096}$/.test(t);
    capture("join_link_viewed", { valid }); // never the ticket
    if (t && !valid) setInvalid(true);
    else setTicket(t);
  }, []);

  if (invalid) return <p>This link is not valid. Ask for a new one.</p>;
  if (ticket === null) return null;
  if (!ticket) {
    return (
      <p>
        This link has no invite in it. Ask whoever sent it to share their join link again, or run{" "}
        <code className="ld-mono">lightbulb invite</code> yourself.
      </p>
    );
  }

  const command = `${INSTALL} && ~/.lightbulb/connect/bin/lightbulb join '${ticket}'`;
  return (
    <div className="space-y-3">
      <pre data-private className="ph-no-capture ld-mono overflow-x-auto whitespace-pre-wrap break-all rounded-lg border border-[var(--midnight-muted)] p-4 text-sm leading-6 text-[var(--midnight)]">
        {command}
      </pre>
      <button
        type="button"
        className="ld-mono rounded-lg border border-[var(--midnight-muted)] px-4 py-2 text-sm text-[var(--midnight)]"
        onClick={() => {
          capture("join_command_copied");
          navigator.clipboard.writeText(command).then(() => setCopied(true)).catch(() => {});
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
      <p>Or tell your agent: join the Lightbulb network with this link</p>
    </div>
  );
}
