"use client";

import Link from "next/link";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { useCredits, isCreditsBlocked } from "@/hooks/useCredits";

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

  // The provider gate + exhausted check live together in isCreditsBlocked so
  // this banner and FirstRunFailedNotice — which can render on the SAME screen
  // — can never disagree about whether the owner is out of credits.
  if (!isCreditsBlocked(balance, me?.provider_choice)) return null;

  return (
    <div className="bg-cream border-b border-err px-4 py-2 text-ui text-err flex items-center justify-between">
      <span>You&apos;re out of workspace credits. Top up to keep chatting.</span>
      <Link href="/chat?panel=llm" className="underline">
        Top up now
      </Link>
    </div>
  );
}
