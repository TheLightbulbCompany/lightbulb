import type { Metadata } from "next";
import { Navbar } from "@/components/landing/Navbar";
import { HeroFunnel } from "@/components/landing/HeroFunnel";
import { WorkspaceDemo } from "@/components/landing/WorkspaceDemo";
import { MarketplaceTeaser } from "@/components/landing/MarketplaceTeaser";
import { PricingThreeCard as Pricing } from "@/components/landing/PricingThreeCard";
import { Footer } from "@/components/landing/Footer";
import { JsonLd, organizationJsonLd, pageMetadata, softwareApplicationJsonLd, websiteJsonLd } from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: "Lightbulb AI — AI Agents for Autonomous Businesses",
  description: "Run your company with persistent AI agents, cloud workspaces, and an agent marketplace for autonomous business operations.",
  path: "/",
});

// Funnel-first landing page: the first-run funnel IS the hero (answers stash
// to sessionStorage and replay after signup via useLandingBriefReplay), the
// workspace demo shows the product working, and the page gets out of the way.
export default function LandingPage() {
  return (
    <main className="ld-page min-h-screen">
      <JsonLd data={organizationJsonLd} />
      <JsonLd data={websiteJsonLd} />
      <JsonLd data={softwareApplicationJsonLd} />
      <Navbar />
      <HeroFunnel />
      <WorkspaceDemo />
      <MarketplaceTeaser />
      <Pricing />
      <Footer />
    </main>
  );
}
