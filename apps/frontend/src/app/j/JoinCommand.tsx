"use client";

import { useEffect, useState, useSyncExternalStore } from "react";

import { capture } from "@/lib/analytics";

const INSTALL = "curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh | sh";

const onHashChange = (cb: () => void) => {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
};

// The ticket lives in the URL fragment: read here in the browser only, never sent or logged.
export function JoinCommand() {
  // null on the server and before hydration: nothing renders until the browser has read it.
  const t = useSyncExternalStore(onHashChange, () => window.location.hash.slice(1), () => null);
  const [copied, setCopied] = useState(false);
  // Tickets are base64url. Anything else is refused, never decoded or repaired into a shell line.
  const valid = t !== null && /^[A-Za-z0-9_-]{1,4096}$/.test(t);
  const invalid = !!t && !valid;
  const ticket = invalid ? null : t;

  useEffect(() => {
    if (t !== null) capture("join_link_viewed", { valid }); // never the ticket
  }, [t, valid]);

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
