import { SmoothScroll } from "@/components/providers/SmoothScroll";
import { ScrollManager } from "@/components/landing/ScrollManager";
import { Navbar } from "@/components/landing/Navbar";
import { Hero } from "@/components/landing/Hero";
import { HowItWorks } from "@/components/landing/HowItWorks";
import { Features } from "@/components/landing/Features";
import { Team } from "@/components/landing/Team";
import { MarketplaceSection } from "@/components/landing/MarketplaceSection";
import { Pricing } from "@/components/landing/Pricing";
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
        <HowItWorks />
        <div className="ld-rule">
          <span>pod_contents</span>
        </div>
        <Features />
        <div className="ld-rule">
          <span>org_chart</span>
        </div>
        <Team />
        <MarketplaceSection />
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
