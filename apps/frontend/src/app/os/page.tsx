import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { RequestSetupForm } from "@/components/requestSetup/RequestSetupForm";
import { pageMetadata } from "@/lib/seo";

export const metadata = pageMetadata({
  title: "Lightbulb OS, a Voice App for the Mac | Lightbulb",
  description:
    "Lightbulb OS is a voice app for the Mac. It isn't out yet. Join the waitlist and we'll email you when you can try it.",
  path: "/os",
});

// The app is built outside this repo and not released: no download link, no capability claim.
export default function OsPage() {
  return (
    <main className="ld-page min-h-screen">
      <Navbar />
      <section className="mx-auto max-w-2xl px-6 py-20 md:py-28">
        <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--lb-ink)] md:text-6xl">
          Your Mac, by voice.
        </h1>
        <p className="mt-6 text-lg leading-8 text-[var(--lb-ink-soft)]">
          Lightbulb OS is a voice app for the Mac. It isn&apos;t out yet. Leave your email and we&apos;ll tell you when
          you can try it.
        </p>
        <div className="mt-12">
          <RequestSetupForm waitlist="os" />
        </div>
      </section>
      <Footer />
    </main>
  );
}
