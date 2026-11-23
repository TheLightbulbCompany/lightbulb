import type { Metadata } from "next";

import { PhoneLanding } from "@/components/landing/PhoneLanding";
import { getPricing } from "@/lib/pricing";
import {
  JsonLd,
  PHONE_DESCRIPTION,
  PHONE_TITLE,
  pageMetadata,
  phoneJsonLd,
} from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: PHONE_TITLE,
  description: PHONE_DESCRIPTION,
  path: "/phone",
});

// Lightbulb Phone's page for a salon, spa or barbershop owner: one day at the front desk (PhoneLanding).
// The price line comes from the billing API (null shows neutral copy).
export default async function PhonePage() {
  const pricing = await getPricing();
  return (
    <>
      <JsonLd data={phoneJsonLd} />
      <PhoneLanding pricing={pricing} />
    </>
  );
}
