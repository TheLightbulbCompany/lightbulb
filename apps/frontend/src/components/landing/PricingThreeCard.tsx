"use client";
import Link from "next/link";

type Card = {
  id: "chatgpt_oauth" | "byo_key" | "bedrock_claude";
  title: string;
  subtitle: string;
  trialNote: string;
  bullets: string[];
  cta: string;
  href: string;
  highlighted?: boolean;
};

const CARDS: Card[] = [
  {
    id: "chatgpt_oauth",
    title: "Sign in with ChatGPT",
    subtitle: "$50 / month + your ChatGPT subscription",
    trialNote: "14-day trial",
    bullets: [
      "GPT-5.5 included via your ChatGPT account",
      "All channels (Telegram, Discord, WhatsApp)",
      "Always-on container",
    ],
    cta: "Start trial",
    href: "/sign-up?provider=chatgpt_oauth",
  },
  {
    id: "byo_key",
    title: "Bring your own API key",
    subtitle: "$50 / month + your provider bill",
    trialNote: "14-day trial",
    bullets: [
      "OpenAI or Anthropic — your key, your billing",
      "All channels",
      "Always-on container",
    ],
    cta: "Start trial",
    href: "/sign-up?provider=byo_key",
  },
  {
    id: "bedrock_claude",
    title: "Powered by Claude",
    subtitle: "$50 / month + Claude credits",
    trialNote: "Pay-as-you-go credits, 1.4× markup",
    bullets: [
      "Claude Sonnet 4.6 + Opus 4.7",
      "All channels",
      "Always-on container",
    ],
    cta: "Get started",
    href: "/sign-up?provider=bedrock_claude",
    highlighted: true,
  },
];

export function PricingThreeCard() {
  return (
    <section
      id="pricing"
      className="container mx-auto px-4 py-16 md:py-24"
    >
      <h2 className="text-3xl md:text-4xl font-bold text-center mb-2">
        One price. Three ways to power it.
      </h2>
      <p className="text-center text-muted-foreground mb-12 max-w-2xl mx-auto">
        Every plan is $50/month for the always-on agent infrastructure.
        Choose how you want to pay for inference.
      </p>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 max-w-6xl mx-auto">
        {CARDS.map((card) => (
          <div
            key={card.id}
            className={
              "rounded-2xl border bg-card p-8 flex flex-col " +
              (card.highlighted
                ? "border-primary shadow-lg shadow-primary/10"
                : "border-border")
            }
          >
            <h3 className="text-xl font-semibold mb-1">{card.title}</h3>
            <p className="text-sm text-muted-foreground mb-1">
              {card.subtitle}
            </p>
            <p className="text-sm text-primary font-medium mb-6">
              {card.trialNote}
            </p>
            <ul className="space-y-2 flex-1 mb-8">
              {card.bullets.map((b) => (
                <li key={b} className="flex items-start gap-2 text-sm">
                  <span aria-hidden className="text-primary">
                    ✓
                  </span>
                  <span>{b}</span>
                </li>
              ))}
            </ul>
            <Link
              href={card.href}
              className="inline-flex items-center justify-center rounded-md bg-primary text-primary-foreground px-4 py-2 text-sm font-medium hover:bg-primary/90 transition"
            >
              {card.cta}
            </Link>
          </div>
        ))}
      </div>
    </section>
  );
}

export default PricingThreeCard;
