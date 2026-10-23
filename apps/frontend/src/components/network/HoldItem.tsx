"use client";

import { useState } from "react";
import useSWR from "swr";
import {
  APPROVALS_DESCRIPTION, APPROVALS_LABEL, HOLDS_PATH, HOLD_FAILED, answerHold, holdAnswers, holdFocusId, holdText, listHolds,
  useApprovalsLink, type HoldAnswer, type NetworkHold,
} from "@lightbulb/shared";
import { useApi } from "@/lib/api";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";

/** This person's open Lightbulb Connect holds, for For you's Needs-you rows. */
export function useNetworkHolds(): NetworkHold[] {
  const api = useApi();
  const { data } = useSWR(HOLDS_PATH, () => listHolds(api), { refreshInterval: 30_000 });
  return data ?? [];
}

/**
 * One hold as a For you row: who wants to reach which session on which computer, and the
 * answers it offers. The hold carries no message text, so none is shown.
 */
export function HoldItem({ hold }: { hold: NetworkHold }) {
  const api = useApi();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const answer = async (choice: HoldAnswer) => {
    setBusy(true);
    setFailure(null);
    try {
      setResult(await answerHold(api, hold, choice));
    } catch {
      setFailure(HOLD_FAILED);
    } finally {
      setBusy(false);
    }
  };
  return <li className="for-you-entry" data-for-you-id={holdFocusId(hold.hold_id)}>
    <div className="for-you-row">
      <div className="for-you-row__open">
        <span className="for-you-row__body">
          <span className="for-you-row__head">
            <strong>{hold.label}</strong>
            <time className="for-you-row__time" dateTime={hold.created_at}>{timeAgo(hold.created_at)}</time>
          </span>
          <span className="for-you-row__text">{holdText(hold)}</span>
        </span>
      </div>
      {result
        ? <span role="status" className="for-you-row__controls">{result}</span>
        : <span className="for-you-row__controls">
          {holdAnswers(hold).map((choice, index) => <button key={choice.id} type="button" disabled={busy}
            className={`editorial-btn editorial-btn--sm ${index === 0 ? "editorial-btn--primary" : choice.verdict === "refuse" ? "editorial-btn--ghost" : "editorial-btn--secondary"}`}
            onClick={() => void answer(choice)}>{choice.label}</button>)}
        </span>}
    </div>
    {failure && <p role="alert" className="for-you-entry__error">{failure}</p>}
  </li>;
}

/** Settings → Profile: route this person's holds to the phone and the web (on writes the link, off removes it). */
export function ApprovalsSwitch() {
  const { linked, busy, error, change } = useApprovalsLink(useApi());
  // No connected workspace (or no answer): no switch.
  if (linked === null) return null;
  return <div className="settings-card">
    <label className="settings-card-header" style={{ cursor: "pointer" }}>
      <div>
        <div className="settings-card-title">{APPROVALS_LABEL}</div>
        <div className="settings-card-desc">{APPROVALS_DESCRIPTION}</div>
        {error && <div role="alert" className="settings-card-desc">{error}</div>}
      </div>
      <input type="checkbox" aria-label={APPROVALS_LABEL} checked={linked} disabled={busy}
        onChange={(event) => change(event.target.checked)} />
    </label>
  </div>;
}
