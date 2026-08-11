"use client";

import { Copy } from "lucide-react";

// A copy-to-clipboard row for a one-time secret value (webhook URL / signing
// secret). Shared by RoutineDetailPanel's rotate/add-trigger flows and
// RoutinesPanel's create-dialog "Any other app" rung — both show a webhook
// secret exactly once, so there is no reveal toggle, unlike the
// fetched-then-masked token in settings/WebhooksPanel.tsx.
export function SecretField({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm text-[var(--ink-soft)]">{label}</span>
        <button
          type="button"
          aria-label={`Copy ${label}`}
          onClick={() => navigator.clipboard.writeText(value)}
          className="text-[var(--mute)] hover:text-[var(--ink)]"
        >
          <Copy className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      </div>
      <code
        className="mt-1 block break-all rounded px-3 py-2 font-mono text-sm"
        style={{ background: "var(--cream-deeper)", color: "var(--ink)" }}
      >
        {value}
      </code>
    </div>
  );
}
