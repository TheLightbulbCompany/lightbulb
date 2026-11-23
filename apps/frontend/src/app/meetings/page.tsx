import { MeetingsLanding } from "@/components/landing/MeetingsLanding";
import { pageMetadata } from "@/lib/seo";

export const metadata = pageMetadata({
  title: "Meeting Notes in Google Meet | Lightbulb Meetings",
  description:
    "Lightbulb Meetings joins your Google Meet when invited, speaks only when named, and leaves notes, decisions and follow-ups. Join the waitlist.",
  path: "/meetings",
});

// Lightbulb Meetings' waitlist page: one Google Meet call, told by its live captions (MeetingsLanding).
// It sells nothing and shows no price.
export default function MeetingsPage() {
  return <MeetingsLanding />;
}
