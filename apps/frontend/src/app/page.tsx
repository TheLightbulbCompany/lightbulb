import type { Metadata } from "next";
import { Suspense } from "react";
import { LaunchRefCapture } from "@/components/launch/LaunchRefCapture";
import { Navbar } from "@/components/landing/Navbar";
import { Hero } from "@/components/landing/Hero";
import { ProofBand } from "@/components/landing/ProofBand";
import { HowItWorks } from "@/components/landing/HowItWorks";
import { WorkspaceBand } from "@/components/landing/WorkspaceBand";
import { PricingThreeCard as Pricing } from "@/components/landing/PricingThreeCard";
import { AskAI } from "@/components/landing/AskAI";
import { Footer } from "@/components/landing/Footer";
import {
  HOME_DESCRIPTION,
  HOME_TITLE,
  JsonLd,
  organizationJsonLd,
  pageMetadata,
  softwareApplicationJsonLd,
  websiteJsonLd,
} from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: HOME_TITLE,
  description: HOME_DESCRIPTION,
  path: "/",
});

// Messaging-workspace landing: the hero sells the room (people and agents in
// one channel), three steps show how it is set up, the band shows the product,
// and pricing closes.
//
// NOTE: the previous funnel hero captured a brief to sessionStorage which
// useLandingBriefReplay replayed after activation. That capture was removed
// with the redesign (owner decision, 2026-09-11) — the hook still exists but
// has no producer on this page.
export default function LandingPage() {
  return (
    <main className="ld-page min-h-screen">
      <JsonLd data={organizationJsonLd} />
      <JsonLd data={websiteJsonLd} />
      <JsonLd data={softwareApplicationJsonLd} />
      {/* Invisible: persists `/?ref=CODE` (the backend's referral link target). */}
      <Suspense fallback={null}>
        <LaunchRefCapture />
      </Suspense>
      <Navbar />
      <Hero />
      <ProofBand />
      <HowItWorks />
      <WorkspaceBand />
      <Pricing />
      <AskAI />
      <Footer />
    </main>
  );
}
