/**
 * /phone's questions. One source for the visible section (components/landing/PhoneLanding.tsx) and the
 * FAQPage JSON-LD (lib/seo.tsx), which must match the page word for word. The words are the owner-approved
 * page's (2026-11-24); answers use only VERIFIED claims from .agents/product-marketing.md.
 */
export const HOME_FAQ: { q: string; a: string }[] = [
  {
    q: "Will callers know it's an AI?",
    a: "It tells callers it's an AI assistant when asked, and the default greeting says so. You can write your own greeting.",
  },
  {
    q: "Can I keep my number?",
    a: "Yes. Forward missed and busy calls, or all calls, with your carrier's code, and the app checks that it works. Or get a new local number.",
  },
  {
    q: "Where do the bookings go?",
    a: "Into your Google Calendar or Nova's own calendar on your Home. It doesn't book into Square, Vagaro or Fresha yet.",
  },
  {
    q: "What if it doesn't know something?",
    a: "It says it will check, takes a message, and puts the question in Needs you. Answer once and it knows next time.",
  },
  {
    q: "How much is it?",
    a: "One monthly plan with minutes included. You see the price when you sign up. Month to month, and you cancel yourself in Settings.",
  },
  {
    q: "Does it take payments?",
    a: "No. It doesn't take payments or keep card numbers.",
  },
];
