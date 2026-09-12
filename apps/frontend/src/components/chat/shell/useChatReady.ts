"use client";

import { useProvisioningState } from "@/hooks/useProvisioningState";

/**
 * "Chat is live" for the shells: the backend-reported `runtime_phase` reaching
 * "ready" — the same gate ProvisioningStepper uses to stop its loader (both
 * read the `/container/status` SWR cache through `useProvisioningState`) — or
 * a parked box, which is a healthy, finished boot that merely sleeps. Strictly
 * stronger than `useProvisioningState().phase === "normal"`, which is true for
 * ANY 200 from /container/status, including the whole cold-start window.
 */
export function useChatReady(): { chatReady: boolean } {
  const { container } = useProvisioningState();
  return { chatReady: container?.runtime_phase === "ready" || container?.lifecycle === "parked" };
}
