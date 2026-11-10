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
  status: "not_enrolled" | "pending_payment" | "active" | "past_due" | "canceling" | "cancellation_pending" | "canceled";
  available: boolean;
  planVersion: "voice-agents-v1";
  monthlyUsd: number;
  includedMinutes: number;
  overageUsdPerMinute: number;
  endsAt?: string | null;
}

const label = {
  not_enrolled: "Choose the voice plan",
  pending_payment: "Payment confirmation pending",
  active: "Voice plan active",
  past_due: "Payment needs attention",
  canceling: "Renewal canceled",
  cancellation_pending: "Confirming cancellation",
  canceled: "Voice plan ended",
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
      setProblem("Couldn’t confirm the update. Refresh the plan status before trying again.");
    } finally {
      setBusy(false);
    }
  }

  return <section className="vx-card" aria-labelledby="voice-plan-heading">
    <h2 className="vx-h" id="voice-plan-heading">Voice plan</h2>
    {(!current || isLoading) && !error && <p className="vx-sub" role="status">Loading your plan…</p>}
    {error && <p role="alert">Couldn’t load your voice plan. <Button variant="ghost" onClick={() => void mutate()}>Retry</Button></p>}
    {current && <>
      <p className="vx-sub" role="status">{label[plan.status]}</p>
      <p className="vx-sub">${plan.monthlyUsd}/month includes {plan.includedMinutes.toLocaleString()} voice minutes. Additional minutes cost ${plan.overageUsdPerMinute.toFixed(2)} each. It renews monthly; you can enter a promotion code at checkout.</p>
      {plan.status === "not_enrolled" && !plan.available && <p className="vx-sub">Voice plan enrollment isn’t available yet.</p>}
      {plan.status === "not_enrolled" && plan.available && admin && <>
        <label className="vx-choice"><input type="checkbox" checked={accepted} onChange={(event) => setAcceptedFor(event.target.checked ? consentKey : null)} disabled={busy} />
          <span>I agree to ${plan.monthlyUsd}/month and ${plan.overageUsdPerMinute.toFixed(2)} per minute after {plan.includedMinutes.toLocaleString()} included minutes.</span>
        </label>
        <Button disabled={!enabled || !accepted} onClick={() => void update("checkout")}>{busy ? "Opening payment…" : "Continue to payment"}</Button>
      </>}
      {plan.status === "pending_payment" && <p className="vx-sub">Your plan becomes active after payment is confirmed.</p>}
      {plan.status !== "not_enrolled" && admin &&
        <Button variant="outline" disabled={!enabled} onClick={() => void update("portal")}>Manage billing</Button>}
      {plan.endsAt && <p className="vx-sub">{plan.status === "canceled" ? "Ended" : ["active", "canceling"].includes(plan.status) ? "Access continues through" : "Renewal ends"} {new Date(plan.endsAt).toLocaleString(undefined, { timeZone: "UTC", timeZoneName: "short" })}.{!["active", "canceling", "canceled"].includes(plan.status) && " Access requires confirmed payment."}</p>}
      {plan.status === "canceled" && <p className="vx-sub">Contact support to start a new plan.</p>}
      {plan.status === "cancellation_pending" && admin && <Button variant="outline" disabled={!enabled} onClick={() => void update("cancel")}>Retry cancellation</Button>}
      {["active", "pending_payment", "past_due"].includes(plan.status) && admin && <div style={{ marginTop: 16 }}>
        {confirmCancel ? <>
          <p className="vx-sub">Cancel the next renewal? Your confirmed paid access continues until the end of its billing period.</p>
          <Button variant="outline" disabled={!enabled} onClick={() => void update("cancel")}>Confirm cancellation</Button>{" "}
          <Button variant="ghost" disabled={busy} onClick={() => setConfirmCancel(false)}>Keep plan</Button>
        </> : <Button variant="ghost" disabled={!enabled} onClick={() => setConfirmCancel(true)}>Cancel renewal</Button>}
      </div>}
      {!admin && <p className="vx-sub">A workspace admin can manage the voice plan.</p>}
    </>}
    {problem && <p role="alert">{problem} <Button variant="ghost" onClick={() => void mutate()}>Refresh status</Button></p>}
  </section>;
}
