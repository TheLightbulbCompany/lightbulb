"use client";

import { Copy } from "lucide-react";

// A copy-to-clipboard row for a one-time secret value (webhook URL / signing
// secret). Shared by RoutineDetailPanel's rotate/add-trigger flows and
// RoutinesPanel's create-dialog "Any other app" rung — both show a webhook
// secret exactly once, so there is no reveal toggle.
// `copyLabel` disambiguates the copy button when several fields share a visible
// label — a routine carries one webhook trigger per provider event, so its
// detail panel renders several "Fire URL" fields at once and the row title is
// the only thing that tells them apart.
export function SecretField({
  label,
  value,
  copyLabel,
}: {
  label: string;
  value: string;
  copyLabel?: string;
}) {
  return (
    <div>
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm text-[var(--midnight-soft)]">{label}</span>
        <button
          type="button"
          aria-label={`Copy ${copyLabel ?? label}`}
          onClick={() => navigator.clipboard.writeText(value)}
          className="text-[var(--midnight-muted)] hover:text-[var(--midnight)]"
        >
          <Copy size={16} aria-hidden="true" />
        </button>
      </div>
      <code
        className="mt-1 block break-all rounded-sm px-3 py-2 font-mono text-sm"
        style={{ background: "var(--cream-deep)", color: "var(--midnight)" }}
      >
        {value}
      </code>
    </div>
  );
}
