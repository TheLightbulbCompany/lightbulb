/**
 * Shared provider-card metadata for the three provider paths.
 *
 * Used by:
 *   - `components/landing/PricingThreeCard.tsx` — renders cards with
 *     anchor links to /sign-up?provider=…
 *   - `components/chat/ProvisioningStepper.tsx#ProviderPicker` — renders
 *     cards with click handlers that POST /billing/trial-checkout and
 *     redirect to Stripe Checkout (billed immediately — no Stripe trial).
 *     All three cards show for personal and org owners alike; for orgs
 *     the subscribing admin's ChatGPT account backs the whole workspace.
 *
 * Previously: three near-identical card arrays lived inline across these
 * two surfaces. The landing copy is the canonical version; the
 * provisioning surface uses the same wording so pricing intent → checkout
 * doesn't read like a different product.
 */

export type ProviderChoiceId = "chatgpt_oauth" | "byo_key" | "bedrock_claude";

export interface ProviderCard {
  id: ProviderChoiceId;
  title: string;
  subtitle: string;
  trialNote: string;
  bullets: string[];
  cta: string;
  /** Landing-page anchor target. Provisioning surface ignores this. */
  href: string;
  highlighted?: boolean;
}

/**
 * In-app (post-signup) provider choices. Same three paths as
 * `PROVIDER_CARDS`, minus the pricing/marketing copy — an owner picking here
 * already has a subscription, so the sales framing would be noise.
 *
 * Used by:
 *   - `ChangeProviderModal#PickerStage` — the deliberate switch flow.
 *   - `LLMPanel`'s empty state — the inline first-pick.
 */
export interface ProviderSwitchCard {
  id: ProviderChoiceId;
  title: string;
  subtitle: string;
}

export const PROVIDER_SWITCH_CARDS: ProviderSwitchCard[] = [
  {
    id: "bedrock_claude",
    title: "Powered by Claude",
    subtitle: "Bedrock-managed Claude credits",
  },
  {
    id: "chatgpt_oauth",
    title: "Sign in with ChatGPT",
    subtitle: "Inference via your ChatGPT subscription",
  },
  {
    id: "byo_key",
    title: "Bring your own API key",
    subtitle: "OpenAI or Anthropic — your key, your billing",
  },
];

export const PROVIDER_CARDS: ProviderCard[] = [
  {
    id: "chatgpt_oauth",
    title: "Sign in with ChatGPT",
    subtitle: "$50 / month + your ChatGPT subscription",
    trialNote: "Billed monthly, starts today",
    bullets: [
      "GPT-5.5 included via your ChatGPT account",
      "All channels (Telegram, Discord, WhatsApp)",
      "Always-on container",
    ],
    cta: "Get started",
    href: "/sign-up?provider=chatgpt_oauth",
  },
  {
    id: "byo_key",
    title: "Bring your own API key",
    subtitle: "$50 / month + your provider bill",
    trialNote: "Billed monthly, starts today",
    bullets: [
      "OpenAI or Anthropic — your key, your billing",
      "All channels",
      "Always-on container",
    ],
    cta: "Get started",
    href: "/sign-up?provider=byo_key",
  },
  {
    id: "bedrock_claude",
    title: "Powered by Claude",
    subtitle: "$50 / month + Claude credits",
    trialNote: "Pay-as-you-go credits",
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
