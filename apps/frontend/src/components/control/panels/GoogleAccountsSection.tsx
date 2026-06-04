"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiError, useGoogleAccounts } from "@/hooks/useGoogleAccounts";
import { agentDisplayName, useAgents } from "@/hooks/useAgents";

interface GoogleAccountsSectionProps {
  /** Org admins may manage accounts/assignments; non-admins see read-only. */
  canManage: boolean;
}

/** Sentinel select value for "no explicit assignment" → uses the owner's
 * default account. Kept distinct from any email so it can never collide. */
const DEFAULT_OPTION = "__default__";

const NON_ADMIN_MSG = "Only organization admins can manage Google accounts.";

/**
 * Maps an ApiError from a disconnect/assign/unassign call to a short,
 * user-facing string. The backend hands back a useful `detail` for most
 * cases via ApiError.message; we special-case 403 so non-admins get a clear
 * permissions message instead of a raw FastAPI detail.
 */
function errorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return NON_ADMIN_MSG;
    if (e.message && e.message !== `API ${e.status}`) return e.message;
  }
  return e instanceof Error ? e.message : "Something went wrong.";
}

/**
 * Google multi-account management, rendered inside the Integrations tab only
 * when the `google` provider is connected. Three parts:
 *
 *   1. Connected accounts — each Google account with a per-row Disconnect.
 *   2. Connect another — additive OAuth redirect to add a second account.
 *   3. Per-agent accounts — a dropdown per agent picking which connected
 *      account that agent acts as ("Default" → no explicit assignment).
 *
 * Mutations are admin-gated; non-admins see disabled controls. State lives in
 * useGoogleAccounts (SWR cache) + useAgents (gateway RPC); this component owns
 * only transient per-row busy/error flags.
 */
