"use client";

import { useEffect, useState } from "react";

const INSTALL = "curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh | sh";

// The ticket lives in the URL fragment: read here in the browser only, never sent or logged.
export function JoinCommand() {
  const [ticket, setTicket] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setTicket(decodeURIComponent(window.location.hash.slice(1)).replace(/'/g, "") || "");
  }, []);

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
      <pre className="ld-mono overflow-x-auto whitespace-pre-wrap break-all rounded-lg border border-[var(--midnight-muted)] p-4 text-sm leading-6 text-[var(--midnight)]">
        {command}
      </pre>
      <button
        type="button"
        className="ld-mono rounded-lg border border-[var(--midnight-muted)] px-4 py-2 text-sm text-[var(--midnight)]"
        onClick={() => {
          void navigator.clipboard.writeText(command).then(() => setCopied(true));
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
      <p>Or tell your agent: join the Lightbulb network with this link</p>
    </div>
  );
}
