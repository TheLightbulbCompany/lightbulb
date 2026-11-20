import type { Metadata } from "next";
import { Navbar } from "@/components/landing/Navbar";
import { Hero } from "@/components/landing/Hero";
import { ProblemBand } from "@/components/landing/ProblemBand";
import { HowItWorks } from "@/components/landing/HowItWorks";
import { AfterTheCall, InCharge, OnTheCall, Voices } from "@/components/landing/HomeSections";
import { PricingThreeCard as Pricing } from "@/components/landing/PricingThreeCard";
import { getPricing } from "@/lib/pricing";
import { HomeFaq } from "@/components/landing/HomeFaq";
import { Footer } from "@/components/landing/Footer";
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

// Lightbulb Phone's page for a salon, spa or barbershop owner: hero, the problem, setup,
// the call, the voices, what you see afterwards, the controls you keep, price,
// questions. The footer carries the final call to action.
export default async function PhonePage() {
  const pricing = await getPricing();
  return (
    <main className="ld-page min-h-screen">
      <JsonLd data={phoneJsonLd} />
      <Navbar />
      <Hero />
      <ProblemBand />
      <HowItWorks />
      <OnTheCall />
      <Voices />
      <AfterTheCall />
      <InCharge />
      <Pricing pricing={pricing} />
      <HomeFaq />
      <Footer />
    </main>
  );
}
