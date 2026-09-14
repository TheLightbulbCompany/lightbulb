"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiError, useGoogleAccounts } from "@/hooks/useGoogleAccounts";
import { agentDisplayName, useAgents } from "@/hooks/useAgents";
import { notifyApiError } from "@/lib/notify";
import { isTauri } from "@/lib/externalAuth";

interface GoogleAccountsSectionProps {
  /** Org admins may manage accounts/assignments; non-admins see read-only. */
  canManage: boolean;
}

/** Sentinel select value for "no explicit assignment" → the agent is told
 * nothing and falls back to bare `gog`. Kept distinct from any email so it can
 * never collide. */
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
 * Google multi-account management. Mounted on BOTH Google surfaces — the
 * workspace Connections panel (which is also where `connectAnother`'s OAuth
 * round trip lands) and the Control dashboard's Integrations tab — so there is
 * no surface where a connected Google reads as single-account-only.
 *
 * Three parts, in the order the work is done:
 *
 *   1. Connect another — additive OAuth, in the header where an add-action
 *      belongs rather than stranded between the two lists.
 *   2. Connected accounts — each account, with how many agents act as it, and
 *      a per-row Disconnect.
 *   3. Per-agent accounts — which account each agent acts as. This is the
 *      identity binding: it writes a standing `gog -a <email>` instruction
 *      into the agent's IDENTITY.md, so it governs every run of that agent,
 *      not one task. The copy says so, because an owner who does not know
 *      that reaches for the wrong lever (an automation's account picker) and
 *      points an agent at the wrong mailbox.
 *
 * Mutations are admin-gated; non-admins see disabled controls. State lives in
 * useGoogleAccounts (SWR cache) + useAgents (gateway RPC); this component owns
 * only transient per-row busy flags.
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

  // Track which row (keyed by email or agent id) is mid-request so spinners
  // stay row-local.
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const run = async (key: string, fn: () => Promise<void>) => {
    setBusyKey(key);
    try {
      await fn();
    } catch (e) {
      notifyApiError(e, errorMessage(e));
    } finally {
      setBusyKey(null);
    }
  };

  const headingStyle = { fontFamily: "'Lora', serif" } as const;

  const onAssignmentChange = (agentId: string, value: string) => {
    if (value === DEFAULT_OPTION) {
      void run(agentId, () => unassign(agentId));
    } else {
      void run(agentId, () => assign(value, agentId));
    }
  };

  return (
    <section
      id="google-accounts"
      aria-labelledby="google-accounts-heading"
      className="overflow-hidden rounded-md border border-[var(--edge)] bg-cream"
    >
      <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 border-b border-[var(--edge)] px-5 py-4">
        <div className="min-w-0 flex-1">
          <h3
            id="google-accounts-heading"
            className="text-base font-semibold text-[var(--midnight)]"
            style={headingStyle}
          >
            Google accounts
          </h3>
          <p className="mt-1 max-w-prose text-sm leading-relaxed text-[var(--midnight-muted)]">
            One Google connection can hold several accounts — a shared inbox, a
            second workspace — and each agent acts as one of them.
          </p>
        </div>
        <ConnectAnotherButton canManage={canManage} />
      </header>

      {error && (
        <div className="flex items-center gap-3 px-5 py-4">
          <p className="flex-1 text-sm text-[var(--err)]">
            Failed to load Google accounts.
          </p>
          <Button variant="outline" size="sm" onClick={() => mutate()}>
            Retry
          </Button>
        </div>
      )}

      {!error && isLoading && (
        <p className="px-5 py-4 text-sm text-[var(--midnight-muted)]">
          Loading Google accounts…
        </p>
      )}

      {!error && !isLoading && (
        <>
          <div className="border-b border-[var(--edge)] px-5 py-4">
            <p className="label-caps text-[var(--midnight-muted)]">
              Connected accounts
            </p>
            {accounts.length === 0 ? (
              <p className="mt-2 text-sm text-[var(--midnight-muted)]">
                No Google accounts connected yet.
              </p>
            ) : (
              <ul className="mt-3 divide-y divide-[var(--edge)] overflow-hidden rounded-sm border border-[var(--edge)]">
                {accounts.map((account) => {
                  const busy = busyKey === account.email;
                  // How many agents act as this account, so Disconnect is
                  // never blind — it strips the gog-account block from every
                  // agent bound to it.
                  const used = Object.values(assignments).filter(
                    (email) => email === account.email,
                  ).length;
                  return (
                    <li
                      key={account.email}
                      className="flex items-center gap-4 bg-[var(--cream)] px-3 py-2.5"
                    >
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium text-[var(--midnight)]">
                          {account.email}
                        </p>
                        <p className="mt-0.5 text-xs text-[var(--midnight-muted)]">
                          {used === 0
                            ? "No agent acts as this account"
                            : `${used} agent${used === 1 ? "" : "s"} act${used === 1 ? "s" : ""} as this account`}
                        </p>
                      </div>
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
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          <div className="px-5 py-4">
            <p className="label-caps text-[var(--midnight-muted)]">
              Which account each agent acts as
            </p>
            <p className="mt-1 max-w-prose text-sm leading-relaxed text-[var(--midnight-muted)]">
              An agent given an account is told to use it for every Google
              action, on every run. Leave it on{" "}
              <span className="text-[var(--midnight-soft)]">Default</span> and the
              agent is never told which account to use.
            </p>
            {agentsLoading ? (
              <p className="mt-3 text-sm text-[var(--midnight-muted)]">Loading agents…</p>
            ) : agents.length === 0 ? (
              <p className="mt-3 text-sm text-[var(--midnight-muted)]">No agents yet.</p>
            ) : accounts.length === 0 ? (
              <p className="mt-3 text-sm text-[var(--midnight-muted)]">
                Connect a Google account to assign it to an agent.
              </p>
            ) : (
              <ul className="mt-3 divide-y divide-[var(--edge)] overflow-hidden rounded-sm border border-[var(--edge)]">
                {agents.map((agent) => {
                  const current = assignments[agent.id] ?? DEFAULT_OPTION;
                  const busy = busyKey === agent.id;
                  const selectId = `google-assign-${agent.id}`;
                  return (
                    <li
                      key={agent.id}
                      className="flex items-center gap-4 bg-[var(--cream)] px-3 py-2.5"
                    >
                      <label
                        htmlFor={selectId}
                        className="min-w-0 flex-1 truncate text-sm font-medium text-[var(--midnight)]"
                      >
                        {agentDisplayName(agent)}
                      </label>
                      <select
                        id={selectId}
                        value={current}
                        disabled={busy || !canManage}
                        onChange={(e) =>
                          onAssignmentChange(agent.id, e.target.value)
                        }
                        className="h-9 w-56 rounded-md border border-[var(--edge)] bg-cream-deep px-2 text-sm text-[var(--midnight)] disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        <option value={DEFAULT_OPTION}>Default</option>
                        {accounts.map((account) => (
                          <option key={account.email} value={account.email}>
                            {account.email}
                          </option>
                        ))}
                      </select>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </>
      )}
    </section>
  );
}

/**
 * The "Connect another Google account" button owns its own busy state (the
 * redirect can fail before navigation, e.g. a 403 for a non-admin) so a
 * failure here doesn't clobber a row-level error elsewhere in the section.
 */
function ConnectAnotherButton({ canManage }: { canManage: boolean }) {
  const { connectAnother } = useGoogleAccounts();
  const [busy, setBusy] = useState(false);

  const onClick = async () => {
    setBusy(true);
    try {
      await connectAnother();
      // Web: the browser has navigated away by now. Desktop (Tauri): the
      // consent screen opened in the system browser and connectAnother
      // resolved after polling for the new grant — clear busy so the button
      // comes back either way.
    } catch (e) {
      notifyApiError(e, errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Button
      className="shrink-0"
      size="sm"
      disabled={busy || !canManage}
      onClick={onClick}
    >
      {busy
        ? isTauri()
          ? "Finish in your browser…"
          : "Redirecting…"
        : "Connect another account"}
    </Button>
  );
}
