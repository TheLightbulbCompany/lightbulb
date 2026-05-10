"use client";

import { useState } from "react";
import { Copy, Eye, EyeOff } from "lucide-react";

import { useHookCredentials } from "@/hooks/useHookCredentials";
import { ApiError } from "@/lib/api";

export function WebhooksPanel() {
  const { credentials, isLoading, error } = useHookCredentials();
  const [revealed, setRevealed] = useState(false);

  if (isLoading) {
    return (
      <div className="settings-panel" id="panel-webhooks" role="tabpanel">
        <div className="text-sm text-zinc-400">Loading…</div>
      </div>
    );
  }
  // 409 hook_token_pending — backend returns this for legacy rows that
  // pre-date hook-token plumbing and haven't been backfilled yet (or
  // haven't triggered a self-healing reprovision). Render a positive
  // "still provisioning" message instead of the generic error so the
  // user knows to wait rather than retrying or flagging the integration.
  if (error instanceof ApiError && error.status === 409) {
    return (
      <div className="settings-panel" id="panel-webhooks" role="tabpanel">
        <h1 className="settings-page-title">Webhooks</h1>
        <p className="settings-page-desc">
          Your webhook ingress is being provisioned. Refresh in a moment to see
          your URL and token.
        </p>
      </div>
    );
  }
  if (error || !credentials) {
    return (
      <div className="settings-panel" id="panel-webhooks" role="tabpanel">
        <div className="text-sm text-red-400">
          Couldn&apos;t load webhook credentials. Try again in a moment.
        </div>
      </div>
    );
  }

  const tokenDisplay = revealed
    ? credentials.token
    : "•".repeat(credentials.token.length);

  return (
    <div className="settings-panel" id="panel-webhooks" role="tabpanel">
      <h1 className="settings-page-title">Webhooks</h1>
      <p className="settings-page-desc">
        External services (Zapier, Stripe, GitHub, custom integrations) can
        trigger your agents by POSTing to your webhook URL with the bearer
        token below.
      </p>
      <div className="space-y-6">
        <Field
          label="Public URL"
          value={credentials.public_url}
          onCopy={() => navigator.clipboard.writeText(credentials.public_url)}
        />

        <div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-medium">Hook token</span>
            <div className="flex gap-2">
              <button
                type="button"
                aria-label={revealed ? "Hide token" : "Reveal token"}
                onClick={() => setRevealed((v) => !v)}
                className="rounded p-1 hover:bg-zinc-800"
              >
                {revealed ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
              <button
                type="button"
                aria-label="Copy token"
                onClick={() => navigator.clipboard.writeText(credentials.token)}
                className="rounded p-1 hover:bg-zinc-800"
              >
                <Copy size={14} />
              </button>
            </div>
          </div>
          <div className="mt-1 rounded border border-zinc-800 bg-zinc-950 px-3 py-2 font-mono text-sm">
            {tokenDisplay}
          </div>
          {credentials.rotated_at && (
            <p className="mt-1 text-xs text-zinc-500">
              Last rotated: {new Date(credentials.rotated_at).toLocaleString()}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onCopy,
}: {
  label: string;
  value: string;
  onCopy: () => void;
}) {
  return (
    <div>
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">{label}</span>
        <button
          type="button"
          aria-label={`Copy ${label}`}
          onClick={onCopy}
          className="rounded p-1 hover:bg-zinc-800"
        >
          <Copy size={14} />
        </button>
      </div>
      <div className="mt-1 break-all rounded border border-zinc-800 bg-zinc-950 px-3 py-2 font-mono text-sm">
        {value}
      </div>
    </div>
  );
}
