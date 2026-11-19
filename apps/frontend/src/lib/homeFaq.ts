/**
 * The homepage FAQ. One source for the visible section (HomeFaq) and the
 * FAQPage JSON-LD (lib/seo.tsx), which must match the page word for word.
 * "Privacy Policy" in an answer renders as a link to /privacy.
 * Answers use only VERIFIED claims from .agents/product-marketing.md.
 */
export const HOME_FAQ: { q: string; a: string }[] = [
  {
    q: "Will my clients mind talking to an AI?",
    a: "It answers in a natural voice, with the name you pick and a greeting you write. If a caller asks for a person, it transfers them to your mobile or takes a message. If someone sincerely asks whether it's a person, it tells the truth.",
  },
  {
    q: "What if it gets something wrong?",
    a: "It only says what you put on its page: your hours, services, prices and staff. When it doesn't know, it says it will check, takes a message, and the question lands in Needs you. Before booking it repeats the name back, and every call shows the transcript and each step it took.",
  },
  {
    q: "Do I have to change my phone number?",
    a: "No. Forward missed and busy calls, or all calls, with your carrier's code. The app gives you the code and checks that forwarding works. Or get a new local number.",
  },
  {
    q: "My bookings are in Square, Vagaro or Fresha. Will it work?",
    a: "Not yet. Today it books into Google Calendar or its own calendar, which you see on your Home. It doesn't write into Square, Vagaro, Fresha or other booking apps. If your shop runs on one of them, tell us.",
  },
  {
    q: "Will I get surprise charges?",
    a: "One monthly plan with minutes included. A meter in Settings shows the minutes you've used, and you choose what happens when they run out: keep answering at the per-minute rate, or send calls to your phone. Month to month, and you cancel the renewal yourself in Settings.",
  },
  {
    q: "Is it safe? What about payments and privacy?",
    a: "It doesn't take payments and removes card numbers from every record. Callers only reach their own bookings, and it won't share anything about other clients. You decide what it may do. More in our Privacy Policy.",
  },
  {
    q: "Who is it not for yet?",
    a: "Medical, dental and med spa practices that handle health information, businesses with several locations, and businesses outside the US.",
  },
];
