"use client";

import { useEffect, useState } from "react";
import useSWR from "swr";
import {
  APPROVALS_LABEL, HOLDS_PATH, answerHold, approvalsLinked, holdAnswers, holdText, listHolds, setApprovalsLinked,
  type HoldAnswer, type NetworkHold,
} from "@lightbulb/shared";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";
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
      setFailure("That didn't go through. Try again.");
    } finally {
      setBusy(false);
    }
  };
  return <li className="for-you-entry" data-for-you-id={`network_hold:${hold.hold_id}`}>
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
  const api = useApi();
  const [linked, setLinked] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let cancelled = false;
    // No connected workspace (or no answer): no switch.
    approvalsLinked(api).then((value) => { if (!cancelled) setLinked(value); }).catch(() => undefined);
    return () => { cancelled = true; };
    // `api` identity churns every render; fetch once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (linked === null) return null;
  const toggle = async (next: boolean) => {
    setBusy(true);
    try {
      await setApprovalsLinked(api, next);
      setLinked(next);
    } catch (err) {
      notifyApiError(err, "Couldn't save the Allow requests setting");
    } finally {
      setBusy(false);
    }
  };
  return <div className="settings-card">
    <label className="settings-card-header" style={{ cursor: "pointer" }}>
      <div>
        <div className="settings-card-title">{APPROVALS_LABEL}</div>
        <div className="settings-card-desc">
          When a teammate&apos;s agent first messages one of your sessions, your Mac asks you. Turn this on to answer here
          and on your phone too.
        </div>
      </div>
      <input type="checkbox" aria-label={APPROVALS_LABEL} checked={linked} disabled={busy}
        onChange={(event) => void toggle(event.target.checked)} />
    </label>
  </div>;
}
