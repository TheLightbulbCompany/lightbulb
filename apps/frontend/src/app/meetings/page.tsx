import { Footer } from "@/components/landing/Footer";
import { Navbar } from "@/components/landing/Navbar";
import { RequestSetupForm } from "@/components/requestSetup/RequestSetupForm";
import { pageMetadata } from "@/lib/seo";

export const metadata = pageMetadata({
  title: "Meeting Notes in Google Meet | Lightbulb Meetings",
  description:
    "Lightbulb Meetings joins your Google Meet when invited, speaks only when named, and leaves notes, decisions and follow-ups. Join the waitlist.",
  path: "/meetings",
});

// Each item is a VERIFIED, prod-deployed row in .agents/product-marketing.md and docs/voice-status.md (5.1, 5.5, 5.6, 5.15).
const IN_THE_MEETING: [string, string][] = [
  [
    "Joins when you invite it",
    "Add its address as a guest on a Google Calendar event with a Meet link. Only invites from people in your business count.",
  ],
  ["Tells everyone it's there", "It says it has joined, that the meeting is recorded, and how to wake it."],
  ["Speaks when you say its name", "Otherwise it listens."],
  ["Asks before it spends", "Spending, sending or publishing waits for your tap."],
];

export default function MeetingsPage() {
  return (
    <main className="ld-page min-h-screen">
      <Navbar />
      <section className="mx-auto max-w-2xl px-6 py-20 md:py-28">
        <h1 className="font-editorial-serif text-5xl leading-tight text-[var(--lb-ink)] md:text-6xl">
          Leave every meeting with the notes already written.
        </h1>
        <p className="mt-6 text-lg leading-8 text-[var(--lb-ink-soft)]">
          Lightbulb Meetings joins your Google Meet when you invite it. It stays quiet until someone says its name,
          answers what it&apos;s asked, and leaves notes, decisions and follow-ups for you afterwards.
        </p>
        <div className="mt-12">
          <RequestSetupForm waitlist="meetings" />
        </div>
      </section>
      <section className="lb-sec">
        <div className="lb-wrap">
          <h2 className="lb-h2">What it does in your meeting</h2>
          <ul className="lb-does">
            {IN_THE_MEETING.map(([title, body]) => (
              <li key={title}>
                <b>{title}</b>
                {body}
              </li>
            ))}
          </ul>
          <p className="lb-sec-sub">Works with Google Meet. Not Zoom or Microsoft Teams.</p>
        </div>
      </section>
      <Footer />
    </main>
  );
}
