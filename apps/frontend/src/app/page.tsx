import type { Metadata } from "next";
import { Navbar } from "@/components/landing/Navbar";
import { HomeDemos } from "@/components/landing/HomeDemos";
import { HomeProducts } from "@/components/landing/HomeProducts";
import { Footer } from "@/components/landing/Footer";
import { HOME_DESCRIPTION, HOME_TITLE, JsonLd, hubJsonLd, pageMetadata } from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: HOME_TITLE,
  description: HOME_DESCRIPTION,
  path: "/",
});

// The company home: one line, then each product shown working (HomeDemos), then a section per
// product with its own door. No product leads: the demos open on a random one, and the products
// read Phone, Spark, Meetings (Meetings last, it is waitlisted).
// The H1 is the founder's wording (20 Nov), his one exception to "no AI in a bold headline".
export default function HomePage() {
  return (
    <main className="ld-page min-h-screen">
      <JsonLd data={hubJsonLd} />
      <Navbar />
      <header className="lb-hero lb-hub hd-hero">
        <div className="lb-wrap">
          <h1>The voice AI application company.</h1>
          <p className="lb-sub">We make assistants you talk to, and that talk back. Watch each one work.</p>
          <HomeDemos />
          <div className="hp-ctas hp-center hd-doors">
            <a className="lb-btn lb-btn-s lb-btn-xl" href="#phone">
              Lightbulb Phone
            </a>
            <a className="lb-btn lb-btn-s lb-btn-xl" href="#spark">
              Spark for Mac
            </a>
            <a className="lb-btn lb-btn-s lb-btn-xl" href="#meetings">
              Lightbulb Meetings
            </a>
          </div>
        </div>
      </header>
      <HomeProducts />
      {/* The home closes on its own three-product band (HomeProducts), not the footer's Phone one. */}
      <Footer cta={false} />
    </main>
  );
}
