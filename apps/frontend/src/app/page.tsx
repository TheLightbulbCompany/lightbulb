import type { Metadata } from "next";
import { Navbar } from "@/components/landing/Navbar";
import { HubProducts } from "@/components/landing/HubProducts";
import { Footer } from "@/components/landing/Footer";
import { HOME_DESCRIPTION, HOME_TITLE, JsonLd, hubJsonLd, pageMetadata } from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: HOME_TITLE,
  description: HOME_DESCRIPTION,
  path: "/",
});

// The company home: one line, then the three products, each with its own door.
// The H1 is the founder's wording (20 Nov), his one exception to "no AI in a bold headline".
export default function HomePage() {
  return (
    <main className="ld-page min-h-screen">
      <JsonLd data={hubJsonLd} />
      <Navbar />
      <header className="lb-hero lb-hub">
        <div className="lb-wrap">
          <h1>The voice AI application company.</h1>
          <p className="lb-sub">
            Lightbulb makes assistants you talk to. Lightbulb Phone answers your business line and books appointments.
            Lightbulb Meetings joins your Google Meet and keeps the notes. Spark is a voice app for the Mac.
          </p>
          <HubProducts />
        </div>
      </header>
      <Footer />
    </main>
  );
}
