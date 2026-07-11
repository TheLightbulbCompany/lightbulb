"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

/**
 * /settings/billing — a stable landing URL for the provision gate's
 * `action_href` and the Stripe billing-portal `return_url`. Both point at
 * /settings/billing; the settings page itself keys the billing pane off
 * `?panel=billing`, so this route just forwards there.
 */
export default function BillingSettingsPage() {
  const router = useRouter();
  useEffect(() => {
    router.replace("/settings?panel=billing");
  }, [router]);
  return null;
}
