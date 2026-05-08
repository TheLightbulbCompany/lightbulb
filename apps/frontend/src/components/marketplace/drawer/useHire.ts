"use client";

import type { Listing } from "@/lib/marketplace/types";

export function useHire() {
  return {
    hire: async (_listing: Listing) => {
      // Real implementation lands in Task 10 (free deploy + paid Stripe redirect).
    },
    isHiring: false,
  };
}
