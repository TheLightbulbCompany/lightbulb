import { Navbar } from "@/components/landing/Navbar";
import { HeroFunnel } from "@/components/landing/HeroFunnel";
import { WorkspaceDemo } from "@/components/landing/WorkspaceDemo";
import { MarketplaceTeaser } from "@/components/landing/MarketplaceTeaser";
import { PricingThreeCard as Pricing } from "@/components/landing/PricingThreeCard";
import { Footer } from "@/components/landing/Footer";

// Funnel-first landing page: the first-run funnel IS the hero (answers stash
// to sessionStorage and replay after signup via useLandingBriefReplay), the
// workspace demo shows the product working, and the page gets out of the way.
export default function LandingPage() {
  return (
    <main className="ld-page min-h-screen">
      <Navbar />
      <HeroFunnel />
      <WorkspaceDemo />
      <MarketplaceTeaser />
      <Pricing />
      <Footer />
    </main>
  );
}
