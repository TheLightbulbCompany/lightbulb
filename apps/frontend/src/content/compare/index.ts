/**
 * The comparison pages: Lightbulb next to the ways a salon, spa or barbershop
 * already answers its phone. The Lightbulb column states only claims marked
 * VERIFIED in .agents/product-marketing.md. The other column describes a kind
 * of service, never a named vendor; a third-party figure carries its source.
 */

export interface CompareRow {
  label: string;
  ours: string;
  other: string;
  /** Where a third-party figure in `other` came from. */
  source?: string;
}

export interface ComparePage {
  slug: string;
  /** Public path. */
  path: string;
  title: string;
  description: string;
  h1: string;
  intro: string;
  /** The other column's heading. */
  other: string;
  rows: CompareRow[];
  betterTitle: string;
  better: string[];
  faq: { q: string; a: string }[];
}

/** The day every row was last checked against the product and its sources. */
export const COMPARE_UPDATED = "2026-11-18";

const INVOCA = "https://invoca.com/reports/the-invoca-lead-conversion-benchmarks-report-2026";
const BLS = "https://www.bls.gov/ooh/office-and-administrative-support/receptionists.htm";

const AFTER_HOURS = "Answers 24/7, including nights, Sundays and days off.";
const SEE_IT = "Who called, a one-line summary, the steps it took, and the transcript.";
const COST = "One monthly plan with minutes included, then a per-minute rate. Month to month; cancel yourself in Settings.";

