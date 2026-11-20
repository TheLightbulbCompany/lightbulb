import type { Metadata } from "next";
import Link from "next/link";
import { Navbar } from "@/components/landing/Navbar";
import { Hero } from "@/components/landing/Hero";
import { HubCtas } from "@/components/landing/HeroCtas";
import { Footer } from "@/components/landing/Footer";
import { HOME_DESCRIPTION, HOME_TITLE, JsonLd, hubJsonLd, pageMetadata } from "@/lib/seo";

export const metadata: Metadata = pageMetadata({
  title: HOME_TITLE,
  description: HOME_DESCRIPTION,
  path: "/",
});

const PRODUCTS: { name: string; body: string; link: string; href: string }[] = [
  {
    name: "Lightbulb Phone",
    body: "Answers your calls day and night, books appointments, takes messages and hands callers to you.",
    link: "See Lightbulb Phone",
    href: "/phone",
  },
  {
    name: "Lightbulb Meetings",
    body: "Joins your Google Meet when invited, speaks only when named, and leaves notes, decisions and follow-ups.",
    link: "Join the Meetings waitlist",
    href: "/meetings",
  },
  { name: "Lightbulb OS", body: "A voice app for the Mac. Not out yet.", link: "Join the OS waitlist", href: "/os" },
];

// The company home: one line, the three products, each linking to its page.
// The H1 is the founder's wording (20 Nov), his one exception to "no AI in a bold headline".
export default function HomePage() {
  return (
    <main className="ld-page min-h-screen">
      <JsonLd data={hubJsonLd} />
      <Navbar />
      <Hero
        title="The number one voice AI application company."
        sub="Lightbulb makes assistants you talk to. Lightbulb Phone answers your business line and books appointments. Lightbulb Meetings joins your Google Meet and keeps the notes. Lightbulb OS is a voice app for the Mac."
        ctas={<HubCtas />}
      />
      <section className="lb-sec">
        <div className="lb-wrap">
          <h2 className="lb-h2">Three ways Lightbulb helps</h2>
          <ul className="lb-does">
            {PRODUCTS.map(({ name, body, link, href }) => (
              <li key={name}>
                <b>{name}</b>
                {body} <Link href={href}>{link}</Link>
              </li>
            ))}
          </ul>
        </div>
      </section>
      <Footer />
    </main>
  );
}
