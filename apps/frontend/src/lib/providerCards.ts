/**
 * Shared provider-card metadata for the three provider paths.
 *
 * Used by:
 *   - `components/landing/PricingThreeCard.tsx` — renders cards with
 *     anchor links to /sign-up?provider=…
 *   - `components/chat/ProvisioningStepper.tsx#ProviderPicker` — renders an
 *     informational provider comparison beside one workspace subscription
 *     CTA. Provider choice is made after entitlement.
 *
 * Previously: three near-identical card arrays lived inline across these
 * two surfaces. The landing copy is the canonical version; the
 * provisioning surface uses the same wording so pricing intent → checkout
 * doesn't read like a different product.
 */

export type ProviderChoiceId = "chatgpt_oauth" | "byo_key" | "bedrock_claude" | "opencode_zen";

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
  { id: "opencode_zen", title: "Muse Spark 1.3", subtitle: "Included inference — no credits or API key needed" },
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
    subtitle: "Inference uses your separate ChatGPT subscription",
    trialNote: "Choose a provider after workspace billing is active",
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
    subtitle: "Your selected provider bills inference separately",
    trialNote: "Add your key after workspace billing is active",
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
    subtitle: "Managed Claude inference uses prepaid workspace credits",
    trialNote: "Choose and fund managed inference separately after billing",
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

/** Backend remains authoritative; unavailable choices should not invite a failed switch. */
export function providerSwitchCards(museAvailable = false, entryPlan = false): ProviderSwitchCard[] {
  return PROVIDER_SWITCH_CARDS.filter((card) =>
    card.id === "opencode_zen" ? museAvailable : !entryPlan,
  );
}
