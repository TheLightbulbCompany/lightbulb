"use client";

import { useEffect, useRef, useState } from "react";

import { resolveAutomationRunner } from "@/components/teams/shared/lib/automationRunner";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { useGoogleAccounts } from "@/hooks/useGoogleAccounts";
import { notifyApiError } from "@/lib/notify";

interface AutomationGoogleAccountProps {
  /** Company roster — used to resolve the CEO, who runs every seeded automation. */
  agents: CompanyAgent[];
  /** The account the resource picker currently has selected. */
  account: string | null;
  /** The resource picker's options; null while still loading. */
  accountOptions: { id: string; label: string }[] | null;
  /** Select an account in the parent's resource picker. */
  onSelectAccount: (email: string) => void;
}

/**
 * The consequence + conflict rail under the automation dialog's Google account
 * picker.
 *
 * Two places name the Google account an agent acts as, and they are NOT the
 * same question:
 *
 *   - The per-agent assignment (Settings → Connections → Google accounts) is
 *     the agent's IDENTITY. `google_identity.seed_agent_google_identity` writes
 *     "ALWAYS run `gog -a <email>` — never bare gog, and never another agent's
 *     account" into IDENTITY.md, which OpenClaw auto-loads on every run. It is
 *     a standing, absolute instruction.
 *   - This picker is the WATCH TARGET. `inbound_triggers.seed_trigger_routine`
 *     registers a real Google push channel (or Gmail poll) against the chosen
 *     account, and interpolates it into the routine description.
 *
 * Neither can go away: the watch channel must name an account, and the
 * assignment is the only thing that puts an account in the agent's context for
 * interactive work. But they CAN disagree — and when they do the agent gets
 * contradictory instructions, with IDENTITY.md phrased as the absolute one.
 * The resolution: the assignment is the source of truth, this picker DEFAULTS
 * to it, and a deliberate divergence is stated plainly with a one-click
 * reconcile. It never rewrites the assignment silently — that write lands in
 * IDENTITY.md on EFS and governs every future run of the agent, which is far
 * more than "create one automation" implies.
 *
 * Every seeded automation is assigned to the CEO (`_find_friday_id` in
 * inbound_triggers.py), which nothing else in this dialog says. Naming the
 * runner is half the point: "Vera will read support@" was never on offer.
 */
export function AutomationGoogleAccount({
  agents,
  account,
  accountOptions,
  onSelectAccount,
}: AutomationGoogleAccountProps) {
  const { assignments, connectAnother, assign } = useGoogleAccounts();
  const [busy, setBusy] = useState(false);
  // Latch so the default only ever applies to an untouched picker — clearing a
  // selection back to empty must not silently re-fill it.
  const defaulted = useRef(false);

  // Resolved with the automation backend's OWN rootless-CEO rule, not the
  // `seeded` stamp — see resolveAutomationRunner. A legacy company with no
  // persisted seeded_agent_ids still gets its runner named and its mailbox
  // conflict flagged, because the backend still creates the automation for it.
  const ceo = resolveAutomationRunner(agents);
  const ceoId = ceo?.id;
  const ceoName = ceo?.name ?? "Lightbulb";
  const assigned = ceoId ? (assignments[ceoId] ?? null) : null;

  // Default the picker to the account the runner already acts as. Reflects
  // fetched state into the parent's selection exactly once, once both the
  // options and the assignment map have landed.
  useEffect(() => {
    if (defaulted.current || account || !assigned || !accountOptions) return;
    if (!accountOptions.some((o) => o.id === assigned)) return;
    defaulted.current = true;
    onSelectAccount(assigned);
  }, [account, assigned, accountOptions, onSelectAccount]);

  const conflicted = !!assigned && !!account && assigned !== account;

  // The redirect can fail before navigation (a 403 for a non-admin), which
  // would otherwise be a dead click.
  const openConnectAnother = async () => {
    try {
      await connectAnother();
    } catch (e) {
      notifyApiError(e, "Couldn't start the Google sign-in.");
    }
  };

  const makeItTheirs = async (email: string) => {
    if (!ceoId) return;
    setBusy(true);
    try {
      await assign(email, ceoId);
    } catch (e) {
      notifyApiError(e, "Couldn't set that account.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-2">
      {/* The consequence, stated. Reads at body weight because it is a fact
          about what is about to happen, not a footnote. When the picked
          account contradicts the agent's own, this line stops promising an
          outcome — the block below owns it, and two paragraphs must not
          disagree about what the agent will read. */}
      <p className="text-caption leading-relaxed text-[var(--midnight-soft)]">
        <span className="font-medium">{ceoName}</span> runs this automation
        {conflicted ? (
          "."
        ) : account ? (
          <>
            {" "}
            and will read <span className="font-medium">{account}</span>&apos;s
            Calendar events and Drive files.
          </>
        ) : (
          " and will read the account you pick."
        )}
      </p>

      {conflicted && (
        <div
          className="flex flex-col gap-2 rounded-md px-3 py-2.5"
          style={{
            border: "1px solid var(--warn)",
            background: "var(--cream)",
          }}
        >
          <p className="text-caption leading-relaxed text-[var(--midnight-soft)]">
            {ceoName} is set to act as{" "}
            <span className="font-medium">{assigned}</span>, and its standing
            instructions say to always use that account — so it may ignore{" "}
            <span className="font-medium">{account}</span>, which is what this
            automation watches.
          </p>
          <button
            type="button"
            disabled={busy}
            onClick={() => account && void makeItTheirs(account)}
            className="self-start text-left text-caption font-medium text-[var(--midnight)] underline underline-offset-2 disabled:opacity-50"
          >
            {busy ? "Saving…" : `Make ${account} ${ceoName}'s account`}
          </button>
        </div>
      )}

      {!conflicted && !assigned && account && ceoId && (
        <button
          type="button"
          disabled={busy}
          onClick={() => void makeItTheirs(account)}
          className="self-start text-left text-caption font-medium text-[var(--midnight)] underline underline-offset-2 disabled:opacity-50"
        >
          {busy
            ? "Saving…"
            : `Also make ${account} ${ceoName}'s account for its other work`}
        </button>
      )}

      {/* The way out. A hairline separates it from the facts above: those
          describe this automation, this is how you change the premise. Without
          it a one-option picker reads as "one account is all there is". */}
      <p className="border-t border-[var(--edge)] pt-2 text-caption leading-relaxed text-[var(--midnight-muted)]">
        Need a different mailbox?{" "}
        <button
          type="button"
          onClick={() => void openConnectAnother()}
          className="font-medium text-[var(--midnight)] underline underline-offset-2"
        >
          Connect another Google account
        </button>{" "}
        — this leaves the dialog and returns you to Settings → Connections.
      </p>
    </div>
  );
}
