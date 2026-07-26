"use client";

import Link from "next/link";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { useCredits, isCreditsExhausted } from "@/hooks/useCredits";

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
  // chatgpt_oauth / byo_key users see a false "out of credits" banner.
  // isCreditsExhausted is the shared predicate (see useCredits.ts) — it
  // prefers the server-computed is_exhausted (gate's own admission
  // threshold, not `> 0`) but falls back to the legacy balance_microcents
  // <= 0 check while the field is absent during a frontend-ahead deploy
  // skew, so a genuinely zero-balance owner is never left with no banner.
  if (me?.provider_choice !== "bedrock_claude") return null;
  if (!isCreditsExhausted(balance)) return null;

  return (
    <div className="bg-destructive/10 border-b border-destructive/20 px-4 py-2 text-sm flex items-center justify-between">
      <span className="text-destructive">
        You&apos;re out of Claude credits. Top up to keep chatting.
      </span>
      {/*
        Route into the CreditsStep flow inside the ProvisioningStepper.
        A link to /chat?panel=credits would strip the ?provider= URL param
        the stepper uses to gate the provider step, causing the wizard to
        proceed into container provisioning without any payment — and
        credits now live inside the Model & Credits panel, which isn't the
        stepper flow this banner must route into. URL stays inside /chat
        so we don't lose ChatLayout chrome / the in-flight gateway socket.
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
