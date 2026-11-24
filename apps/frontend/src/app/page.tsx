import type { Metadata } from "next";

import { HomeLanding } from "@/components/landing/HomeLanding";
import { HOME_DESCRIPTION, HOME_TITLE, JsonLd, hubJsonLd, pageMetadata } from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: HOME_TITLE,
  description: HOME_DESCRIPTION,
  path: "/",
});

// The company home: "Voice to run your life, business and computer", a window to try each product, then one row
// per product (HomeLanding; owner-approved mockup, 2026-11-24).
export default function HomePage() {
  return (
    <>
      <JsonLd data={hubJsonLd} />
      <HomeLanding />
    </>
  );
}
