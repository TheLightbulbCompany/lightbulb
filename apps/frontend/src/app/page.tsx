import { SmoothScroll } from "@/components/providers/SmoothScroll";
import { ScrollManager } from "@/components/landing/ScrollManager";
import { Navbar } from "@/components/landing/Navbar";
import { Hero } from "@/components/landing/Hero";
import { Features } from "@/components/landing/Features";
import { Team } from "@/components/landing/Team";
import { MarketplaceSection } from "@/components/landing/MarketplaceSection";
import { Pricing } from "@/components/landing/Pricing";
import { FAQ } from "@/components/landing/FAQ";
import { Footer } from "@/components/landing/Footer";

export default function LandingPage() {
  return (
    <SmoothScroll>
      <main className="min-h-screen bg-[#faf7f2]">
        <ScrollManager />
        <Navbar />
        <Hero />
        <Features />
        <Team />
        <MarketplaceSection />
        <Pricing />
        <FAQ />
        <Footer />
      </main>
    </SmoothScroll>
  );
}
