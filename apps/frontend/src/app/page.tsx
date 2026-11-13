import type { Metadata } from "next";
import { Navbar } from "@/components/landing/Navbar";
import { Hero } from "@/components/landing/Hero";
import { ProofBand } from "@/components/landing/ProofBand";
import { HowItWorks } from "@/components/landing/HowItWorks";
import { WorkspaceBand } from "@/components/landing/WorkspaceBand";
import { NetworkBand } from "@/components/landing/NetworkBand";
import { PricingThreeCard as Pricing } from "@/components/landing/PricingThreeCard";
import { getPricing } from "@/lib/pricing";
import { AskAI } from "@/components/landing/AskAI";
import { FoundingTeams } from "@/components/landing/FoundingTeams";
import { HomeFaq } from "@/components/landing/HomeFaq";
import { ProblemBand } from "@/components/landing/ProblemBand";
import { ProofStories } from "@/components/landing/ProofStories";
import { Footer } from "@/components/landing/Footer";
import {
  HOME_DESCRIPTION,
  HOME_TITLE,
  JsonLd,
  homeJsonLd,
  pageMetadata,
} from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: HOME_TITLE,
  description: HOME_DESCRIPTION,
  path: "/",
});

// The homepage in the plan's order (docs/marketing 10 Section A): hero, the
// problem, how it works (team first), see the work, proof, price, founding
// teams, FAQ, then Ask AI.
//
// NOTE: the previous funnel hero captured a brief to sessionStorage which
// useLandingBriefReplay replayed after activation. That capture was removed
// with the redesign (owner decision, 2026-09-11) — the hook still exists but
// has no producer on this page.
export default async function LandingPage() {
  const pricing = await getPricing();
  return (
    <main className="ld-page min-h-screen">
      <JsonLd data={homeJsonLd} />
      <Navbar />
      <Hero />
      <ProblemBand />
      <NetworkBand />
      <HowItWorks />
      <WorkspaceBand />
      <ProofStories />
      <ProofBand />
      <Pricing pricing={pricing} />
      <FoundingTeams pricing={pricing} />
      <HomeFaq />
      <AskAI />
      <Footer />
    </main>
  );
}
