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
      <span>
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
        className="underline"
      >
        Top up now
      </Link>
    </div>
  );
}
