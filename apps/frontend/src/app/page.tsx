import { SmoothScroll } from "@/components/providers/SmoothScroll";
import { ScrollManager } from "@/components/landing/ScrollManager";
import { Navbar } from "@/components/landing/Navbar";
import { Hero } from "@/components/landing/Hero";
import { Features } from "@/components/landing/Features";
import { Isolation } from "@/components/landing/Isolation";
import { Team } from "@/components/landing/Team";
import { MarketplaceSection } from "@/components/landing/MarketplaceSection";
import { PricingThreeCard as Pricing } from "@/components/landing/PricingThreeCard";
import { FAQ } from "@/components/landing/FAQ";
import { GooseTownTransition, GooseTown } from "@/components/landing/GooseTown";
import { Footer } from "@/components/landing/Footer";

export default function LandingPage() {
  return (
    <SmoothScroll>
      <main className="ld-page min-h-screen">
        <ScrollManager />
        <Navbar />
        <Hero />
        <div className="ld-rule">
          <span>org_chart</span>
        </div>
        <Team />
        <div className="ld-rule">
          <span>cloud_sandbox</span>
        </div>
        <Isolation />
        <MarketplaceSection />
        <div className="ld-rule">
          <span>what_you_get</span>
        </div>
        <Features />
        <Pricing />
        <div className="ld-rule">
          <span>open_questions</span>
        </div>
        <FAQ />
        <GooseTownTransition />
        <GooseTown />
        <Footer />
      </main>
    </SmoothScroll>
  );
}
