"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import useSWR from "swr";
import { Button } from "@/components/ui/button";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { useApi } from "@/lib/api";
import { openAuthUrl } from "@/lib/externalAuth";

interface VoicePlan {
  ownerId: string;
  status: "not_enrolled" | "pending_payment" | "active" | "past_due" | "canceling" | "canceled";
  available: boolean;
  planVersion: "voice-agents-v1";
  monthlyUsd: number;
  includedMinutes: number;
  overageUsdPerMinute: number;
  endsAt?: string | null;
}

const label = {
  not_enrolled: "No plan yet",
  pending_payment: "Waiting for payment to confirm",
  active: "Phone plan active",
  past_due: "Your last payment didn’t go through",
  canceling: "Renewal canceled",
  canceled: "Phone plan ended",
};

export function VoicePlanCard() {
  const api = useApi();
  const { orgId, userId, isLoaded } = useAuth();
  const scope = orgId ?? userId;
  const admin = useIsWorkspaceAdmin();
  const { data: plan, error, mutate, isLoading } = useSWR<VoicePlan>(
    isLoaded && userId ? ["/billing/voice-plan", scope, userId] : null,
    () => api.get("/billing/voice-plan") as Promise<VoicePlan>,
    { refreshInterval: 15_000 },
  );
  const consentKey = `${userId}:${scope}:${plan?.planVersion ?? ""}`;
  const [acceptedFor, setAcceptedFor] = useState<string | null>(null);
  const accepted = acceptedFor === consentKey;
  const [busy, setBusy] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [problem, setProblem] = useState("");
  useEffect(() => {
    setAcceptedFor(null);
    setConfirmCancel(false);
    setProblem("");
  }, [consentKey]);

  const current = !!plan && plan.ownerId === scope;
  const enabled = admin && current && !busy && !error;

  async function update(kind: "checkout" | "cancel" | "portal") {
    if (!plan || !enabled || (kind === "checkout" && !accepted)) return;
    setBusy(true);
    setProblem("");
    try {
      if (kind === "portal") {
        const { portal_url } = await api.post("/billing/portal", {}) as { portal_url: string };
        await openAuthUrl(portal_url);
      } else if (kind === "cancel") {
        await api.del(`/billing/voice-plan?expectedOwnerId=${encodeURIComponent(plan.ownerId)}`);
        setConfirmCancel(false);
      } else {
        const result = await api.post("/billing/voice-plan/checkout", {
          expectedOwnerId: plan.ownerId,
          planVersion: plan.planVersion,
          acceptMonthlyUsd: plan.monthlyUsd,
          acceptIncludedMinutes: plan.includedMinutes,
          acceptOverageUsdPerMinute: plan.overageUsdPerMinute,
        }) as { checkoutUrl?: string };
        if (result.checkoutUrl) await openAuthUrl(result.checkoutUrl);
      }
      await mutate();
    } catch {
      setProblem("We couldn’t confirm that change. Refresh the status to see where it stands before you try again.");
    } finally {
      setBusy(false);
    }
  }

  return <section className="vx-card" aria-labelledby="voice-plan-heading">
    <h2 className="vx-h" id="voice-plan-heading">Phone plan</h2>
    {(!current || isLoading) && !error && <p className="vx-sub" role="status">Loading your plan…</p>}
    {error && <p role="alert">Couldn’t load your Phone plan. <Button variant="ghost" onClick={() => void mutate()}>Retry</Button></p>}
    {current && <>
      <p className="vx-sub" role="status">{label[plan.status]}</p>
      <p className="vx-sub">${plan.monthlyUsd}/month includes {plan.includedMinutes.toLocaleString()} voice minutes. Additional minutes cost ${plan.overageUsdPerMinute.toFixed(2)} each. Month to month: you can cancel the renewal here at any time.</p>
      {plan.status === "not_enrolled" && !plan.available && <p className="vx-sub">You can’t start a plan here yet. Contact support to get started.</p>}
      {plan.status === "not_enrolled" && plan.available && admin && <>
        <label className="vx-choice"><input type="checkbox" checked={accepted} onChange={(event) => setAcceptedFor(event.target.checked ? consentKey : null)} disabled={busy} />
          <span>I agree to ${plan.monthlyUsd}/month and ${plan.overageUsdPerMinute.toFixed(2)} per minute after {plan.includedMinutes.toLocaleString()} included minutes.</span>
        </label>
        <Button disabled={!enabled || !accepted} onClick={() => void update("checkout")}>{busy ? "Opening payment…" : "Continue to payment"}</Button>
      </>}
      {plan.status === "pending_payment" && <p className="vx-sub">Your plan becomes active after payment is confirmed.</p>}
      {plan.status === "past_due" && <p className="vx-sub">Update your card in Manage billing.</p>}
      {plan.status !== "not_enrolled" && admin &&
        <Button variant="outline" disabled={!enabled} onClick={() => void update("portal")}>Manage billing</Button>}
      {plan.endsAt && <p className="vx-sub">{plan.status === "canceled" ? "Ended" : ["active", "canceling"].includes(plan.status) ? "Access continues through" : "Renewal ends"} {new Date(plan.endsAt).toLocaleString(undefined, { timeZone: "UTC", timeZoneName: "short" })}.{!["active", "canceling", "canceled"].includes(plan.status) && " Access requires confirmed payment."}</p>}
      {plan.status === "canceled" && <p className="vx-sub">Contact support to start a new plan.</p>}
      {["active", "pending_payment", "past_due"].includes(plan.status) && admin && <div style={{ marginTop: 16 }}>
        {confirmCancel ? <>
          <p className="vx-sub">Cancel the next renewal? What you’ve already paid for stays on until the end of its billing period. The plan won’t renew after that.</p>
          <Button variant="outline" disabled={!enabled} onClick={() => void update("cancel")}>Confirm cancellation</Button>{" "}
          <Button variant="ghost" disabled={busy} onClick={() => setConfirmCancel(false)}>Keep plan</Button>
        </> : <Button variant="ghost" disabled={!enabled} onClick={() => setConfirmCancel(true)}>Cancel renewal</Button>}
      </div>}
      {!admin && <p className="vx-sub">Only an admin of this business can change the plan.</p>}
    </>}
    {problem && <p role="alert">{problem} <Button variant="ghost" onClick={() => void mutate()}>Refresh status</Button></p>}
  </section>;
}