export function GoogleAccountsSection({ canManage }: GoogleAccountsSectionProps) {
  const {
    accounts,
    assignments,
    isLoading,
    error,
    mutate,
    disconnectAccount,
    assign,
    unassign,
  } = useGoogleAccounts();
  const { agents, isLoading: agentsLoading } = useAgents();

  // Track which row (keyed by email or agent id) is mid-request, plus the
  // last error for that row, so spinners + messages stay row-local.
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [rowError, setRowError] = useState<{ key: string; msg: string } | null>(null);

  const run = async (key: string, fn: () => Promise<void>) => {
    setBusyKey(key);
    setRowError(null);
    try {
      await fn();
    } catch (e) {
      setRowError({ key, msg: errorMessage(e) });
    } finally {
      setBusyKey(null);
    }
  };

  const headingStyle = { fontFamily: "'Lora', serif" } as const;
  const bodyStyle = { fontFamily: "'DM Sans', sans-serif" } as const;

  const onAssignmentChange = (agentId: string, value: string) => {
    if (value === DEFAULT_OPTION) {
      void run(agentId, () => unassign(agentId));
    } else {
      void run(agentId, () => assign(value, agentId));
    }
  };

  return (
    <div className="space-y-5 rounded-md border border-[#e0dbd0] bg-white p-5">
      <div>
        <h3 className="text-base font-semibold text-[#1c1917]" style={headingStyle}>
          Google accounts
        </h3>
        <p className="mt-0.5 text-sm text-[#8a8578]" style={bodyStyle}>
          Connect one or more Google accounts and choose which account each
          agent acts as.
        </p>
      </div>

      {error && (
        <div className="space-y-2">
          <p className="text-sm text-[#b63025]">Failed to load Google accounts.</p>
          <Button variant="outline" size="sm" onClick={() => mutate()}>
            Retry
          </Button>
        </div>
      )}

      {!error && isLoading && (
        <p className="text-sm text-[#8a8578]" style={bodyStyle}>
          Loading Google accounts…
        </p>
      )}

      {!error && !isLoading && (
        <>
          {/* Connected accounts */}
          <div className="space-y-2">
            <p className="text-xs font-medium uppercase tracking-wide text-[#8a8578]">
              Connected accounts
            </p>
            {accounts.length === 0 ? (
              <p className="text-sm text-[#8a8578]" style={bodyStyle}>
                No Google accounts connected yet.
              </p>
            ) : (
              <ul className="space-y-2">
                {accounts.map((account) => {
                  const busy = busyKey === account.email;
                  const showErr =
                    rowError?.key === account.email ? rowError.msg : null;
                  return (
                    <li
                      key={account.email}
                      className="flex items-center gap-3 rounded-md border border-[#e0dbd0] bg-[#faf7f2] p-3"
                    >
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium text-[#1c1917]">
                          {account.email}
                        </p>
                      </div>
                      <div className="flex flex-col items-end gap-1">
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={busy || !canManage}
                          onClick={() =>
                            run(account.email, () =>
                              disconnectAccount(account.email),
                            )
                          }
                          aria-label={`Disconnect ${account.email}`}
                        >
                          {busy ? "Disconnecting…" : "Disconnect"}
                        </Button>
                        {showErr && (
                          <p className="text-xs text-[#b63025]" role="alert">
                            {showErr}
                          </p>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          {/* Connect another account */}
          <div className="space-y-1">
            <ConnectAnotherButton canManage={canManage} />
            <p className="text-xs text-[#8a8578]" style={bodyStyle}>
              Adding an account sends you to Google to sign in. You&apos;ll
              return via Settings with the account connected.
            </p>
          </div>

          {/* Per-agent account assignment */}
          <div className="space-y-2">
            <p className="text-xs font-medium uppercase tracking-wide text-[#8a8578]">
              Per-agent accounts
            </p>
            {agentsLoading ? (
              <p className="text-sm text-[#8a8578]" style={bodyStyle}>
                Loading agents…
              </p>
            ) : agents.length === 0 ? (
              <p className="text-sm text-[#8a8578]" style={bodyStyle}>
                No agents yet.
              </p>
            ) : accounts.length === 0 ? (
              <p className="text-sm text-[#8a8578]" style={bodyStyle}>
                Connect a Google account to assign it to an agent.
              </p>
            ) : (
              <ul className="space-y-2">
                {agents.map((agent) => {
                  const current = assignments[agent.id] ?? DEFAULT_OPTION;
                  const busy = busyKey === agent.id;
                  const showErr =
                    rowError?.key === agent.id ? rowError.msg : null;
                  const selectId = `google-assign-${agent.id}`;
                  return (
                    <li
                      key={agent.id}
                      className="flex items-center gap-3 rounded-md border border-[#e0dbd0] bg-[#faf7f2] p-3"
                    >
                      <label
                        htmlFor={selectId}
                        className="min-w-0 flex-1 truncate text-sm font-medium text-[#1c1917]"
                      >
                        {agentDisplayName(agent)}
                      </label>
                      <div className="flex flex-col items-end gap-1">
                        <select
                          id={selectId}
                          value={current}
                          disabled={busy || !canManage}
                          onChange={(e) =>
                            onAssignmentChange(agent.id, e.target.value)
                          }
                          className="h-9 w-52 rounded-md border border-[#e0dbd0] bg-white px-2 text-sm text-[#1c1917] disabled:cursor-not-allowed disabled:opacity-50"
                          style={bodyStyle}
                        >
                          <option value={DEFAULT_OPTION}>Default</option>
                          {accounts.map((account) => (
                            <option key={account.email} value={account.email}>
                              {account.email}
                            </option>
                          ))}
                        </select>
                        {showErr && (
                          <p className="text-xs text-[#b63025]" role="alert">
                            {showErr}
                          </p>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </>
      )}
    </div>
  );
}

/**
 * The "Connect another Google account" button owns its own busy/error state
 * (the redirect can fail before navigation, e.g. a 403 for a non-admin) so a
 * failure here doesn't clobber a row-level error elsewhere in the section.
 */
function ConnectAnotherButton({ canManage }: { canManage: boolean }) {
  const { connectAnother } = useGoogleAccounts();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const onClick = async () => {
    setBusy(true);
    setErr(null);
    try {
      await connectAnother();
      // On success the browser navigates away; nothing else runs.
    } catch (e) {
      setErr(errorMessage(e));
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col items-start gap-1">
      <Button size="sm" disabled={busy || !canManage} onClick={onClick}>
        {busy ? "Redirecting…" : "Connect another Google account"}
      </Button>
      {err && (
        <p className="text-xs text-[#b63025]" role="alert">
          {err}
        </p>
      )}
    </div>
  );
}
