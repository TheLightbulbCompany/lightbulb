/**
 * The three trade pages (/salons, /spas, /barbershops). Every capability named
 * here is VERIFIED in .agents/product-marketing.md; the call scenes, service
 * words and busy times are what make each page its own.
 */

export interface Vertical {
  slug: "salons" | "spas" | "barbershops";
  path: string;
  /** Anchor text when another page links here. */
  linkLabel: string;
  title: string;
  description: string;
  h1: string;
  intro: string;
  /** What callers ask, and what the assistant does with it. */
  calls: { ask: string; does: string }[];
  servicesIntro: string;
  services: string[];
  busy: string;
  faq: { q: string; a: string }[];
}

const OWN_CALENDAR =
  "It books into its own calendar, which you see on your Home, or into your Google Calendar.";

export const VERTICALS: Vertical[] = [
  {
    slug: "salons",
    path: "/salons",
    linkLabel: "hair and nail salons",
    title: "AI Receptionist for Hair & Nail Salons | Lightbulb",
    description:
      "An AI receptionist for your salon. It answers while your hands are in color, books cuts, color, nails and lashes with the right stylist, and takes messages.",
    h1: "An AI receptionist for your salon",
    intro:
      "You're halfway through a balayage and the phone rings again. Your assistant picks up, answers the question, and books the appointment with a stylist who does that service. Your hands stay where they are.",
    calls: [
      {
        ask: "“Do you have anything Saturday for a balayage?”",
        does: "It checks your opening hours, how long a balayage takes and who does color, offers the open times, and books one.",
      },
      {
        ask: "“How much is a full set?”",
        does: "It answers from the services and prices you entered. If a price isn't there, it says it will check and takes a message.",
      },
      {
        ask: "“I need to move my appointment to next week.”",
        does: "It moves it when the caller's carrier verifies their number. Otherwise it takes a message for you.",
      },
      {
        ask: "“Do you do bridal parties?”",
        does: "If you haven't told it, it takes a message and asks you in Needs you. You answer once, and it knows next time.",
      },
    ],
    servicesIntro:
      "Enter each service with its length, its price and the people who do it, and add a buffer so a color process doesn't run into the next client. At setup it reads your website to draft the list, and you check it.",
    services: [
      "Women's and men's cuts",
      "Blowouts",
      "Single-process color",
      "Highlights and balayage",
      "Gel manicures and pedicures",
      "Full sets and fills",
      "Lash extensions and lifts",
      "Brow waxing and tinting",
    ],
    busy:
      "Salon calls come in with a client in the chair, mid-color, with wet hands or gloves on. They come on a Saturday when every chair is full, and after you close, when people plan the week ahead.",
    faq: [
      {
        q: "Can it book with the right stylist?",
        a: "Yes. You list your stylists and the services each one does, and it books someone who does that service. It doesn't keep each stylist's own schedule or days off.",
      },
      {
        q: "Does it work with my salon booking app?",
        a: `${OWN_CALENDAR} It doesn't book into Square, Vagaro, Fresha, GlossGenius, Booksy or Boulevard.`,
      },
      {
        q: "Will my regulars know it's an AI?",
        a: "It answers with the name and greeting you choose, in one of three voices. The default greeting says it's an AI assistant, and it tells the truth if a caller sincerely asks. It can greet returning clients by name when their carrier verifies their number.",
      },
      {
        q: "Can it speak Spanish?",
        a: "Yes. Switch on Spanish and it opens in English, then follows the caller into Spanish.",
      },
    ],
  },
  {
    slug: "spas",
    path: "/spas",
    linkLabel: "day spas and massage studios",
    title: "AI Receptionist for Day Spas & Massage Studios | Lightbulb",
    description:
      "An AI receptionist for your spa. It answers while you're in a session, explains your treatments, and books times that fit each treatment and your room turnover.",
    h1: "An AI receptionist for your spa",
    intro:
      "Nobody wants a phone ringing in the treatment room. While you're an hour into a massage, your assistant answers the line, tells the caller what's on your menu, and books a time that fits the treatment and the turnover after it.",
    calls: [
      {
        ask: "“Do you have a 90-minute massage open Friday afternoon?”",
        does: "It looks for a slot long enough for that treatment, inside your hours, with a therapist who offers it, and books it.",
      },
      {
        ask: "“What's the difference between your facials?”",
        does: "It describes each treatment from what you wrote. It doesn't invent details you didn't give it.",
      },
      {
        ask: "“I need to cancel tomorrow.”",
        does: "It cancels the booking when the caller's carrier verifies their number, so the time opens up. Otherwise it takes a message.",
      },
      {
        ask: "“Where do I park?”",
        does: "It gives the directions and parking notes you added. If you haven't added them, it takes a message.",
      },
    ],
    servicesIntro:
      "Give every treatment its real length and add a buffer for turning over the room, and it only offers times that fit. List which therapist offers which treatment. At setup it reads your website to draft your menu, and you check it.",
    services: [
      "Swedish and deep tissue massage",
      "Hot stone massage",
      "Prenatal massage",
      "Facials",
      "Body wraps and scrubs",
    ],
    busy:
      "Spa calls come in during sessions, when nobody can step out of a quiet room. They come in the evening and on days off, when people plan a treat for the weekend.",
    faq: [
      {
        q: "Is it for medical spas?",
        a: "No. It's for day spas and massage studios. It isn't built for medical spas or any practice that handles health information.",
      },
      {
        q: "Does it work with my spa software?",
        a: `${OWN_CALENDAR} It doesn't book into Mindbody, Boulevard, Vagaro or other spa software.`,
      },
      {
        q: "Can it take payment for a gift card or deposit?",
        a: "No. It doesn't take payments and removes card numbers from every record it keeps. It can tell callers how to buy, using the answer you give it.",
      },
      {
        q: "Can I check what it told a caller?",
        a: "Yes. Every call shows who called, a one-line summary, the steps it took and the transcript.",
      },
    ],
  },
  {
    slug: "barbershops",
    path: "/barbershops",
    linkLabel: "barbershops",
    title: "AI Receptionist for Barbershops | Lightbulb",
    description:
      "An AI receptionist for your barbershop. It answers while the clippers run, books cuts with a barber who does them, and answers walk-in questions your way.",
    h1: "An AI receptionist for your barbershop",
    intro:
      "Clippers in one hand, the phone buzzing on the counter. Your assistant picks up, answers the quick questions a barbershop gets all day, and books the cut with a barber who does it. The chair keeps moving.",
    calls: [
      {
        ask: "“Do you take walk-ins?”",
        does: "It answers with your own rule, from the instructions you write for it.",
      },
      {
        ask: "“Can I get a fade after 5 today?”",
        does: "It finds an open time with a barber who does fades, inside your hours, and books it.",
      },
      {
        ask: "“How much for a kids' cut?”",
        does: "It answers from the prices you entered.",
      },
      {
        ask: "“Are you open on the holiday?”",
        does: "It answers from your hours and the closures you add.",
      },
    ],
    servicesIntro:
      "List each service with its length and price, and which barbers do it. Set lengths and buffers to match how your shop really runs. At setup it reads your website to draft the list, and you check it.",
    services: [
      "Haircuts",
      "Skin fades and tapers",
      "Lineups",
      "Beard trims",
      "Hot towel shaves",
      "Kids' cuts",
      "Head shaves",
    ],
    busy:
      "Barbershop calls come in when every chair is full and the clippers are running. They come after you close, when clients plan tomorrow's cut.",
    faq: [
      {
        q: "Can it book with the right barber?",
        a: "Yes. You list your barbers and the services each one does, and it books a barber who does that service. It doesn't keep each barber's own schedule or days off.",
      },
      {
        q: "What if two people call at once?",
        a: "It handles several calls at once on one line.",
      },
      {
        q: "Can I keep my shop's number?",
        a: "Yes. Forward missed and busy calls, or all calls, with your carrier's code. The app gives you the code and checks that forwarding works. Or get a new local number.",
      },
      {
        q: "Can I try it before I pay?",
        a: "Your assistant's phone number is set up when you choose the plan, so your first test call happens after payment. The plan is month to month, and you cancel the renewal yourself in Settings.",
      },
    ],
  },
];

export function getVertical(slug: Vertical["slug"]): Vertical {
  return VERTICALS.find((v) => v.slug === slug)!;
}
