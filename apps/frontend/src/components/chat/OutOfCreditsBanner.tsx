"use client";

import Link from "next/link";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { useCredits } from "@/hooks/useCredits";

interface UserMeResponse {
  user_id: string;
  provider_choice: string | null;
  byo_provider: string | null;
}

export function OutOfCreditsBanner() {
  const api = useApi();
  const { balance } = useCredits();
  const { data: me } = useSWR<UserMeResponse>(
    "/users/me",
    () => api.get("/users/me") as Promise<UserMeResponse>,
  );

  // Only the bedrock_claude path uses prepaid credits. Without the gate,
  // chatgpt_oauth / byo_key users see a false "out of credits" banner
  // because /billing/credits/balance returns 0 when no credits row exists.
  if (me?.provider_choice !== "bedrock_claude") return null;
  if (!balance || balance.balance_microcents > 0) return null;

  return (
    <div className="bg-destructive/10 border-b border-destructive/20 px-4 py-2 text-sm flex items-center justify-between">
      <span className="text-destructive">
        You&apos;re out of Claude credits. Top up to keep chatting.
      </span>
      {/*
        Route into the CreditsStep flow inside the ProvisioningStepper. A
        link to /chat?panel=credits would strip the ?provider= URL param
        the stepper uses to gate the provider step, causing the wizard to
        proceed into container provisioning without any payment — and the
        destination panel (CreditsPanel) is a placeholder that can't take
        a payment. URL stays inside /chat so we don't lose ChatLayout
        chrome / the in-flight gateway socket.
      */}
      <Link
        href="/chat?provider=bedrock_claude"
        className="rounded-md bg-destructive px-3 py-1 text-destructive-foreground text-xs"
      >
        Top up now
      </Link>
    </div>
  );
}