export const COMPARE_PAGES: ComparePage[] = [
  {
    slug: "voicemail",
    path: "/compare/voicemail",
    title: "AI Receptionist vs Voicemail for Salons | Lightbulb",
    description:
      "Voicemail takes a message and waits for your call back. An AI receptionist answers, books the appointment and shows you the transcript. Which fits your shop.",
    h1: "Lightbulb vs voicemail",
    intro:
      "When you're with a client, the phone goes to voicemail. The caller hears your greeting, then leaves a message or calls the next salon. Lightbulb answers instead: it talks with the caller, answers from what you told it, and books the appointment while you keep working.",
    other: "Voicemail",
    rows: [
      { label: "When the phone rings mid-service", ours: "Answers and talks with the caller.", other: "Plays your greeting and records a message, if the caller leaves one." },
      { label: "After hours", ours: AFTER_HOURS, other: "Records messages until you open again." },
      { label: "Booking", ours: "Books, moves and cancels appointments during the call, inside your opening hours.", other: "You call back and book it yourself." },
      { label: "\"Are you open?\" \"How much is a cut?\"", ours: "Answers from the hours, services and prices you entered.", other: "Only what your greeting says." },
      { label: "Calls that reach no one", ours: "Handles several calls at once.", other: "44% of business calls never reach a person (Invoca, 70 million calls, 2026).", source: INVOCA },
      { label: "What you see afterwards", ours: SEE_IT, other: "A recording to listen to, then a call to return." },
      { label: "Cost", ours: COST, other: "Usually included with your phone line." },
    ],
    betterTitle: "When voicemail is enough",
    better: [
      "If your phone rarely rings while you're busy, and your callers are happy to wait for a call back, voicemail costs nothing extra and does the job.",
      "Voicemail also needs no setup. An AI receptionist needs your hours, services and prices once (it drafts them from your website for you to check) and your line forwarded to it.",
    ],
    faq: [
      {
        q: "Can I keep my voicemail?",
        a: "Yes. Forward only missed and busy calls to your assistant, and you still answer when you can. Or forward all calls. The app gives you your carrier's code and checks that forwarding works.",
      },
      {
        q: "What if the assistant doesn't know the answer?",
        a: "It says it will check and takes a message. The question lands in your Needs you list, and once you answer it, the assistant knows next time.",
      },
      {
        q: "Will I still get messages?",
        a: "Yes. It takes the caller's name and number, every message shows up as a Needs you card, and the call's transcript is on your Home.",
      },
    ],
  },
  {
    slug: "receptionist",
    path: "/compare/receptionist",
    title: "AI Receptionist vs a Front Desk Receptionist | Lightbulb",
    description:
      "A front desk person greets walk-ins and checks clients out. An AI receptionist answers the phone 24/7 and books. What each does well and how they work together.",
    h1: "Lightbulb vs a front desk receptionist",
    intro:
      "A good front desk person knows your regulars, greets walk-ins, checks clients out and answers the phone between all of that. Lightbulb only does the phone, but it does it at any hour, and it can take several calls while your desk is busy with the client in front of them.",
    other: "Front desk receptionist",
    rows: [
      { label: "Hours", ours: AFTER_HOURS, other: "Their shift." },
      { label: "Two lines ringing at once", ours: "Handles several calls at once.", other: "One call at a time, between clients at the desk." },
      { label: "Booking", ours: "Books, moves and cancels appointments during the call, with a staff member who does that service.", other: "Books, moves and cancels, in whatever system you use." },
      { label: "Your regulars", ours: "Remembers returning callers and can greet them by name when their carrier verifies their number.", other: "Knows them by face and voice." },
      { label: "Walk-ins, checkout, payments", ours: "Phone only. It doesn't take payments.", other: "Yes." },
      { label: "Languages", ours: "English, or English and Spanish.", other: "Whatever they speak." },
      { label: "Cost", ours: COST, other: "US median pay for receptionists: $18.27 an hour, $38,010 a year (BLS, May 2025), before taxes and benefits.", source: BLS },
    ],
    betterTitle: "When a receptionist is the better choice",
    better: [
      "If your shop is busy enough that someone needs to be at the desk all day, greeting walk-ins, checking clients out and selling retail, a person does things an AI receptionist can't.",
      "You can have both. Forward only missed and busy calls, and the assistant picks up when your desk is already on the phone, before opening and after closing.",
    ],
    faq: [
      {
        q: "Will it replace my front desk?",
        a: "It answers the phone. It doesn't greet walk-ins, check clients out or take payments. You can use it only for the calls your desk misses and for after-hours calls.",
      },
      {
        q: "Can a caller still talk to a person?",
        a: "Yes. When a caller asks for a person, it transfers the call to your mobile, only during opening hours if you choose. If the transfer doesn't go through, you get a Needs you card.",
      },
      {
        q: "Does it know which stylist does what?",
        a: "Yes. You list your staff and the services each one does, and it books someone who does that service. It doesn't keep each person's own schedule or days off.",
      },
    ],
  },
  {
    slug: "answering-service",
    path: "/compare/answering-service",
    title: "AI Receptionist vs a Human Answering Service | Lightbulb",
    description:
      "Answering services bill by the minute or the call, and many only take messages. Lightbulb's AI receptionist books into your calendar and shows every transcript.",
    h1: "Lightbulb vs a human answering service",
    intro:
      "A human answering service puts a trained person on your line. Many take messages; some book appointments, sometimes for an added fee. Lightbulb answers in the name and voice you choose, books into your calendar during the call, and shows you exactly what it said and did.",
    other: "Human answering service",
    rows: [
      { label: "Who answers", ours: "Your assistant, with the name, greeting and voice you choose.", other: "A person on the service's team." },
      { label: "Booking", ours: "Books, moves and cancels during the call, into its own calendar or Google Calendar.", other: "Varies: many take messages, some book for an added fee." },
      { label: "Hours", ours: AFTER_HOURS, other: "Varies by plan." },
      { label: "What you see afterwards", ours: SEE_IT, other: "Usually a message summary." },
      { label: "Languages", ours: "English, or English and Spanish.", other: "Varies by service." },
      { label: "How you're billed", ours: COST, other: "Usually per minute or per call." },
    ],
    betterTitle: "When a human service is the better choice",
    better: [
      "If your callers need a real person every time, or your calls are often long and unusual, a human service handles that better.",
      "If you need a language other than English or Spanish, or your bookings must go into Square, Vagaro, Fresha or another salon platform, ask a human service what they support. Lightbulb books only into its own calendar or Google Calendar today.",
    ],
    faq: [
      {
        q: "Does it say it's an AI?",
        a: "The default greeting says it's an AI assistant, and if a caller sincerely asks whether it's a person, it tells the truth. You can write your own greeting instead.",
      },
      {
        q: "How do I avoid surprise charges?",
        a: "A meter in Settings shows the minutes you've used. When they run out, you choose: keep answering at the per-minute rate, or send calls to your phone. It's month to month, and you cancel the renewal yourself in Settings.",
      },
      {
        q: "Can I check what it told my callers?",
        a: "Yes. Every call shows a one-line summary, the steps it took and the full transcript.",
      },
    ],
  },
];

export function getComparePage(slug: string): ComparePage | undefined {
  return COMPARE_PAGES.find((p) => p.slug === slug);
}
